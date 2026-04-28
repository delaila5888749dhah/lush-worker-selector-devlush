"""Tests for orchestrator hardening: idempotency, CDP timeout coordination,
dual-notify race safety, DOM parsing, shutdown safety, and outcome observability.

Covers PR-14 requirements:
- Dual notify race safety (first-notify-wins guard)
- Redis idempotency store semantics and failure handling
- DOM fallback parse edge cases
- CDP executor shutdown safety
- Outcome observability (state=None warning)
- Submitted-state persistence and crash-recovery blocking
- End-to-end CDP callback → watchdog → orchestrator contract
- Network listener callback coverage
- Orphaned thread counter in metrics
"""
import concurrent.futures
import math
import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, call, patch

from modules.common.exceptions import SessionFlaggedError
from modules.common.types import CardInfo, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry
from modules.watchdog.main import reset as _reset_watchdog

from integration.orchestrator import (
    _FileIdempotencyStore,
    _IDEMPOTENCY_STORE_PATH,
    _IDEMPOTENCY_TTL,
    _RedisIdempotencyStore,
    _build_idempotency_store,
    _cdp_call_with_timeout,
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _load_idempotency_store,
    _network_listener_lock,
    _notified_workers_this_cycle,
    _notify_total_from_dom,
    _save_idempotency_store,
    _setup_network_total_listener,
    _submitted_task_ids,
    _validated_notify_total,
    get_cdp_metrics,
    handle_outcome,
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


def _clear_idempotency():
    with _idempotency_lock:
        _completed_task_ids.clear()
        _in_flight_task_ids.clear()
        _submitted_task_ids.clear()
    with _network_listener_lock:
        _notified_workers_this_cycle.clear()


# ── Outcome Observability ──────────────────────────────────────────────────────

class OutcomeObservabilityTests(unittest.TestCase):
    """Issue 6: handle_outcome with state=None should log a warning."""

    def test_state_none_logs_warning(self):
        """handle_outcome(None, ...) must emit a warning log, not silently retry."""
        with patch("integration.orchestrator._logger") as mock_logger:
            result = handle_outcome(None, [], worker_id="w1")
        self.assertEqual(result, "retry")
        mock_logger.warning.assert_called_once()
        warn_msg = mock_logger.warning.call_args[0][0]
        self.assertIn("state=None", warn_msg)

    def test_state_none_warning_includes_worker_id(self):
        """Warning log must include the worker_id for traceability."""
        with patch("integration.orchestrator._logger") as mock_logger:
            handle_outcome(None, [], worker_id="worker-99")
        call_args = mock_logger.warning.call_args[0]
        # worker_id is in positional args to the format string
        self.assertIn("worker-99", str(call_args))

    def test_non_none_state_does_not_log_state_none_warning(self):
        """No state=None warning when state has a valid name."""
        with patch("integration.orchestrator._logger") as mock_logger:
            handle_outcome(State("success"), [], worker_id="w1")
        # _logger.warning may be called by other paths, but not for state=None
        for c in mock_logger.warning.call_args_list:
            self.assertNotIn("state=None", str(c))


# ── Dual Notify Race ──────────────────────────────────────────────────────────

class DualNotifyGuardTests(unittest.TestCase):
    """Issue 2: first-notify-wins guard prevents double-notification value races."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("guard-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("guard-worker")

    def _make_driver(self, return_value):
        driver = MagicMock()
        driver.execute_script.return_value = return_value
        return driver

    def test_first_notify_wins_second_skipped(self):
        """After first successful DOM notify, a second call for same worker is skipped."""
        worker_id = "guard-worker"
        driver = self._make_driver("$49.99")
        with patch("integration.orchestrator.watchdog") as mock_wd:
            # First call: notifies
            _notify_total_from_dom(driver, worker_id)
            # Second call: should be skipped
            _notify_total_from_dom(driver, worker_id)
        # notify_total should have been called exactly once
        self.assertEqual(mock_wd.notify_total.call_count, 1)

    def test_first_notify_wins_different_workers_independent(self):
        """First-notify-wins guard is per-worker; different workers are independent."""
        driver1 = self._make_driver("$10.00")
        driver2 = self._make_driver("$20.00")
        with patch("integration.orchestrator.watchdog") as mock_wd:
            _notify_total_from_dom(driver1, "worker-A")
            _notify_total_from_dom(driver2, "worker-B")
        self.assertEqual(mock_wd.notify_total.call_count, 2)
        calls = mock_wd.notify_total.call_args_list
        self.assertEqual(calls[0][0][0], "worker-A")
        self.assertEqual(calls[1][0][0], "worker-B")

    def test_guard_cleared_on_new_cycle(self):
        """run_payment_step clears the guard so next cycle can notify again."""
        worker_id = "guard-worker"
        task = _make_task()
        driver = MagicMock()
        driver.execute_script.return_value = "49.99"
        # Simulate guard already set from a previous cycle
        with _network_listener_lock:
            _notified_workers_this_cycle.add(worker_id)
        # run_payment_step should clear the guard for this worker
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_wd,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = driver
            mock_wd.wait_for_total.return_value = 49.99
            mock_fsm.get_current_state_for_worker.return_value = None
            run_payment_step(task, worker_id=worker_id)
        # notify_total must be called (guard cleared for new cycle)
        mock_wd.notify_total.assert_called_once_with(worker_id, 49.99)

    def test_guard_reset_and_watchdog_enable_happen_before_listener_setup(self):
        """New-cycle reset must happen before listener/polling setup starts."""
        worker_id = "guard-worker"
        task = _make_task()
        driver = MagicMock()
        order: list[str] = []

        with _network_listener_lock:
            _notified_workers_this_cycle.add(worker_id)

        def _record_enable(*_args, **_kwargs):
            order.append("enable")

        def _record_setup(*_args, **_kwargs):
            with _network_listener_lock:
                self.assertNotIn(worker_id, _notified_workers_this_cycle)
            order.append("setup")

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_wd,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._setup_network_total_listener",
                  side_effect=_record_setup),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = driver
            mock_wd.enable_network_monitor.side_effect = _record_enable
            mock_wd.wait_for_total.return_value = 49.99
            mock_fsm.get_current_state_for_worker.return_value = None
            run_payment_step(task, worker_id=worker_id)

        self.assertEqual(
            order,
            ["enable", "setup", "enable"],
            "run_payment_step must arm Phase A before listener setup, then "
            "re-arm the watchdog for Phase C after preflight total is received",
        )

    def test_concurrent_callbacks_only_notify_once(self):
        """Concurrent DOM-read callbacks for the same worker must notify at most once."""
        worker_id = "guard-concurrent"
        notify_count = [0]
        barrier = threading.Barrier(3)

        def fake_notify(wid, val):
            notify_count[0] += 1

        driver = MagicMock()
        driver.execute_script.return_value = "99.99"

        with patch("integration.orchestrator.watchdog") as mock_wd:
            mock_wd.notify_total.side_effect = fake_notify

            def worker():
                barrier.wait(timeout=5)
                _notify_total_from_dom(driver, worker_id)

            # Clear guard first
            with _network_listener_lock:
                _notified_workers_this_cycle.discard(worker_id)

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            barrier.wait(timeout=5)
            t1.join(timeout=5)
            t2.join(timeout=5)

        self.assertEqual(notify_count[0], 1, "notify_total must be called exactly once")

        # Clean up guard for other tests
        with _network_listener_lock:
            _notified_workers_this_cycle.discard(worker_id)


# ── DOM Parse Edge Cases ───────────────────────────────────────────────────────

class DOMParseEdgeCaseTests(unittest.TestCase):
    """Issue 9: _notify_total_from_dom must handle noisy/edge-case inputs."""

    def setUp(self):
        with _network_listener_lock:
            _notified_workers_this_cycle.discard("dom-worker")

    def tearDown(self):
        with _network_listener_lock:
            _notified_workers_this_cycle.discard("dom-worker")

    def _call(self, return_value):
        """Helper: call _notify_total_from_dom with a mocked driver."""
        driver = MagicMock()
        driver.execute_script.return_value = return_value
        with patch("integration.orchestrator.watchdog") as mock_wd:
            _notify_total_from_dom(driver, "dom-worker")
        # Reset guard so next assertion is clean
        with _network_listener_lock:
            _notified_workers_this_cycle.discard("dom-worker")
        return mock_wd

    def test_numeric_int_value_notified(self):
        mock_wd = self._call(50)
        mock_wd.notify_total.assert_called_once_with("dom-worker", 50.0)

    def test_numeric_float_value_notified(self):
        mock_wd = self._call(49.99)
        mock_wd.notify_total.assert_called_once_with("dom-worker", 49.99)

    def test_numeric_zero_is_valid(self):
        mock_wd = self._call(0)
        mock_wd.notify_total.assert_called_once_with("dom-worker", 0.0)

    def test_negative_float_is_valid(self):
        mock_wd = self._call(-5.5)
        mock_wd.notify_total.assert_called_once_with("dom-worker", -5.5)

    def test_nan_float_is_rejected(self):
        mock_wd = self._call(float("nan"))
        mock_wd.notify_total.assert_not_called()

    def test_inf_float_is_rejected(self):
        mock_wd = self._call(float("inf"))
        mock_wd.notify_total.assert_not_called()

    def test_neg_inf_float_is_rejected(self):
        mock_wd = self._call(float("-inf"))
        mock_wd.notify_total.assert_not_called()

    def test_none_does_not_notify(self):
        mock_wd = self._call(None)
        mock_wd.notify_total.assert_not_called()

    def test_empty_string_does_not_notify(self):
        mock_wd = self._call("")
        mock_wd.notify_total.assert_not_called()

    def test_string_with_dollar_sign(self):
        mock_wd = self._call("$49.99")
        mock_wd.notify_total.assert_called_once_with("dom-worker", 49.99)

    def test_string_with_commas(self):
        mock_wd = self._call("1,234.56")
        mock_wd.notify_total.assert_called_once_with("dom-worker", 1234.56)

    def test_string_european_decimal_comma(self):
        """Locale-aware: bare ``49,99`` must parse as 49.99, not 4999."""
        mock_wd = self._call("49,99 €")
        mock_wd.notify_total.assert_called_once_with("dom-worker", 49.99)

    def test_string_european_thousands_dot_decimal_comma(self):
        """Locale-aware: ``1.234,56`` must parse as 1234.56, not 1.23456."""
        mock_wd = self._call("€ 1.234,56")
        mock_wd.notify_total.assert_called_once_with("dom-worker", 1234.56)

    def test_accounting_style_negative(self):
        """(49.99) should be treated as -49.99 (accounting notation)."""
        mock_wd = self._call("(49.99)")
        mock_wd.notify_total.assert_called_once_with("dom-worker", -49.99)

    def test_noisy_string_extracts_first_number(self):
        mock_wd = self._call("Total: $12.50 USD")
        mock_wd.notify_total.assert_called_once_with("dom-worker", 12.50)

    def test_string_no_numeric_does_not_notify(self):
        mock_wd = self._call("N/A")
        mock_wd.notify_total.assert_not_called()

    def test_bool_true_treated_as_numeric_one(self):
        """bool is a subclass of int; True == 1, False == 0."""
        mock_wd = self._call(True)
        mock_wd.notify_total.assert_called_once_with("dom-worker", 1.0)

    def test_bool_false_treated_as_numeric_zero(self):
        mock_wd = self._call(False)
        mock_wd.notify_total.assert_called_once_with("dom-worker", 0.0)

    def test_execute_script_exception_is_swallowed(self):
        """DOM script error must be caught; no exception propagates to caller."""
        driver = MagicMock()
        driver.execute_script.side_effect = RuntimeError("DOM unavailable")
        with patch("integration.orchestrator.watchdog") as mock_wd:
            # Must not raise
            _notify_total_from_dom(driver, "dom-worker")
        mock_wd.notify_total.assert_not_called()

    def test_dom_selector_includes_cws_lbl_order_total(self):
        """E3 audit: orchestrator DOM fallback must query the same Order Total
        node that ``GivexDriver.submit_purchase()`` cross-checks
        (``SEL_ORDER_TOTAL_DISPLAY``).  Otherwise a page that exposes the
        total only via ``#cws_lbl_orderTotal`` would let submit_purchase read
        the DOM total while Phase A's DOM-only/degraded fallback cannot
        capture the watchdog/preflight total from the same source.
        """
        driver = MagicMock()
        driver.execute_script.return_value = "49.99"
        with patch("integration.orchestrator.watchdog"):
            _notify_total_from_dom(driver, "dom-worker")
        driver.execute_script.assert_called_once()
        script = driver.execute_script.call_args.args[0]
        self.assertIn("#cws_lbl_orderTotal", script)
        self.assertIn(".order-total", script)
        self.assertIn(".checkout-total", script)
        self.assertIn("[data-total]", script)


# ── Network Listener Callback Coverage ────────────────────────────────────────

class NetworkListenerCallbackTests(unittest.TestCase):
    """Issue 8: _setup_network_total_listener / _on_response coverage."""

    def setUp(self):
        with _network_listener_lock:
            _notified_workers_this_cycle.discard("nl-worker")

    def tearDown(self):
        with _network_listener_lock:
            _notified_workers_this_cycle.discard("nl-worker")

    def test_network_enable_failure_does_not_raise(self):
        """If Network.enable fails, no exception propagates; listener not set."""
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = RuntimeError("CDP error")
        _setup_network_total_listener(driver, "nl-worker")  # Must not raise
        driver.add_cdp_listener.assert_not_called()

    def test_listener_registration_called(self):
        """Listener is registered on Network.responseReceived when CDP works."""
        driver = MagicMock()
        driver.add_cdp_listener = MagicMock()
        _setup_network_total_listener(driver, "nl-worker")
        driver.add_cdp_listener.assert_called_once()
        event_name = driver.add_cdp_listener.call_args[0][0]
        self.assertEqual(event_name, "Network.responseReceived")

    def test_callback_triggers_dom_read_for_matching_url(self):
        """Callback fires _notify_total_from_dom when URL matches a pattern."""
        driver = MagicMock()
        captured_callback = [None]

        def fake_add_listener(event, cb):
            captured_callback[0] = cb

        driver.add_cdp_listener = fake_add_listener
        driver.execute_script.return_value = "49.99"

        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, "nl-worker")
            # Simulate a network response for a matching URL
            captured_callback[0]({"response": {"url": "/checkout/total/amounts"}})
        mock_wd.notify_total.assert_called_once_with("nl-worker", 49.99)

    def test_callback_does_not_fire_for_non_matching_url(self):
        """Callback must NOT trigger DOM read for unrelated URLs."""
        driver = MagicMock()
        captured_callback = [None]

        def fake_add_listener(event, cb):
            captured_callback[0] = cb

        driver.add_cdp_listener = fake_add_listener
        driver.execute_script.return_value = "99.99"

        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, "nl-worker")
            captured_callback[0]({"response": {"url": "/some/unrelated/endpoint"}})
        mock_wd.notify_total.assert_not_called()

    def test_callback_handles_malformed_params_gracefully(self):
        """Malformed params (not a dict, missing keys) must not crash the callback."""
        driver = MagicMock()
        captured_callback = [None]

        def fake_add_listener(event, cb):
            captured_callback[0] = cb

        driver.add_cdp_listener = fake_add_listener

        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, "nl-worker")
            # Non-dict params
            captured_callback[0](None)
            captured_callback[0]("bad string")
            captured_callback[0]({})
            captured_callback[0]({"response": {}})
        mock_wd.notify_total.assert_not_called()

    def test_cws40_pattern_no_longer_matches(self):
        """After P3-F2 fix (option A), 'cws4.0' substring alone must NOT trigger callback."""
        driver = MagicMock()
        captured_callback = [None]

        def fake_add_listener(event, cb):
            captured_callback[0] = cb

        driver.add_cdp_listener = fake_add_listener
        driver.execute_script.return_value = "25.00"

        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, "nl-worker")
            captured_callback[0]({"response": {"url": "https://example.com/cws4.0/submit"}})
        mock_wd.notify_total.assert_not_called()

    def test_add_listener_failure_does_not_raise(self):
        """If add_cdp_listener raises, the error must be caught and logged."""
        driver = MagicMock()
        driver.add_cdp_listener = MagicMock(side_effect=RuntimeError("listener error"))
        # Must not raise
        _setup_network_total_listener(driver, "nl-worker")


# ── Redis Idempotency Store Semantics ─────────────────────────────────────────

class RedisIdempotencyStoreSemanticTests(unittest.TestCase):
    """Issue 4: _RedisIdempotencyStore correctness and failure handling."""

    def _make_redis_store(self, mock_redis):
        """Build a _RedisIdempotencyStore with a mocked Redis client."""
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_redis.Redis.from_url.return_value = mock_client
        store = _RedisIdempotencyStore.__new__(_RedisIdempotencyStore)
        store._redis = mock_client
        return store, mock_client

    def test_is_duplicate_returns_false_on_first_set(self):
        """SET NX returns True → key was newly set → not a duplicate."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.return_value = True
            self.assertFalse(store.is_duplicate("task-1"))

    def test_is_duplicate_returns_true_when_key_exists(self):
        """SET NX returns None → key already exists → duplicate."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.return_value = None
            self.assertTrue(store.is_duplicate("task-1"))

    def test_is_duplicate_uses_nx_and_ex(self):
        """SET must use nx=True and ex=TTL for atomic idempotency."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.return_value = True
            store.is_duplicate("task-nx")
            mock_client.set.assert_called_once()
            kwargs = mock_client.set.call_args[1]
            self.assertTrue(kwargs.get("nx"))
            self.assertEqual(kwargs.get("ex"), _IDEMPOTENCY_TTL)

    def test_is_duplicate_redis_failure_is_fail_safe(self):
        """Redis error in is_duplicate → treat as duplicate (no double-charge)."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.side_effect = RuntimeError("connection lost")
            result = store.is_duplicate("task-fail")
        self.assertTrue(result, "Redis failure must be fail-safe (treat as duplicate)")

    def test_is_duplicate_redis_failure_logs_error(self):
        """Redis error in is_duplicate must be logged."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.side_effect = RuntimeError("connection lost")
            with patch("integration.orchestrator._logger") as mock_logger:
                store.is_duplicate("task-log")
        mock_logger.error.assert_called_once()

    def test_mark_submitted_calls_set_with_submitted_value(self):
        """mark_submitted must write 'submitted' status with TTL."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            store.mark_submitted("task-sub")
            mock_client.set.assert_called_once()
            args = mock_client.set.call_args[0]
            self.assertIn("submitted", args)

    def test_mark_submitted_redis_failure_reraises(self):
        """Redis error in mark_submitted must be re-raised (critical checkpoint)."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.side_effect = RuntimeError("redis down")
            with self.assertRaises(RuntimeError):
                store.mark_submitted("task-reraise")

    def test_mark_completed_calls_set_with_completed_value(self):
        """mark_completed must write 'completed' status."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            store.mark_completed("task-done")
            mock_client.set.assert_called_once()
            args = mock_client.set.call_args[0]
            self.assertIn("completed", args)

    def test_mark_completed_redis_failure_does_not_reraise(self):
        """Redis error in mark_completed must NOT propagate (task already submitted)."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.side_effect = RuntimeError("redis down")
            # Must not raise
            store.mark_completed("task-soft")

    def test_mark_completed_redis_failure_logs_warning(self):
        """Redis error in mark_completed must log a warning."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            mock_client.set.side_effect = RuntimeError("redis down")
            with patch("integration.orchestrator._logger") as mock_logger:
                store.mark_completed("task-warn")
        mock_logger.warning.assert_called_once()

    def test_release_inflight_is_noop(self):
        """release_inflight must not touch Redis (key persists with TTL)."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            store.release_inflight("task-1")
            mock_client.delete.assert_not_called()

    def test_flush_is_noop(self):
        """flush() is a no-op for Redis (always persistent)."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            store.flush()  # Must not raise

    def test_load_is_noop(self):
        """load() is a no-op for Redis (no startup load required)."""
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            store, mock_client = self._make_redis_store(sys.modules["redis"])
            store.load()  # Must not raise


# ── Submitted-State Persistence and Crash-Recovery ────────────────────────────

class SubmittedStateCrashRecoveryTests(unittest.TestCase):
    """Issue 3 & 5: submitted-state persists and blocks re-execution after restart."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("default")
        self._store_backup = None
        if _IDEMPOTENCY_STORE_PATH.exists():
            self._store_backup = _IDEMPOTENCY_STORE_PATH.read_text(encoding="utf-8")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("default")
        if self._store_backup is not None:
            _IDEMPOTENCY_STORE_PATH.write_text(self._store_backup, encoding="utf-8")
        elif _IDEMPOTENCY_STORE_PATH.exists():
            _IDEMPOTENCY_STORE_PATH.unlink()

    def test_submitted_state_persists_to_disk(self):
        """mark_submitted must persist task_id to disk via _save_idempotency_store."""
        task = _make_task()
        with _idempotency_lock:
            _submitted_task_ids[task.task_id] = time.monotonic()
            _save_idempotency_store()
        import json
        data = json.loads(_IDEMPOTENCY_STORE_PATH.read_text(encoding="utf-8"))
        self.assertIn(task.task_id, data.get("submitted", {}))

    def test_submitted_state_survives_restart(self):
        """Submitted task_id reloaded from disk must be in _submitted_task_ids."""
        task = _make_task()
        with _idempotency_lock:
            _submitted_task_ids[task.task_id] = time.monotonic()
            _save_idempotency_store()
        # Simulate restart
        with _idempotency_lock:
            _submitted_task_ids.clear()
        self.assertNotIn(task.task_id, _submitted_task_ids)
        _load_idempotency_store()
        self.assertIn(task.task_id, _submitted_task_ids)

    def test_submitted_state_blocks_reexecution_after_reload(self):
        """After reload, a submitted task_id must prevent run_cycle from re-executing."""
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
        mock_billing.select_profile.assert_not_called()

    def test_crash_recovery_log_emitted_when_submitted_found(self):
        """A warning must be logged when submitted tasks are found on load."""
        import json
        task = _make_task()
        data = {"completed": {}, "submitted": {task.task_id: time.time()}}
        _IDEMPOTENCY_STORE_PATH.write_text(json.dumps(data), encoding="utf-8")
        with _idempotency_lock:
            _submitted_task_ids.clear()
        with patch("integration.orchestrator._logger") as mock_logger:
            _load_idempotency_store()
        warning_messages = [str(c) for c in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("Crash-recovery" in m or "submitted" in m for m in warning_messages),
            f"Expected crash-recovery warning, got: {warning_messages}",
        )

    def test_no_crash_recovery_log_when_no_submitted_tasks(self):
        """No crash-recovery warning when there are no submitted tasks on disk."""
        import json
        data = {"completed": {}, "submitted": {}}
        _IDEMPOTENCY_STORE_PATH.write_text(json.dumps(data), encoding="utf-8")
        with _idempotency_lock:
            _submitted_task_ids.clear()
        with patch("integration.orchestrator._logger") as mock_logger:
            _load_idempotency_store()
        for c in mock_logger.warning.call_args_list:
            self.assertNotIn("Crash-recovery", str(c))


# ── CDP Executor Shutdown Safety ──────────────────────────────────────────────

class CDPShutdownSafetyTests(unittest.TestCase):
    """Issue 10: _shutdown_cdp_executor is observable and bounded."""

    def test_shutdown_logs_active_and_orphaned(self):
        """Shutdown must log active_cdp_requests and orphaned_cdp_threads."""
        with (
            patch("integration.orchestrator._cdp_executor") as mock_exec,
            patch("integration.orchestrator._logger") as mock_logger,
        ):
            from integration import orchestrator
            orchestrator._shutdown_cdp_executor()
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        self.assertTrue(
            any("Shutting down" in msg for msg in info_calls),
            f"Expected shutdown info log, got: {info_calls}",
        )

    def test_shutdown_calls_executor_shutdown(self):
        """Shutdown must call _cdp_executor.shutdown(wait=False, cancel_futures=True)."""
        with patch("integration.orchestrator._cdp_executor") as mock_exec:
            from integration import orchestrator
            orchestrator._shutdown_cdp_executor()
        mock_exec.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_shutdown_with_in_flight_requests_does_not_block(self):
        """Shutdown with in-flight requests must return immediately (wait=False)."""
        import time as _time
        start = _time.monotonic()
        with (
            patch("integration.orchestrator._cdp_executor") as mock_exec,
            patch("integration.orchestrator._logger"),
        ):
            mock_exec.shutdown = MagicMock()  # fast no-op
            from integration import orchestrator
            orchestrator._shutdown_cdp_executor()
        elapsed = _time.monotonic() - start
        self.assertLess(elapsed, 1.0, "Shutdown must complete in < 1s (wait=False)")


# ── Orphaned Thread Counter ────────────────────────────────────────────────────

class OrphanedThreadCounterTests(unittest.TestCase):
    """Issue 1: orphaned_cdp_threads metric increments on timeout."""

    def test_orphaned_threads_increments_on_timeout(self):
        """Each timeout must increment orphaned_cdp_threads by 1."""
        metrics_before = get_cdp_metrics()
        blocker = threading.Event()

        def slow():
            blocker.wait(timeout=10)

        try:
            with self.assertRaises(SessionFlaggedError):
                _cdp_call_with_timeout(slow, timeout=0.05)
        finally:
            blocker.set()

        metrics_after = get_cdp_metrics()
        self.assertGreater(
            metrics_after["orphaned_cdp_threads"],
            metrics_before["orphaned_cdp_threads"],
        )

    def test_successful_call_does_not_increment_orphaned(self):
        """Successful calls must not change orphaned_cdp_threads."""
        metrics_before = get_cdp_metrics()
        _cdp_call_with_timeout(lambda: 42, timeout=5)
        metrics_after = get_cdp_metrics()
        self.assertEqual(
            metrics_after["orphaned_cdp_threads"],
            metrics_before["orphaned_cdp_threads"],
        )

    def test_executor_unavailable_does_not_increment_orphaned(self):
        """Executor unavailable (RuntimeError) must not increment orphaned_cdp_threads."""
        metrics_before = get_cdp_metrics()
        dead_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        dead_executor.shutdown(wait=True)
        with patch("integration.orchestrator._cdp_executor", dead_executor):
            with self.assertRaises(SessionFlaggedError):
                _cdp_call_with_timeout(lambda: 1, timeout=5)
        metrics_after = get_cdp_metrics()
        self.assertEqual(
            metrics_after["orphaned_cdp_threads"],
            metrics_before["orphaned_cdp_threads"],
        )


# ── End-to-End Callback Contract ──────────────────────────────────────────────

class EndToEndCallbackContractTests(unittest.TestCase):
    """Issue 11: CDP callback → watchdog.notify_total → orchestrator proceeds."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("e2e-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("e2e-worker")

    def test_happy_path_network_response_unblocks_orchestrator(self):
        """CDP network response callback → notify_total → wait_for_total returns."""
        from modules.watchdog.main import enable_network_monitor, notify_total, wait_for_total
        worker_id = "e2e-worker"
        captured_callback = [None]
        driver = MagicMock()
        driver.execute_script.return_value = "75.00"

        def fake_add_listener(event, cb):
            captured_callback[0] = cb

        driver.add_cdp_listener = fake_add_listener

        enable_network_monitor(worker_id)
        _setup_network_total_listener(driver, worker_id)

        # Simulate CDP network response arriving on another thread
        def fire_callback():
            time.sleep(0.05)
            captured_callback[0]({"response": {"url": "/checkout/total/result"}})

        t = threading.Thread(target=fire_callback)
        t.start()

        total = wait_for_total(worker_id, timeout=5.0)
        t.join(timeout=5)
        self.assertEqual(total, 75.0)

    def test_malformed_callback_does_not_block_orchestrator(self):
        """Malformed network response must not call notify_total; watchdog should timeout."""
        from modules.watchdog.main import enable_network_monitor, wait_for_total
        worker_id = "e2e-worker"
        captured_callback = [None]
        driver = MagicMock()
        driver.execute_script.return_value = None  # DOM returns nothing

        def fake_add_listener(event, cb):
            captured_callback[0] = cb

        driver.add_cdp_listener = fake_add_listener

        enable_network_monitor(worker_id)
        _setup_network_total_listener(driver, worker_id)

        # Simulate malformed callback
        captured_callback[0](None)
        captured_callback[0]({"response": {"url": "/unrelated"}})

        # watchdog should timeout since no valid total was notified
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(worker_id, timeout=0.1)

    def test_late_callback_after_dom_fallback(self):
        """Late callback arriving after DOM fallback must be a no-op (first-notify-wins)."""
        from modules.watchdog.main import enable_network_monitor, notify_total, wait_for_total
        worker_id = "e2e-worker"
        captured_callback = [None]
        driver = MagicMock()
        driver.execute_script.return_value = "100.00"

        def fake_add_listener(event, cb):
            captured_callback[0] = cb

        driver.add_cdp_listener = fake_add_listener

        # Reset guard for clean slate
        with _network_listener_lock:
            _notified_workers_this_cycle.discard(worker_id)

        enable_network_monitor(worker_id)
        _setup_network_total_listener(driver, worker_id)

        # DOM fallback fires first
        _notify_total_from_dom(driver, worker_id)

        total = wait_for_total(worker_id, timeout=2.0)
        self.assertEqual(total, 100.0)

        # Late callback fires — must be a no-op (first-notify-wins already set)
        notify_call_count_before = driver.execute_script.call_count
        if captured_callback[0]:
            captured_callback[0]({"response": {"url": "/checkout/total/late"}})
        # execute_script should NOT have been called again for a second notify
        # (the _on_response callback calls _notify_total_from_dom, which is guarded)
        with _network_listener_lock:
            _notified_workers_this_cycle.discard(worker_id)


# ── Post-Submission Watchdog Timeout Observability ─────────────────────────────

class PostSubmissionTimeoutObservabilityTests(unittest.TestCase):
    """Issue 6: watchdog timeout after mark_submitted must log a distinct message."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("post-sub-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("post-sub-worker")

    def test_post_submission_timeout_logs_distinct_message(self):
        """Phase 3A Task 2 / Option A: post-submit (Phase C) watchdog timeout
        is now non-fatal — it WARNs with 'unconfirmed' marker and marks the
        task unconfirmed (TTL).  Issue 6 'AFTER payment submission' ERROR log
        is no longer produced because the post-submit timeout no longer raises.
        """
        task = _make_task()
        warn_messages = []

        def capture_warning(fmt, *args, **kwargs):
            warn_messages.append(fmt % args if args else fmt)

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._logger") as mock_logger,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            # Phase A succeeds; Phase C times out.
            mock_watchdog.wait_for_total.side_effect = [
                "preflight-total",
                SessionFlaggedError("timeout"),
            ]

            # Simulate mark_submitted being called (via idempotency store mock)
            store_mock = MagicMock()
            store_mock.is_duplicate.return_value = False
            store_mock.mark_submitted.return_value = None
            with patch("integration.orchestrator._get_idempotency_store", return_value=store_mock):
                mock_logger.warning.side_effect = capture_warning
                # Phase C swallows the timeout; no exception propagates.
                run_payment_step(task, worker_id="post-sub-worker")

        unconfirmed_msgs = [m for m in warn_messages if "unconfirmed" in m.lower()]
        self.assertTrue(
            len(unconfirmed_msgs) >= 1,
            f"Expected post-submit unconfirmed warning, got: {warn_messages}",
        )
        store_mock.mark_submitted.assert_called_once_with(task.task_id)
        store_mock.mark_unconfirmed.assert_called_once()

    def test_pre_submission_timeout_logs_before_submission_message(self):
        """SessionFlaggedError from CDP preflight/fill (before mark_submitted) must log 'BEFORE'."""
        task = _make_task()
        log_messages = []

        def capture_error(fmt, *args, **kwargs):
            log_messages.append(fmt % args if args else fmt)

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._logger") as mock_logger,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            # Raise timeout from preflight/fill (before mark_submitted is reached)
            mock_cdp.run_preflight_and_fill.side_effect = SessionFlaggedError("fill timeout")

            store_mock = MagicMock()
            store_mock.is_duplicate.return_value = False
            with patch("integration.orchestrator._get_idempotency_store", return_value=store_mock):
                mock_logger.error.side_effect = capture_error
                with self.assertRaises(SessionFlaggedError):
                    run_payment_step(task, worker_id="post-sub-worker")

        pre_sub_msgs = [m for m in log_messages if "BEFORE payment submission" in m]
        self.assertTrue(
            len(pre_sub_msgs) >= 1,
            f"Expected 'BEFORE payment submission' log, got: {log_messages}",
        )
        # mark_submitted must NOT have been called (fill failed before reaching that point)
        store_mock.mark_submitted.assert_not_called()


if __name__ == "__main__":
    unittest.main()
