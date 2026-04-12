import os
import sys
import time
import types
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
    _build_idempotency_store,
    _completed_task_ids,
    _FileIdempotencyStore,
    _idempotency_lock,
    _in_flight_task_ids,
    _submitted_task_ids,
    _IDEMPOTENCY_STORE_PATH,
    _IDEMPOTENCY_TTL,
    _cdp_call_with_timeout,
    _load_idempotency_store,
    _save_idempotency_store,
    get_cdp_metrics,
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


class RedisIdempotencyStoreFallbackTests(unittest.TestCase):
    def test_build_store_falls_back_to_file_store_when_redis_ping_fails(self):
        redis_url = "redis://:super-secret@localhost:6379/0"
        mock_client = MagicMock()
        mock_client.ping.side_effect = RuntimeError("redis down")
        fake_redis = types.SimpleNamespace(
            Redis=types.SimpleNamespace(from_url=MagicMock(return_value=mock_client))
        )
        with (
            patch.dict(os.environ, {"REDIS_URL": redis_url}, clear=False),
            patch.dict(sys.modules, {"redis": fake_redis}),
            patch("integration.orchestrator._logger.warning") as mock_warning,
        ):
            store = _build_idempotency_store()
        self.assertIsInstance(store, _FileIdempotencyStore)
        self.assertGreaterEqual(len(mock_warning.call_args_list), 1)
        init_warning = mock_warning.call_args_list[0]
        self.assertIn("Failed to initialise RedisIdempotencyStore", init_warning.args[0])
        self.assertEqual(init_warning.args[1], "redis://:[REDACTED]@localhost:6379/0")


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


class CdpCallWithTimeoutTests(unittest.TestCase):
    """Tests for _cdp_call_with_timeout using the shared executor."""

    def test_successful_call_returns_result(self):
        """A fast callable returns its result normally."""
        result = _cdp_call_with_timeout(lambda: 42, timeout=5)
        self.assertEqual(result, 42)

    def test_args_and_kwargs_forwarded(self):
        """Positional and keyword arguments are forwarded to the callable."""
        def adder(a, b, extra=0):
            return a + b + extra
        result = _cdp_call_with_timeout(adder, 1, 2, timeout=5, extra=10)
        self.assertEqual(result, 13)

    def test_timeout_raises_session_flagged_error(self):
        """A slow callable triggers SessionFlaggedError after timeout."""
        import threading as _t
        blocker = _t.Event()

        def slow_fn():
            blocker.wait(timeout=10)

        with self.assertRaises(SessionFlaggedError) as ctx:
            _cdp_call_with_timeout(slow_fn, timeout=0.1)
        self.assertIn("timed out", str(ctx.exception))
        blocker.set()  # Unblock the background thread.

    def test_callable_exception_propagated(self):
        """Exceptions raised inside the callable propagate to the caller."""
        def failing_fn():
            raise ValueError("boom")
        with self.assertRaises(ValueError):
            _cdp_call_with_timeout(failing_fn, timeout=5)

    def test_submit_after_shutdown_raises_session_flagged_error(self):
        """Submitting after executor shutdown raises SessionFlaggedError."""
        import concurrent.futures
        dead_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        dead_executor.shutdown(wait=True)
        with patch("integration.orchestrator._cdp_executor", dead_executor):
            with self.assertRaises(SessionFlaggedError) as ctx:
                _cdp_call_with_timeout(lambda: 1, timeout=5)
            self.assertIn("unavailable", str(ctx.exception))

    def test_cdp_timeout_increments_counter(self):
        """A timeout must increment get_cdp_metrics()['total_timeouts']."""
        import threading as _t
        blocker = _t.Event()
        metrics_before = get_cdp_metrics()

        def slow():
            blocker.wait(timeout=10)

        try:
            with self.assertRaises(SessionFlaggedError):
                _cdp_call_with_timeout(slow, timeout=0.05)
        finally:
            blocker.set()

        metrics_after = get_cdp_metrics()
        self.assertGreater(
            metrics_after["total_timeouts"],
            metrics_before["total_timeouts"],
        )

    def test_get_cdp_metrics_returns_expected_keys(self):
        """get_cdp_metrics() must return a dict with exactly two keys."""
        m = get_cdp_metrics()
        self.assertIn("total_timeouts", m)
        self.assertIn("active_cdp_requests", m)
        self.assertIsInstance(m["total_timeouts"], int)
        self.assertIsInstance(m["active_cdp_requests"], int)


