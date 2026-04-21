"""L3 integration harness — task-level integration validation (F-09).

Scope
-----
Every test here exercises the **real** orchestrator / CDP / FSM / watchdog
stack against a *stub* GivexDriver and a *mocked* billing layer.  Nothing is
mocked at the orchestrator or CDP-module level, so the tests validate:

  * Full call-order invariants (prefill → mark_submitted → submit).
  * Single idempotency completion per cycle.
  * Watchdog notify-once contract.
  * SessionFlaggedError propagation and driver cleanup on all exit paths.
  * Recovery behaviour across success, decline, VBV/3DS, and timeout scenarios.
  * ``make_task_fn`` lifecycle: BitBrowser create → Selenium build → CDP
    registration → optional run_cycle → unregister on all exits.

Test categories
---------------
  TestL3FullSequenceCallOrder  — prefill → mark_submitted → submit call order.
  TestL3OrchestratorScenarios  — success / decline / VBV-3DS outcomes via
                                  real run_cycle with stub driver.
  TestL3WatchdogTimeout        — watchdog timeout raises SessionFlaggedError.
  TestL3IdempotencyContracts   — duplicate guard, mark_submitted ordering,
                                  single mark_completed per cycle.
  TestL3ErrorInjection         — error at prefill / after mark_submitted
                                  produces correct log messages; driver
                                  unregistered on all error paths.
  TestL3DriverCleanup          — driver always unregistered regardless of
                                  outcome (success, exception, cancellation).
  TestL3TaskFnLifecycle        — make_task_fn wires BitBrowser + Selenium +
                                  CDP registration + run_cycle + unregister.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure the helpers in this directory are importable regardless of how
# unittest discovers this file (with or without __init__.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import modules.cdp.main as _cdp_main  # noqa: E402  pylint: disable=wrong-import-position
from integration.orchestrator import (  # noqa: E402  pylint: disable=wrong-import-position
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _network_listener_lock,
    _notified_workers_this_cycle,
    _submitted_task_ids,
    run_cycle,
    run_payment_step,
)
from modules.common.exceptions import (  # noqa: E402  pylint: disable=wrong-import-position
    InvalidStateError,
    InvalidTransitionError,
    SessionFlaggedError,
)
from modules.fsm.main import (  # noqa: E402  pylint: disable=wrong-import-position
    cleanup_worker,
    get_current_state_for_worker,
    initialize_for_worker,
    reset_registry,
    transition_for_worker,
)
from modules.watchdog.main import reset as _reset_watchdog  # noqa: E402  pylint: disable=wrong-import-position

from integration.worker_task import make_task_fn  # noqa: E402  pylint: disable=wrong-import-position
from _integration_harness import (  # noqa: E402  pylint: disable=wrong-import-position,wrong-import-order
    _IntegrationBase,
    _StubGivexDriver,
    _action_name,
    _make_task,
    make_mock_billing,
)


# ── Shared state-reset helpers ─────────────────────────────────────────────────

def _clear_idempotency() -> None:
    with _idempotency_lock:
        _completed_task_ids.clear()
        _in_flight_task_ids.clear()
        _submitted_task_ids.clear()
    with _network_listener_lock:
        _notified_workers_this_cycle.clear()


# ── F-09 / L3 / Full sequence call order ──────────────────────────────────────

class TestL3FullSequenceCallOrder(_IntegrationBase, unittest.TestCase):
    """run_payment_step must invoke prefill → mark_submitted → submit in order."""

    worker_id = "l3-seq-worker"

    def setUp(self):
        super().setUp()
        reset_registry()
        cleanup_worker(self.worker_id)

    def tearDown(self):
        super().tearDown()

    def _run_payment(self, stub: _StubGivexDriver, task=None, billing_mock=None):
        """Register *stub*, call run_payment_step, return stub."""
        _cdp_main.register_driver(self.worker_id, stub)
        task = task or _make_task()
        billing_mock = billing_mock or make_mock_billing()
        with patch("integration.orchestrator.billing", billing_mock):
            run_payment_step(task, worker_id=self.worker_id)
        return stub

    def test_prefill_called_before_submit(self):
        """preflight_geo_check / navigate / fill must precede submit_purchase."""
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        self._run_payment(stub)
        self.assertIn("preflight_geo_check", stub.calls)
        self.assertIn("submit_purchase", stub.calls)
        self.assertLess(
            stub.calls.index("preflight_geo_check"),
            stub.calls.index("submit_purchase"),
            "preflight_geo_check must precede submit_purchase",
        )

    def test_full_purchase_step_sequence(self):
        """Complete step sequence: preflight→navigate→fill→cart→guest→pay→submit."""
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        self._run_payment(stub)
        expected_order = [
            "preflight_geo_check",
            "navigate_to_egift",
            "fill_egift_form",
            "add_to_cart_and_checkout",
            "select_guest_checkout",
            "fill_payment_and_billing",
            "submit_purchase",
        ]
        for step in expected_order:
            self.assertIn(step, stub.calls, f"Expected '{step}' in call log")
        # Verify ordering
        for i in range(len(expected_order) - 1):
            self.assertLess(
                stub.calls.index(expected_order[i]),
                stub.calls.index(expected_order[i + 1]),
                f"Expected '{expected_order[i]}' before '{expected_order[i + 1]}'",
            )

    def test_mark_submitted_called_between_prefill_and_submit(self):
        """Idempotency checkpoint must be written AFTER prefill and BEFORE submit."""
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        call_order = []
        store_mock = MagicMock()

        def record_submit():
            call_order.append("submit")
            stub.calls.append("submit_purchase")
            try:
                transition_for_worker(self.worker_id, "success")
            except (InvalidStateError, InvalidTransitionError, ValueError):
                pass

        def record_mark(_task_id):
            call_order.append("mark_submitted")

        stub.submit_purchase = record_submit
        store_mock.mark_submitted.side_effect = record_mark
        store_mock.is_duplicate.return_value = False

        _cdp_main.register_driver(self.worker_id, stub)
        task = _make_task()

        with patch(
            "integration.orchestrator.billing", make_mock_billing()
        ), patch(
            "integration.orchestrator._get_idempotency_store", return_value=store_mock
        ), patch(
            "integration.orchestrator.cdp.run_preflight_and_fill",
            side_effect=lambda *_a, **_kw: (call_order.append("prefill"), None)[-1],
        ):
            run_payment_step(task, worker_id=self.worker_id)

        self.assertEqual(
            call_order,
            ["prefill", "mark_submitted", "submit"],
            f"Expected prefill → mark_submitted → submit, got: {call_order}",
        )

    def test_submit_receives_correct_worker_id(self):
        """cdp.submit_purchase must use the correct worker_id for registry lookup."""
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        self._run_payment(stub)
        self.assertIn("submit_purchase", stub.calls)

    def test_single_prefill_call(self):
        """run_preflight_and_fill is called exactly once per payment step."""
        call_counts: dict[str, int] = {}
        stub = _StubGivexDriver(self.worker_id, final_state="success")

        original_methods = [
            "preflight_geo_check", "navigate_to_egift", "fill_egift_form",
            "add_to_cart_and_checkout", "select_guest_checkout",
            "fill_payment_and_billing",
        ]
        for m in original_methods:
            call_counts[m] = 0

        def count_method(name):
            def _counter(*_a, **_kw):
                call_counts[name] += 1
            return _counter

        for m in original_methods:
            setattr(stub, m, count_method(m))

        # submit_purchase must still transition FSM
        def submit():
            stub.calls.append("submit_purchase")
            try:
                transition_for_worker(self.worker_id, "success")
            except (InvalidStateError, InvalidTransitionError, ValueError):
                pass
        stub.submit_purchase = submit

        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            run_payment_step(_make_task(), worker_id=self.worker_id)

        for m in original_methods:
            self.assertEqual(call_counts[m], 1, f"Expected exactly 1 call to '{m}'")


# ── F-09 / L3 / Orchestrator scenarios ────────────────────────────────────────

class TestL3OrchestratorScenarios(_IntegrationBase, unittest.TestCase):
    """run_cycle with stub driver produces correct action for each state."""

    worker_id = "l3-orch-worker"

    def setUp(self):
        super().setUp()
        reset_registry()

    def _run_cycle(self, final_state: str, task=None) -> tuple:
        task = task or _make_task(task_id=f"l3-orch-{final_state}")
        stub = _StubGivexDriver(self.worker_id, final_state=final_state)
        _cdp_main.register_driver(self.worker_id, stub)
        try:
            with patch("integration.orchestrator.billing", make_mock_billing()):
                return run_cycle(task, worker_id=self.worker_id)
        finally:
            _cdp_main.unregister_driver(self.worker_id)
            cleanup_worker(self.worker_id)

    def test_success_state_returns_complete(self):
        """Stub driver in 'success' final state → run_cycle returns ('complete', ...)."""
        action, state, total = self._run_cycle("success")
        self.assertEqual(action, "complete")
        self.assertIsNotNone(state)
        self.assertEqual(state.name, "success")
        self.assertEqual(total, 50.0)

    def test_declined_state_returns_abort_after_retry_exhaustion(self):
        """Stub in 'declined' final state → retry loop exhausts swaps → abort_cycle."""
        action, state, _total = self._run_cycle("declined")
        # With ENABLE_RETRY_LOOP=1 (default), a permanently-declined card exhausts
        # the order_queue swap slots and returns abort_cycle.
        self.assertEqual(_action_name(action), "abort_cycle")
        self.assertIsNotNone(state)
        self.assertEqual(state.name, "declined")

    def test_vbv_3ds_state_returns_await_3ds(self):
        """Stub in 'vbv_3ds' final state → run_cycle returns 'await_3ds'."""
        action, state, _total = self._run_cycle("vbv_3ds")
        self.assertEqual(_action_name(action), "await_3ds")
        self.assertIsNotNone(state)
        self.assertEqual(state.name, "vbv_3ds")

    def test_run_cycle_marks_completed_on_success(self):
        """run_cycle must call mark_completed exactly once on the happy path."""
        task = _make_task(task_id="l3-mark-completed-test")
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub)
        store_mock = MagicMock()
        store_mock.is_duplicate.return_value = False
        try:
            with patch(
                "integration.orchestrator.billing", make_mock_billing()
            ), patch(
                "integration.orchestrator._get_idempotency_store",
                return_value=store_mock,
            ):
                run_cycle(task, worker_id=self.worker_id)
        finally:
            _cdp_main.unregister_driver(self.worker_id)
            cleanup_worker(self.worker_id)
        store_mock.mark_completed.assert_called_once_with(task.task_id)

    def test_single_audit_event_per_cycle(self):
        """Exactly one billing_selection audit event must be logged per cycle."""
        task = _make_task(task_id="l3-audit-event-test")
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub)
        audit_events = []

        def capture_audit(fmt, *args, **_kwargs):
            if args:
                msg = fmt % args
            else:
                msg = fmt
            if "billing_selection" in msg:
                audit_events.append(msg)

        try:
            with patch(
                "integration.orchestrator.billing", make_mock_billing()
            ), patch("integration.orchestrator._AUDIT_LOGGER") as mock_audit:
                mock_audit.info.side_effect = capture_audit
                run_cycle(task, worker_id=self.worker_id)
        finally:
            _cdp_main.unregister_driver(self.worker_id)
            cleanup_worker(self.worker_id)

        self.assertEqual(
            len(audit_events), 1,
            f"Expected exactly 1 billing_selection audit event, got {len(audit_events)}",
        )

    def test_driver_unregistered_after_successful_cycle(self):
        """CDP driver registry must be empty for worker after run_cycle completes."""
        task = _make_task(task_id="l3-unreg-success")
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            run_cycle(task, worker_id=self.worker_id)
        # After run_cycle, driver must have been unregistered
        with self.assertRaises(RuntimeError):
            _cdp_main._get_driver(self.worker_id)  # pylint: disable=protected-access


# ── F-09 / L3 / Watchdog timeout ──────────────────────────────────────────────

class TestL3WatchdogTimeout(_IntegrationBase, unittest.TestCase):
    """Watchdog timeout raises SessionFlaggedError (F-05 contract)."""

    worker_id = "l3-wd-worker"

    def setUp(self):
        super().setUp()
        reset_registry()

    def tearDown(self):
        super().tearDown()

    def test_watchdog_timeout_raises_session_flagged_error(self):
        """When watchdog never receives a total, SessionFlaggedError must be raised."""
        # Stub with dom_total=None → DOM fallback won't notify watchdog.
        stub = _StubGivexDriver(
            self.worker_id,
            final_state="success",
            dom_total=None,
        )
        _cdp_main.register_driver(self.worker_id, stub)
        # Use a very short watchdog timeout so the test doesn't hang.
        with patch(
            "integration.orchestrator.billing", make_mock_billing()
        ), patch("integration.orchestrator._WATCHDOG_TIMEOUT", 0.05):
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(task_id="l3-wd-timeout"), worker_id=self.worker_id)

    def test_watchdog_timeout_logs_after_submission(self):
        """Watchdog timeout AFTER mark_submitted must log 'AFTER payment submission'."""
        # We inject timeout after submit to simulate crash-after-charge scenario.
        stub = _StubGivexDriver(
            self.worker_id,
            final_state="success",
            dom_total=None,
        )
        _cdp_main.register_driver(self.worker_id, stub)
        log_messages = []

        def capture_error(fmt, *args, **_kwargs):
            msg = fmt % args if args else fmt
            log_messages.append(msg)

        store_mock = MagicMock()
        store_mock.is_duplicate.return_value = False

        with patch(
            "integration.orchestrator.billing", make_mock_billing()
        ), patch(
            "integration.orchestrator._WATCHDOG_TIMEOUT", 0.05
        ), patch(
            "integration.orchestrator._logger"
        ) as mock_log, patch(
            "integration.orchestrator._get_idempotency_store",
            return_value=store_mock,
        ):
            mock_log.error.side_effect = capture_error
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(
                    _make_task(task_id="l3-wd-after-submit"),
                    worker_id=self.worker_id,
                )

        after_submit_logs = [m for m in log_messages if "AFTER payment submission" in m]
        self.assertTrue(
            len(after_submit_logs) >= 1,
            f"Expected 'AFTER payment submission' log, got: {log_messages}",
        )

    def test_watchdog_notify_once_per_cycle(self):
        """notify_total must be called at most once per cycle (first-notify-wins)."""
        # Local import: avoids importing notify_total at module scope where it would
        # be patched globally for all tests via the import binding.
        from modules.watchdog.main import notify_total as _notify_total  # pylint: disable=import-outside-toplevel
        notify_calls: list = []
        original_notify = _notify_total

        def counting_notify(wid, value):
            if wid == self.worker_id:
                notify_calls.append(value)
            original_notify(wid, value)

        stub = _StubGivexDriver(self.worker_id, final_state="success", dom_total="50.00")
        _cdp_main.register_driver(self.worker_id, stub)

        with patch(
            "integration.orchestrator.billing", make_mock_billing()
        ), patch("modules.watchdog.main.notify_total", side_effect=counting_notify):
            run_payment_step(_make_task(task_id="l3-wd-once"), worker_id=self.worker_id)

        self.assertLessEqual(
            len(notify_calls), 1,
            f"notify_total must be called at most once; was called {len(notify_calls)} times",
        )


# ── F-09 / L3 / Idempotency contracts ─────────────────────────────────────────

class TestL3IdempotencyContracts(_IntegrationBase, unittest.TestCase):
    """Idempotency invariants: duplicate guard, mark ordering."""

    worker_id = "l3-idem-worker"

    def setUp(self):
        super().setUp()
        reset_registry()

    def tearDown(self):
        super().tearDown()

    def test_duplicate_task_id_skips_billing_and_cdp(self):
        """Second run_cycle call with same task_id must return 'complete' without CDP activity."""
        task = _make_task(task_id="l3-dup-task")
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub)

        store_mock = MagicMock()
        # First call: not duplicate
        store_mock.is_duplicate.return_value = False

        mock_billing = make_mock_billing()

        with patch(
            "integration.orchestrator.billing", mock_billing
        ), patch(
            "integration.orchestrator._get_idempotency_store",
            return_value=store_mock,
        ):
            run_cycle(task, worker_id=self.worker_id)

        # Second call: simulate duplicate
        _cdp_main.register_driver(self.worker_id, stub)  # re-register after cycle
        cleanup_worker(self.worker_id)
        store_mock.is_duplicate.return_value = True
        stub2 = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub2)

        with patch(
            "integration.orchestrator.billing", mock_billing
        ), patch(
            "integration.orchestrator._get_idempotency_store",
            return_value=store_mock,
        ):
            action, _state, _total = run_cycle(task, worker_id=self.worker_id)

        self.assertEqual(action, "complete")
        # stub2 must have no calls (cycle was skipped)
        self.assertEqual(
            stub2.calls, [],
            f"Duplicate cycle must not call driver, got: {stub2.calls}",
        )

    def test_mark_submitted_precedes_submit_purchase(self):
        """mark_submitted must be persisted before submit_purchase is called."""
        task = _make_task(task_id="l3-idem-order")
        call_order: list[str] = []
        store_mock = MagicMock()
        store_mock.is_duplicate.return_value = False
        store_mock.mark_submitted.side_effect = lambda tid: call_order.append("mark_submitted")

        stub = _StubGivexDriver(self.worker_id, final_state="success")
        original_submit = stub.submit_purchase

        def recording_submit():
            call_order.append("submit_purchase")
            original_submit()

        stub.submit_purchase = recording_submit
        _cdp_main.register_driver(self.worker_id, stub)

        with patch(
            "integration.orchestrator.billing", make_mock_billing()
        ), patch(
            "integration.orchestrator._get_idempotency_store",
            return_value=store_mock,
        ):
            run_payment_step(task, worker_id=self.worker_id)

        self.assertIn("mark_submitted", call_order)
        self.assertIn("submit_purchase", call_order)
        self.assertLess(
            call_order.index("mark_submitted"),
            call_order.index("submit_purchase"),
            f"mark_submitted must precede submit_purchase; got: {call_order}",
        )

    def test_in_memory_duplicate_blocked_within_same_process(self):
        """Second run_cycle with same task_id in same process must be blocked."""
        task = _make_task(task_id="l3-in-mem-dup")
        stub = _StubGivexDriver(self.worker_id, final_state="success")

        # First cycle
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            run_cycle(task, worker_id=self.worker_id)

        # Reset worker state but NOT idempotency store (task_id already completed)
        cleanup_worker(self.worker_id)
        stub2 = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub2)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            action, _, _ = run_cycle(task, worker_id=self.worker_id)
        second_calls = list(stub2.calls)

        self.assertEqual(action, "complete", "Duplicate task must return 'complete'")
        self.assertEqual(
            second_calls, [],
            f"Duplicate cycle must not invoke driver methods, got: {second_calls}",
        )


# ── F-09 / L3 / Error injection ───────────────────────────────────────────────

class TestL3ErrorInjection(_IntegrationBase, unittest.TestCase):
    """SessionFlaggedError injected at various points propagates correctly."""

    worker_id = "l3-err-worker"

    def setUp(self):
        super().setUp()
        reset_registry()

    def tearDown(self):
        super().tearDown()

    def test_error_in_preflight_propagates_as_session_flagged(self):
        """SessionFlaggedError raised in preflight_geo_check propagates from run_cycle."""
        stub = _StubGivexDriver(self.worker_id, error_at="preflight_geo_check")
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(task_id="l3-err-preflight"), worker_id=self.worker_id)

    def test_error_after_submit_logs_after_payment_submission(self):
        """Timeout after mark_submitted must log 'AFTER payment submission'."""
        # Simulate watchdog timeout occurring AFTER mark_submitted.
        stub = _StubGivexDriver(
            self.worker_id,
            final_state="success",
            dom_total=None,  # DOM returns None → watchdog not notified
        )
        _cdp_main.register_driver(self.worker_id, stub)
        store_mock = MagicMock()
        store_mock.is_duplicate.return_value = False
        log_messages: list[str] = []

        def capture(fmt, *args, **_kwargs):
            log_messages.append(fmt % args if args else fmt)

        with patch(
            "integration.orchestrator.billing", make_mock_billing()
        ), patch(
            "integration.orchestrator._WATCHDOG_TIMEOUT", 0.05
        ), patch(
            "integration.orchestrator._logger"
        ) as mock_log, patch(
            "integration.orchestrator._get_idempotency_store",
            return_value=store_mock,
        ):
            mock_log.error.side_effect = capture
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(
                    _make_task(task_id="l3-err-after-submit"),
                    worker_id=self.worker_id,
                )

        after_msgs = [m for m in log_messages if "AFTER payment submission" in m]
        self.assertTrue(
            len(after_msgs) >= 1,
            f"Expected 'AFTER payment submission' in logs, got: {log_messages}",
        )

    def test_error_in_navigate_propagates(self):
        """SessionFlaggedError raised in navigate_to_egift propagates."""
        stub = _StubGivexDriver(self.worker_id, error_at="navigate_to_egift")
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(task_id="l3-err-nav"), worker_id=self.worker_id)

    def test_error_in_fill_payment_propagates(self):
        """SessionFlaggedError raised in fill_payment_and_billing propagates."""
        stub = _StubGivexDriver(self.worker_id, error_at="fill_payment_and_billing")
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(task_id="l3-err-fill"), worker_id=self.worker_id)

    def test_session_flagged_error_increments_autoscaler_failure(self):
        """SessionFlaggedError from run_cycle must call autoscaler.record_failure."""
        stub = _StubGivexDriver(self.worker_id, error_at="preflight_geo_check")
        _cdp_main.register_driver(self.worker_id, stub)
        autoscaler_mock = MagicMock()
        with patch(
            "integration.orchestrator.billing", make_mock_billing()
        ), patch(
            "integration.orchestrator._get_autoscaler",
            return_value=autoscaler_mock,
        ):
            with self.assertRaises(SessionFlaggedError):
                run_cycle(_make_task(task_id="l3-err-autoscaler"), worker_id=self.worker_id)
        autoscaler_mock.record_failure.assert_called_once_with(self.worker_id)


# ── F-09 / L3 / Driver cleanup ────────────────────────────────────────────────

class TestL3DriverCleanup(_IntegrationBase, unittest.TestCase):
    """Driver must be unregistered on all exit paths (success, error, exception)."""

    worker_id = "l3-clean-worker"

    def setUp(self):
        super().setUp()
        reset_registry()

    def tearDown(self):
        super().tearDown()

    def _assert_driver_unregistered(self) -> None:
        with self.assertRaises(RuntimeError, msg="Driver must be unregistered after cycle"):
            _cdp_main._get_driver(self.worker_id)  # pylint: disable=protected-access

    def test_driver_unregistered_after_success(self):
        """CDP driver registry cleared after successful run_cycle."""
        task = _make_task(task_id="l3-clean-success")
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            run_cycle(task, worker_id=self.worker_id)
        self._assert_driver_unregistered()

    def test_driver_unregistered_after_session_flagged_error(self):
        """CDP driver registry cleared even when SessionFlaggedError propagates."""
        task = _make_task(task_id="l3-clean-sfx")
        stub = _StubGivexDriver(self.worker_id, error_at="preflight_geo_check")
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            with self.assertRaises(SessionFlaggedError):
                run_cycle(task, worker_id=self.worker_id)
        self._assert_driver_unregistered()

    def test_driver_unregistered_after_generic_exception(self):
        """CDP driver registry cleared even when unexpected RuntimeError propagates."""
        task = _make_task(task_id="l3-clean-runtime")
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        stub.preflight_geo_check = MagicMock(side_effect=RuntimeError("unexpected"))
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            with self.assertRaises(RuntimeError):
                run_cycle(task, worker_id=self.worker_id)
        self._assert_driver_unregistered()

    def test_fsm_cleanup_after_successful_cycle(self):
        """FSM state for worker must be cleaned up after run_cycle."""
        task = _make_task(task_id="l3-clean-fsm")
        stub = _StubGivexDriver(self.worker_id, final_state="success")
        _cdp_main.register_driver(self.worker_id, stub)
        with patch("integration.orchestrator.billing", make_mock_billing()):
            run_cycle(task, worker_id=self.worker_id)
        # FSM cleanup_worker must have been called; re-init should start clean.
        initialize_for_worker(self.worker_id)
        state = get_current_state_for_worker(self.worker_id)
        self.assertIsNone(state, "FSM must start in None state after cleanup + reinit")


# ── F-09 / L3 / make_task_fn lifecycle ────────────────────────────────────────

class TestL3TaskFnLifecycle(unittest.TestCase):
    """make_task_fn wires BitBrowser + Selenium + CDP registration + run_cycle + unregister."""

    worker_id = "l3-taskfn-worker"

    def setUp(self):
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        with _network_listener_lock:
            _notified_workers_this_cycle.discard(self.worker_id)
        _reset_watchdog()
        cleanup_worker(self.worker_id)

    def tearDown(self):
        _cdp_main.unregister_driver(self.worker_id)
        cleanup_worker(self.worker_id)

    def _make_mocks(self):
        """Return (bb_client, selenium_driver, givex_driver) mocks."""
        bb_client = MagicMock()
        bb_client.create_profile.return_value = "bb-profile-1"
        bb_client.launch_profile.return_value = {
            "webdriver": "ws://127.0.0.1:9222/bb-profile-1"
        }
        selenium_drv = MagicMock()
        selenium_drv.execute_cdp_cmd = MagicMock(return_value=None)
        givex_drv = MagicMock()
        return bb_client, selenium_drv, givex_drv

    def test_register_then_unregister_on_success(self):
        """make_task_fn must register the driver and unregister it after completion."""
        bb_client, selenium_drv, givex_drv = self._make_mocks()
        # Single ordered event log to verify register happens before unregister.
        event_log: list[str] = []

        class _TrackingCdp:
            """Stub of modules.cdp.main mirroring its public/protected interface."""

            @staticmethod
            def register_driver(wid, _drv):
                event_log.append(f"register:{wid}")

            @staticmethod
            def unregister_driver(wid):
                event_log.append(f"unregister:{wid}")

            @staticmethod
            def _register_pid(wid, pid):  # pylint: disable=unused-argument
                """No-op stub for cdp._register_pid."""

            @staticmethod
            def register_browser_profile(wid, pid):  # pylint: disable=unused-argument
                """No-op stub for cdp.register_browser_profile."""

        with patch(
            "integration.worker_task.get_bitbrowser_client",
            return_value=bb_client,
        ), patch(
            "integration.worker_task._build_remote_driver",
            return_value=selenium_drv,
        ), patch(
            "modules.cdp.driver.GivexDriver", return_value=givex_drv
        ), patch(
            "integration.worker_task.cdp", _TrackingCdp()
        ), patch("integration.runtime.probe_cdp_listener_support"):
            make_task_fn()(self.worker_id)

        register_key = f"register:{self.worker_id}"
        unregister_key = f"unregister:{self.worker_id}"
        self.assertIn(register_key, event_log, "register_driver must be called")
        self.assertIn(unregister_key, event_log, "unregister_driver must be called")
        self.assertLess(
            event_log.index(register_key),
            event_log.index(unregister_key),
            f"register_driver must be called before unregister_driver; log: {event_log}",
        )

    def test_unregister_called_on_exception_in_run_cycle(self):
        """make_task_fn must unregister driver even when run_cycle raises."""
        bb_client, selenium_drv, givex_drv = self._make_mocks()
        unregister_calls: list[str] = []

        class _TrackingCdp:
            """Stub of modules.cdp.main capturing unregister calls."""

            @staticmethod
            def register_driver(wid, drv):  # pylint: disable=unused-argument
                """No-op stub for cdp.register_driver."""

            @staticmethod
            def unregister_driver(wid):
                unregister_calls.append(wid)

            @staticmethod
            def _register_pid(wid, pid):  # pylint: disable=unused-argument
                """No-op stub for cdp._register_pid."""

            @staticmethod
            def register_browser_profile(wid, pid):  # pylint: disable=unused-argument
                """No-op stub for cdp.register_browser_profile."""

        with patch(
            "integration.worker_task.get_bitbrowser_client",
            return_value=bb_client,
        ), patch(
            "integration.worker_task._build_remote_driver",
            return_value=selenium_drv,
        ), patch(
            "modules.cdp.driver.GivexDriver", return_value=givex_drv
        ), patch(
            "integration.worker_task.cdp", _TrackingCdp()
        ), patch(
            "integration.runtime.probe_cdp_listener_support"
        ), patch(
            "integration.worker_task._get_current_ip_best_effort",
            return_value=None,
        ):
            task_source = MagicMock(return_value=_make_task())
            # Patch run_cycle in the dynamically-imported orchestrator module.
            with patch("integration.orchestrator.run_cycle",
                       side_effect=SessionFlaggedError("boom")):
                with self.assertRaises(SessionFlaggedError):
                    make_task_fn(task_source=task_source)(self.worker_id)

        self.assertIn(self.worker_id, unregister_calls,
                      "unregister_driver must be called even on exception")

    def test_bitbrowser_unavailable_raises_runtime_error(self):
        """make_task_fn raises RuntimeError when BitBrowser client is None."""
        with patch("integration.worker_task.get_bitbrowser_client", return_value=None):
            with self.assertRaises(RuntimeError):
                make_task_fn()(self.worker_id)

    def test_run_cycle_called_with_zip_code(self):
        """make_task_fn forwards MaxMind-resolved zip to run_cycle."""
        bb_client, selenium_drv, givex_drv = self._make_mocks()
        run_cycle_kwargs: list[dict] = []

        def capture_run_cycle(_task, zip_code=None, worker_id="default", ctx=None, **_kwargs):
            run_cycle_kwargs.append({"zip_code": zip_code, "worker_id": worker_id})
            return "complete", None, None

        with patch(
            "integration.worker_task.get_bitbrowser_client",
            return_value=bb_client,
        ), patch(
            "integration.worker_task._build_remote_driver",
            return_value=selenium_drv,
        ), patch(
            "modules.cdp.driver.GivexDriver", return_value=givex_drv
        ), patch(
            "integration.worker_task.cdp"
        ), patch(
            "integration.runtime.probe_cdp_listener_support"
        ), patch(
            "integration.worker_task._get_current_ip_best_effort",
            return_value="1.2.3.4",
        ), patch(
            "integration.worker_task.maxmind_lookup_zip", return_value="10001"
        ), patch(
            "integration.orchestrator.run_cycle",
            side_effect=capture_run_cycle,
        ):
            task_source = MagicMock(return_value=_make_task())
            make_task_fn(task_source=task_source)(self.worker_id)

        self.assertTrue(
            len(run_cycle_kwargs) == 1,
            f"run_cycle must be called exactly once, got {len(run_cycle_kwargs)}",
        )
        self.assertEqual(run_cycle_kwargs[0]["zip_code"], "10001")
        self.assertEqual(run_cycle_kwargs[0]["worker_id"], self.worker_id)

    def test_bitbrowser_lifecycle_order(self):
        """BitBrowser lifecycle must be: create_profile → launch_profile → (close + delete)."""
        bb_client, selenium_drv, givex_drv = self._make_mocks()
        with patch(
            "integration.worker_task.get_bitbrowser_client",
            return_value=bb_client,
        ), patch(
            "integration.worker_task._build_remote_driver",
            return_value=selenium_drv,
        ), patch(
            "modules.cdp.driver.GivexDriver", return_value=givex_drv
        ), patch(
            "integration.worker_task.cdp"
        ), patch("integration.runtime.probe_cdp_listener_support"):
            make_task_fn()(self.worker_id)

        bb_client.create_profile.assert_called_once()
        bb_client.launch_profile.assert_called_once()
        # close/delete are called on context-manager exit
        bb_client.close_profile.assert_called()
        bb_client.delete_profile.assert_called()

        # Verify creation precedes launch by checking call order in mock_calls.
        method_calls = [str(c) for c in bb_client.mock_calls]
        create_pos = next(
            (i for i, m in enumerate(method_calls) if "create_profile" in m), -1
        )
        launch_pos = next(
            (i for i, m in enumerate(method_calls) if "launch_profile" in m), -1
        )
        self.assertLess(
            create_pos, launch_pos,
            f"create_profile must precede launch_profile; calls: {method_calls}",
        )


if __name__ == "__main__":
    unittest.main()
