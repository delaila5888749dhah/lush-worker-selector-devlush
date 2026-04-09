import time
import unittest
from unittest.mock import MagicMock, patch

from modules.common.exceptions import (
    CycleExhaustedError,
    InvalidTransitionError,
    SessionFlaggedError,
)
from modules.common.types import CardInfo, State, WorkerTask
from modules.fsm.main import (
    cleanup_worker,
    get_current_state_for_worker,
    reset_states,
    transition_for_worker,
)
from modules.watchdog.main import reset as _reset_watchdog
from integration.orchestrator import (
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _submitted_task_ids,
    _IDEMPOTENCY_STORE_PATH,
    _IDEMPOTENCY_TTL,
    _load_idempotency_store,
    _save_idempotency_store,
    handle_outcome,
    initialize_cycle,
    run_cycle,
    run_payment_step,
)


def _make_task(order_queue=None):
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="07",
        exp_year="27",
        cvv="123",
    )
    return WorkerTask(
        recipient_email="test@example.com",
        amount=100,
        primary_card=card,
        order_queue=tuple(order_queue) if order_queue else (),
    )


class InitializeCycleTests(unittest.TestCase):
    def setUp(self):
        reset_states()
        cleanup_worker("default")

    def tearDown(self):
        cleanup_worker("default")

    def test_registers_all_states(self):
        for name in ("ui_lock", "success", "vbv_3ds", "declined"):
            initialize_cycle()
            state = transition_for_worker("default", name)
            self.assertEqual(state.name, name)

    def test_is_idempotent(self):
        initialize_cycle()
        initialize_cycle()
        self.assertIsNone(get_current_state_for_worker("default"))

    def test_resets_current_state(self):
        initialize_cycle()
        transition_for_worker("default", "success")
        initialize_cycle()
        self.assertIsNone(get_current_state_for_worker("default"))

    def test_configures_rollout_with_monitor_callbacks(self):
        with (
            patch("integration.orchestrator.rollout") as mock_rollout,
            patch("integration.orchestrator.monitor") as mock_monitor,
        ):
            initialize_cycle()
        mock_rollout.configure.assert_called_once_with(
            mock_monitor.check_rollback_needed,
            mock_monitor.save_baseline,
        )


class HandleOutcomeTests(unittest.TestCase):
    def test_none_state_returns_retry(self):
        self.assertEqual(handle_outcome(None, []), "retry")

    def test_success_returns_complete(self):
        self.assertEqual(handle_outcome(State("success"), []), "complete")

    def test_declined_with_queue_returns_retry_new_card(self):
        queue = [MagicMock()]
        self.assertEqual(handle_outcome(State("declined"), queue), "retry_new_card")

    def test_declined_empty_queue_returns_retry(self):
        self.assertEqual(handle_outcome(State("declined"), []), "retry")

    def test_ui_lock_returns_retry(self):
        self.assertEqual(handle_outcome(State("ui_lock"), []), "retry")

    def test_vbv_3ds_clears_fields_and_returns_await_3ds(self):
        with patch("integration.orchestrator.cdp") as mock_cdp:
            result = handle_outcome(State("vbv_3ds"), [])
        self.assertEqual(result, "await_3ds")
        mock_cdp.clear_card_fields.assert_called_once_with(worker_id="default")

    def test_vbv_3ds_cdp_failure_still_returns_await_3ds(self):
        """BUG-003: CDP error in clear_card_fields must be swallowed; await_3ds returned."""
        with patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.clear_card_fields.side_effect = RuntimeError("browser crashed")
            result = handle_outcome(State("vbv_3ds"), [])
        self.assertEqual(result, "await_3ds")

    def test_unknown_state_returns_retry(self):
        self.assertEqual(handle_outcome(State("unknown_state"), []), "retry")