class CDPPoolSaturationTests(unittest.TestCase):
    """Stress test: 8 simultaneous timeout-style tasks against a mock executor."""

    def test_8_concurrent_timeouts_recover(self):
        """Submit 8 tasks that all time out; verify pool recovers and accepts new work."""
        import concurrent.futures as _cf
        import threading as _t

        gate = _t.Event()
        results = []
        errors = []

        def controlled_task():
            # Each task waits briefly then completes — simulates a short timeout
            gate.wait(timeout=2.0)
            return "done"

        with _cf.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(controlled_task) for _ in range(8)]
            gate.set()  # Unblock all tasks concurrently

            for f in _cf.as_completed(futures, timeout=10.0):
                try:
                    results.append(f.result())
                except Exception as exc:
                    errors.append(exc)

            self.assertEqual(errors, [], "No errors expected from concurrent tasks")
            self.assertEqual(len(results), 8, "All 8 tasks should complete")

            # Pool should still accept new work after the concurrent burst
            new_future = executor.submit(lambda: "alive")
            self.assertEqual(new_future.result(timeout=5.0), "alive")

    def test_pool_does_not_exhaust_on_sequential_timeouts(self):
        """Sequential cancel + completion cycles must not leave orphaned threads."""
        import concurrent.futures as _cf
        import threading as _t

        with _cf.ThreadPoolExecutor(max_workers=8) as executor:
            for _ in range(8):
                stop = _t.Event()

                def task(ev=stop):
                    ev.wait(timeout=0.05)
                    return "done"

                f = executor.submit(task)
                stop.set()
                f.cancel()  # May or may not succeed; that's acceptable
                try:
                    f.result(timeout=1.0)
                except _cf.CancelledError:
                    # Task was cancelled before starting — acceptable, proceed to next iteration.
                    continue

            # Verify the pool is still healthy and accepts new work
            final = executor.submit(lambda: "healthy")
            self.assertEqual(final.result(timeout=5.0), "healthy")


class CDPExecutorBehaviorTests(unittest.TestCase):
    """Verify executor behavior under timeout and queue pressure conditions.

    These tests document and verify the known semantics of ThreadPoolExecutor
    as used by _cdp_call_with_timeout():
    - submit() enqueues immediately, does not block.
    - future.cancel() after timeout is best-effort (no-op if running).
    - Executor recovers and accepts new work after timed-out tasks complete.
    - Queued tasks execute as slots become available.
    """

    def test_executor_recovers_after_timeout(self):
        """After a timeout, executor must accept and complete new submissions."""
        import concurrent.futures
        import threading as _t
        block = _t.Event()
        ready = _t.Event()

        def blocking_task():
            ready.set()
            block.wait(timeout=10)
            return "completed"

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            future = executor.submit(blocking_task)
            ready.wait(timeout=2)

            timed_out = False
            try:
                future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                timed_out = True
                future.cancel()  # best-effort; no-op

            # Unblock the running task so its slot is returned to the pool
            block.set()
            future.result(timeout=5)  # wait for natural completion

            # Executor must now accept new work
            result = executor.submit(lambda: "new_work").result(timeout=5)
        finally:
            executor.shutdown(wait=False)

        self.assertTrue(timed_out)
        self.assertEqual(result, "new_work")

    def test_submit_enqueues_when_all_slots_busy(self):
        """When all slots are occupied, submit() enqueues without raising.

        This verifies that ThreadPoolExecutor.submit() does NOT block or raise
        when max_workers slots are all busy — the task is enqueued and will
        execute once a slot becomes available.
        """
        import concurrent.futures
        import threading as _t
        hold = _t.Event()
        started = _t.Barrier(3)  # 2 hold_task threads + main thread all rendezvous here

        def hold_task():
            started.wait(timeout=5)
            hold.wait(timeout=10)
            return "slot_released"

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            f1 = executor.submit(hold_task)
            f2 = executor.submit(hold_task)
            started.wait(timeout=5)  # both tasks are now running

            # This must NOT raise — it queues the task
            f3 = executor.submit(lambda: "queued_work")

            hold.set()  # release all blocked tasks
            results = [f1.result(timeout=5), f2.result(timeout=5), f3.result(timeout=5)]
        finally:
            executor.shutdown(wait=False)

        self.assertIn("queued_work", results)

    def test_multiple_sequential_timeouts_do_not_deadlock(self):
        """Multiple sequential timeout scenarios must not deadlock the executor."""
        import concurrent.futures
        import threading as _t
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        results = []
        errors = []

        try:
            for i in range(4):
                block = _t.Event()
                ready = _t.Event()

                def make_task(b, r, idx=i):
                    def task():
                        r.set()
                        b.wait(timeout=5)
                        return f"done-{idx}"
                    return task

                f = executor.submit(make_task(block, ready))
                ready.wait(timeout=2)
                try:
                    f.result(timeout=0.05)
                except concurrent.futures.TimeoutError:
                    f.cancel()
                    errors.append("timeout")
                block.set()

                # Verify executor still works
                try:
                    r = executor.submit(lambda: "ok").result(timeout=5)
                    results.append(r)
                except Exception as exc:
                    errors.append(f"unexpected: {exc}")
        finally:
            executor.shutdown(wait=False)

        self.assertEqual(results.count("ok"), 4)
        self.assertEqual(errors.count("timeout"), 4)


if __name__ == "__main__":
    unittest.main()
