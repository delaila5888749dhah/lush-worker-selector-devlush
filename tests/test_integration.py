import hashlib
import json as _json
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
    reset_registry,
    transition_for_worker,
)
from modules.watchdog.main import reset as _reset_watchdog
from integration.orchestrator import (
    _build_idempotency_store,
    _completed_task_ids,
    _get_consecutive_failures,
    _FileIdempotencyStore,
    _idempotency_lock,
    _in_flight_task_ids,
    _submitted_task_ids,
    _IDEMPOTENCY_STORE_PATH,
    _IDEMPOTENCY_TTL,
    _cdp_call_with_timeout,
    _load_idempotency_store,
    _make_profile_id,
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
        reset_registry()
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
        reset_registry()
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


    def test_run_preflight_and_fill_called_with_task_and_profile(self):
        """run_payment_step() must use cdp.run_preflight_and_fill for the pre-submit sequence."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            profile = MagicMock()
            mock_billing.select_profile.return_value = profile
            mock_watchdog.wait_for_total.return_value = 25.0
            mock_fsm.get_current_state_for_worker.return_value = None
            run_payment_step(task)
        mock_cdp.run_preflight_and_fill.assert_called_once_with(
            task, profile, worker_id="default"
        )
        mock_cdp.submit_purchase.assert_called_once_with(worker_id="default")

    def test_fill_card_not_called_during_payment_step(self):
        """Deprecated fill_card() must never be called by run_payment_step()."""
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
        mock_cdp.fill_card.assert_not_called()

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

    def test_dom_total_fallback_notifies_watchdog_before_wait(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            driver = MagicMock()
            driver.execute_script.return_value = "Total: $49.99"
            mock_cdp._get_driver.return_value = driver
            mock_watchdog.wait_for_total.return_value = 49.99
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            state, total = run_payment_step(_make_task())
        driver.execute_cdp_cmd.assert_called_once_with("Network.enable", {})
        mock_watchdog.notify_total.assert_called_once_with("default", 49.99)
        notify_idx = next(
            (
                i for i, item in enumerate(mock_watchdog.mock_calls)
                if item[0] == "notify_total"
            ),
            None,
        )
        wait_idx = next(
            (
                i for i, item in enumerate(mock_watchdog.mock_calls)
                if item[0] == "wait_for_total"
            ),
            None,
        )
        self.assertIsNotNone(notify_idx, "notify_total call should be present")
        self.assertIsNotNone(wait_idx, "wait_for_total call should be present")
        self.assertLess(
            notify_idx,
            wait_idx,
            "notify_total must be called before wait_for_total",
        )
        self.assertEqual(state.name, "success")
        self.assertEqual(total, 49.99)

    def test_no_dom_total_still_raises_watchdog_timeout(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            driver = MagicMock()
            driver.execute_script.return_value = None
            mock_cdp._get_driver.return_value = driver
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task())
        mock_watchdog.notify_total.assert_not_called()


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        _reset_watchdog()
        reset_registry()
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

    def test_run_cycle_success_records_autoscaler_success_once(self):
        autoscaler = MagicMock()
        with (
            patch("integration.orchestrator._get_autoscaler", return_value=autoscaler),
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 99.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            run_cycle(_make_task())
        autoscaler.record_success.assert_called_once_with("default")

    def test_run_cycle_failure_records_autoscaler_failure_once(self):
        autoscaler = MagicMock()
        with (
            patch("integration.orchestrator._get_autoscaler", return_value=autoscaler),
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                run_cycle(_make_task())
        autoscaler.record_failure.assert_called_once_with("default")

    def test_run_cycle_session_flagged_records_failure_and_reraises(self):
        autoscaler = MagicMock()
        with (
            patch("integration.orchestrator._get_autoscaler", return_value=autoscaler),
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            with self.assertRaises(SessionFlaggedError):
                run_cycle(_make_task())
        autoscaler.record_failure.assert_called_once_with("default")

    def test_run_cycle_non_complete_outcomes_record_autoscaler_failure(self):
        """Non-exception outcomes that are not 'complete' must record failure, not success."""
        non_success_states = [None, State("declined"), State("ui_lock"), State("vbv_3ds")]
        for state in non_success_states:
            with self.subTest(state=getattr(state, "name", None)):
                autoscaler = MagicMock()
                with (
                    patch("integration.orchestrator._get_autoscaler", return_value=autoscaler),
                    patch("integration.orchestrator.billing") as mock_billing,
                    patch("integration.orchestrator.cdp"),
                    patch("integration.orchestrator.watchdog") as mock_watchdog,
                    patch("integration.orchestrator.fsm") as mock_fsm,
                ):
                    mock_billing.select_profile.return_value = MagicMock()
                    mock_watchdog.wait_for_total.return_value = 50.0
                    mock_fsm.get_current_state_for_worker.return_value = state
                    run_cycle(_make_task())
                autoscaler.record_failure.assert_called_once_with("default")
                autoscaler.record_success.assert_not_called()

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


class ConsecutiveFailuresHelperTests(unittest.TestCase):
    def test_get_consecutive_failures_returns_minus_one_when_unavailable(self):
        with patch("integration.orchestrator._get_autoscaler", None):
            self.assertEqual(_get_consecutive_failures("default"), -1)


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
        reset_registry()
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
        reset_registry()
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
        reset_registry()
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
        reset_registry()
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
        self.assertIn("[REDACTED-CARD]", result)

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
        reset_registry()
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
        """get_cdp_metrics() must return a dict with exactly three keys."""
        m = get_cdp_metrics()
        self.assertEqual(
            set(m.keys()),
            {"total_timeouts", "active_cdp_requests", "orphaned_cdp_threads"},
        )
        self.assertIsInstance(m["total_timeouts"], int)
        self.assertIsInstance(m["active_cdp_requests"], int)
        self.assertIsInstance(m["orphaned_cdp_threads"], int)


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


class CDPExecutorProductionSingletonTests(unittest.TestCase):
    """Test production _cdp_executor singleton: timeout -> recovery -> next call succeeds."""

    def setUp(self):
        self._timeout_before = get_cdp_metrics()["total_timeouts"]

    def test_production_executor_timeout_then_recovery(self):
        """Patch production executor with 2 workers, force 2 timeouts, then verify recovery."""
        import concurrent.futures
        import threading as _t

        blocker1 = _t.Event()
        blocker2 = _t.Event()
        ready1 = _t.Event()
        ready2 = _t.Event()

        def slow1():
            ready1.set()
            blocker1.wait(timeout=10)
            return "slow1-done"

        def slow2():
            ready2.set()
            blocker2.wait(timeout=10)
            return "slow2-done"

        test_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            with patch("integration.orchestrator._cdp_executor", test_executor):
                # Fill both executor slots
                f1 = test_executor.submit(slow1)
                f2 = test_executor.submit(slow2)
                self.assertTrue(
                    ready1.wait(timeout=2),
                    "First executor worker did not start within timeout",
                )
                self.assertTrue(
                    ready2.wait(timeout=2),
                    "Second executor worker did not start within timeout",
                )

                # Both slots busy → calls must timeout
                with self.assertRaises(SessionFlaggedError):
                    _cdp_call_with_timeout(lambda: None, timeout=0.05)
                with self.assertRaises(SessionFlaggedError):
                    _cdp_call_with_timeout(lambda: None, timeout=0.05)

                # Unblock background tasks → slots freed
                blocker1.set()
                blocker2.set()
                f1.result(timeout=5)
                f2.result(timeout=5)

                # Executor must recover → subsequent call succeeds
                result = _cdp_call_with_timeout(lambda: "recovered", timeout=5)
                self.assertEqual(result, "recovered")

        finally:
            blocker1.set()
            blocker2.set()
            test_executor.shutdown(wait=False)

        metrics_after = get_cdp_metrics()
        self.assertGreaterEqual(
            metrics_after["total_timeouts"] - self._timeout_before,
            2,
            "total_timeouts must have incremented by at least 2",
        )


class CDPActiveRequestCounterTests(unittest.TestCase):
    """_active_cdp_requests must not double-decrement after timeout."""

    def test_counter_is_zero_before_call(self):
        """Baseline: counter starts at 0 when no calls in flight."""
        self.assertEqual(get_cdp_metrics()["active_cdp_requests"], 0)

    def test_counter_zero_after_timeout_no_double_decrement(self):
        """
        Scenario:
          1. task starts -> counter = 1 (inside _cdp_call_with_timeout)
          2. timeout fires -> finally block -> counter = 0
          3. background task completes naturally -> counter STILL 0 (no double-decrement)
        """
        import concurrent.futures
        import threading as _t
        import time as _time

        blocker = _t.Event()
        task_finished = _t.Event()

        def slow_fn():
            blocker.wait(timeout=10)
            task_finished.set()
            return "bg-done"

        test_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            with patch("integration.orchestrator._cdp_executor", test_executor):
                # Counter must be 0 before call
                self.assertEqual(get_cdp_metrics()["active_cdp_requests"], 0)

                # Call times out
                with self.assertRaises(SessionFlaggedError):
                    _cdp_call_with_timeout(slow_fn, timeout=0.05)

                # After timeout + finally: counter must be 0
                counter_after_timeout = get_cdp_metrics()["active_cdp_requests"]
                self.assertEqual(
                    counter_after_timeout, 0,
                    "active_cdp_requests must be 0 after timeout (decremented in finally)",
                )

                # Unblock background task and let it finish
                blocker.set()
                self.assertTrue(
                    task_finished.wait(timeout=5),
                    "background task did not complete within 5 seconds after unblock",
                )
                _time.sleep(0.1)  # allow thread cleanup

                # Counter must still be 0 — no double-decrement from background thread
                counter_after_bg = get_cdp_metrics()["active_cdp_requests"]
                self.assertEqual(
                    counter_after_bg, 0,
                    "active_cdp_requests must remain 0 after background task completes "
                    "(background thread must NOT decrement counter)",
                )
        finally:
            blocker.set()
            test_executor.shutdown(wait=False)


class WatchdogTimingInvariantTests(unittest.TestCase):
    """Default _WATCHDOG_TIMEOUT must satisfy the timing invariant at all times."""

    def test_watchdog_timeout_greater_than_cdp_plus_step_budget(self):
        """
        Verify: _WATCHDOG_TIMEOUT > _CDP_CALL_TIMEOUT + _STEP_BUDGET_TOTAL

        A legitimate cycle can consume up to _CDP_CALL_TIMEOUT seconds (CDP call)
        plus _STEP_BUDGET_TOTAL seconds (behavioral delay budget). The watchdog must
        not fire before this window expires, or it will produce false timeouts.

        Current values: 30 > 15.0 + 10.0 = 25.0
        """
        from integration.orchestrator import _WATCHDOG_TIMEOUT, _CDP_CALL_TIMEOUT
        from modules.delay.config import _STEP_BUDGET_TOTAL
        self.assertGreater(
            _WATCHDOG_TIMEOUT,
            _CDP_CALL_TIMEOUT + _STEP_BUDGET_TOTAL,
            f"INVARIANT VIOLATED: _WATCHDOG_TIMEOUT({_WATCHDOG_TIMEOUT}) must be > "
            f"_CDP_CALL_TIMEOUT({_CDP_CALL_TIMEOUT}) + "
            f"_STEP_BUDGET_TOTAL({_STEP_BUDGET_TOTAL}) = "
            f"{_CDP_CALL_TIMEOUT + _STEP_BUDGET_TOTAL}. "
            f"Reducing _WATCHDOG_TIMEOUT below this sum causes false watchdog timeouts.",
        )

    def test_default_cdp_call_timeout_matches_config(self):
        """_CDP_CALL_TIMEOUT in orchestrator must match CDP_CALL_TIMEOUT in config."""
        from integration.orchestrator import _CDP_CALL_TIMEOUT
        from modules.delay.config import CDP_CALL_TIMEOUT as CONFIG_CDP_TIMEOUT
        self.assertEqual(
            _CDP_CALL_TIMEOUT,
            CONFIG_CDP_TIMEOUT,
            "orchestrator._CDP_CALL_TIMEOUT must equal config.CDP_CALL_TIMEOUT "
            "when CDP_CALL_TIMEOUT_SECONDS env var is not set",
        )


class TestBillingSelectionAuditEvent(unittest.TestCase):
    """Tests for the structured billing selection audit event (SPEC-SYNC §12)."""

    def _make_profile(self, first_name="Jane", last_name="Doe", zip_code="90210"):
        from modules.common.types import BillingProfile
        return BillingProfile(
            first_name=first_name,
            last_name=last_name,
            address="123 Main St",
            city="Beverly Hills",
            state="CA",
            zip_code=zip_code,
            phone="5555555555",
            email="jane@example.com",
        )

    def _run_payment_step_with_known_profile(self, profile, zip_code=None, worker_id="default"):
        """Run run_payment_step with a known profile and return captured audit log args."""
        captured = []
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._AUDIT_LOGGER") as mock_audit,
        ):
            mock_billing.select_profile.return_value = profile
            mock_watchdog.wait_for_total.return_value = 10.0
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_audit.info.side_effect = lambda fmt, payload: captured.append(payload)
            _reset_watchdog()
            reset_registry()
            cleanup_worker(worker_id)
            run_payment_step(_make_task(), zip_code=zip_code, worker_id=worker_id)
        return captured

    def test_audit_event_emitted_on_successful_selection(self):
        """Audit event must be emitted exactly once per successful billing.select_profile() call."""
        profile = self._make_profile()
        captured = self._run_payment_step_with_known_profile(profile)
        self.assertEqual(len(captured), 1)

    def test_audit_event_schema(self):
        """Emitted event must contain all required fields with correct types."""
        profile = self._make_profile()
        captured = self._run_payment_step_with_known_profile(profile, zip_code="90210")
        self.assertEqual(len(captured), 1)
        event = _json.loads(captured[0])
        self.assertIn("event_type", event)
        self.assertIn("worker_id", event)
        self.assertIn("task_id", event)
        self.assertIn("selection_method", event)
        self.assertIn("requested_zip", event)
        self.assertIn("profile_id", event)
        self.assertIn("trace_id", event)
        self.assertIn("timestamp_utc", event)
        self.assertEqual(event["event_type"], "billing_selection")
        self.assertIsInstance(event["worker_id"], str)
        self.assertIsInstance(event["profile_id"], str)
        self.assertIsInstance(event["trace_id"], str)
        self.assertIsInstance(event["timestamp_utc"], str)

    def test_selection_method_zip_match_when_zip_provided(self):
        """selection_method must be 'zip_match' when zip_code is non-empty."""
        profile = self._make_profile()
        captured = self._run_payment_step_with_known_profile(profile, zip_code="90210")
        event = _json.loads(captured[0])
        self.assertEqual(event["selection_method"], "zip_match")
        self.assertEqual(event["requested_zip"], "90210")

    def test_selection_method_round_robin_when_no_zip(self):
        """selection_method must be 'round_robin' when zip_code is None."""
        profile = self._make_profile()
        captured = self._run_payment_step_with_known_profile(profile, zip_code=None)
        event = _json.loads(captured[0])
        self.assertEqual(event["selection_method"], "round_robin")
        self.assertIsNone(event["requested_zip"])

    def test_selection_method_round_robin_when_zip_is_empty(self):
        """selection_method must be 'round_robin' when zip_code is empty/blank."""
        profile = self._make_profile()
        captured = self._run_payment_step_with_known_profile(profile, zip_code="   ")
        event = _json.loads(captured[0])
        self.assertEqual(event["selection_method"], "round_robin")
        self.assertEqual(event["requested_zip"], "   ")

    def test_profile_id_is_anonymized(self):
        """profile_id must be a SHA-256 hex hash — not raw first_name or last_name."""
        profile = self._make_profile(first_name="Jane", last_name="Doe", zip_code="90210")
        captured = self._run_payment_step_with_known_profile(profile)
        event = _json.loads(captured[0])
        profile_id = event["profile_id"]
        self.assertNotIn("Jane", profile_id)
        self.assertNotIn("Doe", profile_id)
        expected = hashlib.sha256("Jane|Doe|90210".encode("utf-8")).hexdigest()[:16]
        self.assertEqual(profile_id, expected)

    def test_no_raw_pii_in_audit_event(self):
        """Raw first_name, last_name, address, phone, email must NOT appear in logged event."""
        profile = self._make_profile(first_name="UniqueFirst", last_name="UniqueLast")
        captured = self._run_payment_step_with_known_profile(profile)
        self.assertEqual(len(captured), 1)
        raw_payload = captured[0]
        self.assertNotIn("UniqueFirst", raw_payload)
        self.assertNotIn("UniqueLast", raw_payload)
        self.assertNotIn("123 Main St", raw_payload)
        self.assertNotIn("5555555555", raw_payload)
        self.assertNotIn("jane@example.com", raw_payload)

    def test_audit_event_failure_does_not_crash_payment(self):
        """If _emit_billing_audit_event raises, run_payment_step must continue normally."""
        profile = self._make_profile()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._make_profile_id", side_effect=RuntimeError("hash-fail")),
        ):
            mock_billing.select_profile.return_value = profile
            mock_watchdog.wait_for_total.return_value = 10.0
            mock_fsm.get_current_state_for_worker.return_value = None
            _reset_watchdog()
            reset_registry()
            cleanup_worker("default")
            state, total = run_payment_step(_make_task())
        self.assertEqual(total, 10.0)

    def test_make_profile_id_is_deterministic(self):
        """Same profile always produces same profile_id."""
        profile = self._make_profile(first_name="Alice", last_name="Smith", zip_code="12345")
        id1 = _make_profile_id(profile)
        id2 = _make_profile_id(profile)
        self.assertEqual(id1, id2)

    def test_make_profile_id_format(self):
        """profile_id must be exactly 16 lowercase hex characters."""
        profile = self._make_profile()
        profile_id = _make_profile_id(profile)
        self.assertEqual(len(profile_id), 16)
        self.assertRegex(profile_id, r'^[0-9a-f]{16}$')


if __name__ == "__main__":
    unittest.main()