class RunPaymentStepTests(unittest.TestCase):
    def setUp(self):
        _reset_watchdog()
        reset_states()
        cleanup_worker("default")

    def tearDown(self):
        cleanup_worker("default")

    def test_raises_runtime_error_when_no_driver_registered(self):
        with patch("integration.orchestrator.billing") as mock_billing:
            mock_billing.select_profile.return_value = MagicMock()
            with self.assertRaises(RuntimeError):
                run_payment_step(_make_task())

    def test_raises_cycle_exhausted_from_billing(self):
        with patch("integration.orchestrator.billing") as mock_billing:
            mock_billing.select_profile.side_effect = CycleExhaustedError("empty")
            with self.assertRaises(CycleExhaustedError):
                run_payment_step(_make_task())

    def test_zip_code_forwarded_to_select_profile(self):
        with patch("integration.orchestrator.billing") as mock_billing:
            mock_billing.select_profile.side_effect = CycleExhaustedError("empty")
            with self.assertRaises(CycleExhaustedError):
                run_payment_step(_make_task(), zip_code="90210")
        mock_billing.select_profile.assert_called_once_with("90210")



    def test_fill_card_called_with_primary_card(self):
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 25.0
            mock_fsm.get_current_state_for_worker.return_value = None
            run_payment_step(task)
        mock_cdp.fill_card.assert_called_once_with(task.primary_card, worker_id="default")

    def test_raises_session_flagged_on_watchdog_timeout(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task())

    def test_returns_state_and_total(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 49.99
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            state, total = run_payment_step(_make_task())
        self.assertEqual(total, 49.99)
        self.assertEqual(state.name, "success")


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        _reset_watchdog()
        reset_states()
        cleanup_worker("default")

    def tearDown(self):
        cleanup_worker("default")

    def test_run_cycle_complete_on_success(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 99.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            action, state, total = run_cycle(_make_task())
        self.assertEqual(action, "complete")
        self.assertEqual(total, 99.0)

    def test_run_cycle_retry_when_no_state(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = None
            action, state, total = run_cycle(_make_task())
        self.assertEqual(action, "retry")
        self.assertIsNone(state)

    def test_run_cycle_propagates_session_flagged_error(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            with self.assertRaises(SessionFlaggedError):
                run_cycle(_make_task())

    def test_run_cycle_initializes_fsm_before_payment(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 1.0
            mock_fsm.get_current_state_for_worker.return_value = None
            run_cycle(_make_task())
        mock_fsm.initialize_for_worker.assert_called_once_with("default")


class WorkerTaskFrozenTests(unittest.TestCase):
    """WorkerTask must be immutable (frozen=True) to prevent accidental mutation."""

    def test_worker_task_is_frozen(self):
        task = _make_task()
        with self.assertRaises(AttributeError):
            task.amount = 200

    def test_worker_task_created_with_correct_values(self):
        task = _make_task()
        self.assertEqual(task.recipient_email, "test@example.com")
        self.assertEqual(task.amount, 100)
        self.assertEqual(task.primary_card.card_number, "4111111111111111")
        self.assertEqual(task.order_queue, ())

    def test_worker_task_has_task_id(self):
        task = _make_task()
        self.assertIsInstance(task.task_id, str)
        self.assertGreater(len(task.task_id), 0)

    def test_worker_task_task_id_unique(self):
        task1 = _make_task()
        task2 = _make_task()
        self.assertNotEqual(task1.task_id, task2.task_id)

    def test_card_info_is_frozen(self):
        card = CardInfo(card_number="4111111111111111", exp_month="07", exp_year="27", cvv="123")
        with self.assertRaises(AttributeError):
            card.card_number = "tampered"


class WorkerIdPropagationTests(unittest.TestCase):
    """Verify worker_id is correctly threaded through the orchestrator calls."""

    def test_worker_id_forwarded_to_watchdog(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 10.0
            mock_fsm.get_current_state_for_worker.return_value = None
            run_payment_step(_make_task(), worker_id="worker-42")
        mock_watchdog.enable_network_monitor.assert_called_once_with("worker-42")
        mock_watchdog.wait_for_total.assert_called_once_with(
            "worker-42", timeout=30,
        )


class WatchdogCleanupTests(unittest.TestCase):
    """Verify watchdog session is cleaned up when CDP raises an exception."""

    def setUp(self):
        _reset_watchdog()
        reset_states()
        cleanup_worker("default")

    def tearDown(self):
        cleanup_worker("default")

    def test_watchdog_session_cleaned_up_on_cdp_error(self):
        from modules.watchdog.main import _watchdog_registry
        with patch("integration.orchestrator.billing") as mock_billing:
            mock_billing.select_profile.return_value = MagicMock()
            with self.assertRaises(RuntimeError):
                run_payment_step(_make_task())
        self.assertNotIn("default", _watchdog_registry)


class IdempotencyTests(unittest.TestCase):
    """Verify duplicate task_ids are detected and skipped."""

    def setUp(self):
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        _reset_watchdog()
        reset_states()
        cleanup_worker("default")

    def tearDown(self):
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        cleanup_worker("default")

    def test_duplicate_task_id_skipped(self):
        task = _make_task()
        with _idempotency_lock:
            _completed_task_ids[task.task_id] = time.monotonic()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            action, state, total = run_cycle(task)
        self.assertEqual(action, "complete")
        self.assertIsNone(state)
        self.assertIsNone(total)
        mock_billing.select_profile.assert_not_called()

    def test_in_flight_task_id_skipped(self):
        task = _make_task()
        with _idempotency_lock:
            _in_flight_task_ids.add(task.task_id)
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            action, state, total = run_cycle(task)
        self.assertEqual(action, "complete")
        self.assertIsNone(state)
        self.assertIsNone(total)
        mock_billing.select_profile.assert_not_called()

    def test_completed_task_id_recorded(self):
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 99.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            run_cycle(task)
        with _idempotency_lock:
            self.assertIn(task.task_id, _completed_task_ids)


class PersistentIdempotencyStoreTests(unittest.TestCase):
    """Tests for the file-based persistent idempotency store."""

    def setUp(self):
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        _reset_watchdog()
        reset_states()
        cleanup_worker("default")
        # Back up original store path content if it exists
        self._store_backup = None
        if _IDEMPOTENCY_STORE_PATH.exists():
            self._store_backup = _IDEMPOTENCY_STORE_PATH.read_text(encoding="utf-8")

    def tearDown(self):
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        cleanup_worker("default")
        # Restore original store content
        if self._store_backup is not None:
            _IDEMPOTENCY_STORE_PATH.write_text(self._store_backup, encoding="utf-8")
        elif _IDEMPOTENCY_STORE_PATH.exists():
            _IDEMPOTENCY_STORE_PATH.unlink()

    def test_save_load_roundtrip_completed(self):
        """Completed task IDs survive a save → clear → load cycle."""
        task = _make_task()
        with _idempotency_lock:
            _completed_task_ids[task.task_id] = time.monotonic()
            _save_idempotency_store()
        # Simulate restart: clear in-memory state
        with _idempotency_lock:
            _completed_task_ids.clear()
        self.assertNotIn(task.task_id, _completed_task_ids)
        # Load from disk
        _load_idempotency_store()
        self.assertIn(task.task_id, _completed_task_ids)

    def test_save_load_roundtrip_submitted(self):
        """Submitted task IDs survive a save → clear → load cycle."""
        task = _make_task()
        with _idempotency_lock:
            _submitted_task_ids[task.task_id] = time.monotonic()
            _save_idempotency_store()
        # Simulate restart: clear in-memory state
        with _idempotency_lock:
            _submitted_task_ids.clear()
        self.assertNotIn(task.task_id, _submitted_task_ids)
        # Load from disk
        _load_idempotency_store()
        self.assertIn(task.task_id, _submitted_task_ids)

    def test_submitted_task_id_blocks_reexecution(self):
        """A task_id in _submitted_task_ids prevents run_cycle from re-executing."""
        task = _make_task()
        with _idempotency_lock:
            _submitted_task_ids[task.task_id] = time.monotonic()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            action, state, total = run_cycle(task)
        self.assertEqual(action, "complete")
        self.assertIsNone(state)
        self.assertIsNone(total)
        mock_billing.select_profile.assert_not_called()

    def test_clock_skew_future_timestamp_clamped(self):
        """Future wall-clock timestamps (clock skew) are clamped to age=0."""
        import json
        task_id = "clock-skew-test-id"
        future_ts = time.time() + 9999  # Far in the future
        data = {"completed": {task_id: future_ts}, "submitted": {}}
        _IDEMPOTENCY_STORE_PATH.write_text(json.dumps(data), encoding="utf-8")
        _load_idempotency_store()
        # Entry should be loaded (age clamped to 0, which is < TTL)
        self.assertIn(task_id, _completed_task_ids)
        # Its monotonic timestamp should be approximately now (age=0 → mono = now_mono - 0)
        now_mono = time.monotonic()
        self.assertAlmostEqual(_completed_task_ids[task_id], now_mono, delta=2.0)


class CDPDriverCleanupTests(unittest.TestCase):
    """Verify CDP driver is unregistered after run_cycle completes."""

    def setUp(self):
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        _reset_watchdog()
        reset_states()
        cleanup_worker("default")

    def tearDown(self):
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        cleanup_worker("default")

    def test_cdp_unregister_driver_called_on_success(self):
        """CDP driver must be cleaned up after a successful cycle."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 99.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            run_cycle(task)
        mock_cdp.unregister_driver.assert_called_once_with("default")

    def test_cdp_unregister_driver_called_on_error(self):
        """CDP driver must be cleaned up even when the cycle fails."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = RuntimeError("browser crashed")
            with self.assertRaises(RuntimeError):
                run_cycle(task)
        mock_cdp.unregister_driver.assert_called_once_with("default")


class SanitizeErrorTests(unittest.TestCase):
    """Verify _sanitize_error redacts card-like numbers from orchestrator error messages."""

    def test_redacts_card_number(self):
        from integration.orchestrator import _sanitize_error
        exc = RuntimeError("Card 4111111111111111 was declined")
        result = _sanitize_error(exc)
        self.assertNotIn("4111111111111111", result)
        self.assertIn("[REDACTED]", result)

    def test_preserves_non_card_data(self):
        from integration.orchestrator import _sanitize_error
        exc = RuntimeError("Worker timeout after 30 seconds")
        result = _sanitize_error(exc)
        self.assertEqual(result, "Worker timeout after 30 seconds")


class TraceIdPropagationTests(unittest.TestCase):
    """Verify _get_trace_id() returns a value from the runtime."""

    def test_returns_no_trace_when_runtime_not_started(self):
        from integration.orchestrator import _get_trace_id
        # Runtime not started → trace_id is None → returns "no-trace"
        result = _get_trace_id()
        self.assertIsInstance(result, str)

    def test_returns_trace_id_when_runtime_started(self):
        from integration.orchestrator import _get_trace_id
        with patch("integration.runtime.get_trace_id", return_value="abc123def456"):
            result = _get_trace_id()
        self.assertEqual(result, "abc123def456")


class FsmRegistryLeakTests(unittest.TestCase):
    """Verify FSM registry is cleaned up after run_cycle (HIGH-02 / FSM-002)."""

    def setUp(self):
        _reset_watchdog()
        reset_states()
        self._worker_ids = []

    def tearDown(self):
        for worker_id in self._worker_ids:
            cleanup_worker(worker_id)

    def _prepare_workers(self, *worker_ids):
        self._worker_ids.extend(worker_ids)
        for worker_id in worker_ids:
            cleanup_worker(worker_id)

    def test_fsm_registry_cleaned_up_after_run_cycle(self):
        """run_cycle must remove each worker FSM entry before returning."""
        worker_ids = ["worker-101", "worker-102", "worker-103"]
        self._prepare_workers(*worker_ids)
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.rollout"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 1.0
            for wid in worker_ids:
                action, state, total = run_cycle(_make_task(), worker_id=wid)
                self.assertEqual(action, "retry")
                self.assertIsNone(state)
                self.assertEqual(total, 1.0)
        for wid in worker_ids:
            with self.assertRaises(InvalidTransitionError):
                transition_for_worker(wid, "success")

    def test_fsm_registry_cleaned_up_on_exception(self):
        """run_cycle must remove the worker FSM entry on exception paths too."""
        worker_id = "worker-201"
        self._prepare_workers(worker_id)
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.rollout"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            with self.assertRaises(SessionFlaggedError):
                run_cycle(_make_task(), worker_id=worker_id)
        with self.assertRaises(InvalidTransitionError):
            transition_for_worker(worker_id, "success")


if __name__ == "__main__":
    unittest.main()
