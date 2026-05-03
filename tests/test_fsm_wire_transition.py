"""Tests for P0-1: FSM transition wired into production run_payment_step.

Verifies:
  - After submit_purchase(), wait_for_post_submit_outcome() is called.
  - The detected page state is immediately transitioned in the FSM via
    transition_for_worker() (primary path, all 4 FSM states).
  - InvalidTransitionError is caught, logged as warning, and does NOT crash.
  - Fallback path: if get_current_state_for_worker() returns None (primary
    wait_for_post_submit_outcome failed), a second outcome + transition attempt is made.
  - Fallback succeeds for all 4 FSM states.
  - Fallback InvalidTransitionError is caught, logged as warning, no crash.
"""

import unittest
from unittest.mock import MagicMock, patch

from modules.common.exceptions import InvalidTransitionError
from modules.common.types import CardInfo, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry
from modules.watchdog.main import reset as _reset_watchdog
from integration.orchestrator import run_payment_step


def _make_task() -> WorkerTask:
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
        order_queue=(),
    )


class TestFSMPrimaryTransitionPath(unittest.TestCase):
    """Primary path: wait_for_post_submit_outcome + transition_for_worker after submit."""

    def setUp(self):
        _reset_watchdog()
        reset_registry()
        cleanup_worker("default")

    def tearDown(self):
        cleanup_worker("default")

    def _run(self, page_state: str):
        """Run run_payment_step with the given page state and return (state, total)."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.return_value = page_state
            mock_fsm.transition_for_worker.return_value = State(page_state)
            mock_fsm.get_current_state_for_worker.return_value = State(page_state)
            mock_watchdog.wait_for_total.return_value = 49.99
            result = run_payment_step(task)
            # Capture mock references for assertions
            self._mock_cdp = mock_cdp
            self._mock_fsm = mock_fsm
        return result

    def test_success_state_detect_then_transition(self):
        """Mock DOM /confirmation → FSM state becomes success."""
        state, _total = self._run("success")
        self.assertEqual(state.name, "success")
        self._mock_cdp._get_driver.assert_called_with("default")
        self._mock_fsm.transition_for_worker.assert_called_with("default", "success")

    def test_declined_state_detect_then_transition(self):
        """Mock DOM declined → FSM state becomes declined."""
        state, _total = self._run("declined")
        self.assertEqual(state.name, "declined")
        self._mock_cdp._get_driver.assert_called_with("default")
        self._mock_fsm.transition_for_worker.assert_called_with("default", "declined")

    def test_vbv_3ds_state_detect_then_transition(self):
        """Mock DOM iframe 3dsecure → FSM state becomes vbv_3ds."""
        state, _total = self._run("vbv_3ds")
        self.assertEqual(state.name, "vbv_3ds")
        self._mock_cdp._get_driver.assert_called_with("default")
        self._mock_fsm.transition_for_worker.assert_called_with("default", "vbv_3ds")

    def test_ui_lock_state_detect_then_transition(self):
        """Mock DOM spinner → FSM state becomes ui_lock."""
        state, _total = self._run("ui_lock")
        self.assertEqual(state.name, "ui_lock")
        self._mock_cdp._get_driver.assert_called_with("default")
        self._mock_fsm.transition_for_worker.assert_called_with("default", "ui_lock")

    def test_wait_for_post_submit_outcome_called_after_submit_purchase(self):
        """wait_for_post_submit_outcome must be called AFTER submit_purchase in call order."""
        task = _make_task()
        call_order = []
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp.submit_purchase.side_effect = lambda **kw: call_order.append("submit")
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.side_effect = lambda: (
                call_order.append("detect"), "success"
            )[1]
            mock_fsm.transition_for_worker.return_value = State("success")
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            mock_watchdog.wait_for_total.return_value = 49.99
            run_payment_step(task)
        self.assertEqual(call_order, ["submit", "detect"])

    def test_invalid_transition_error_does_not_crash(self):
        """InvalidTransitionError after submit must be logged and not propagate."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.return_value = "success"
            mock_fsm.transition_for_worker.side_effect = InvalidTransitionError("bad")
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_watchdog.wait_for_total.return_value = 49.99
            # Must not raise
            state, total = run_payment_step(task)
        self.assertIsNone(state)
        self.assertEqual(total, 49.99)

    def test_wait_for_post_submit_outcome_exception_does_not_crash(self):
        """A generic exception from wait_for_post_submit_outcome after submit must not propagate."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.side_effect = RuntimeError("page gone")
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_watchdog.wait_for_total.return_value = 49.99
            state, total = run_payment_step(task)
        self.assertIsNone(state)
        self.assertEqual(total, 49.99)


class TestFSMFallbackTransitionPath(unittest.TestCase):
    """Fallback path: when get_current_state_for_worker returns None, retry detect+transition."""

    def setUp(self):
        _reset_watchdog()
        reset_registry()
        cleanup_worker("default")

    def tearDown(self):
        cleanup_worker("default")

    def _run_with_fallback(self, page_state: str):
        """Run run_payment_step where primary wait_for_post_submit_outcome raises so the fallback is hit."""
        task = _make_task()
        detect_calls = []

        def _detect_side_effect():
            detect_calls.append(len(detect_calls))
            if len(detect_calls) == 1:
                raise RuntimeError("primary outcome failed")
            return page_state

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.side_effect = _detect_side_effect
            mock_fsm.transition_for_worker.return_value = State(page_state)
            # Primary path failed → state is None → fallback must run
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_watchdog.wait_for_total.return_value = 49.99
            result = run_payment_step(task)
            self._detect_calls = detect_calls
            self._mock_fsm = mock_fsm
            self._mock_cdp = mock_cdp
        return result

    def test_fallback_success_state(self):
        """Fallback: after primary outcome fails, second attempt returns success."""
        state, _total = self._run_with_fallback("success")
        self.assertEqual(state.name, "success")
        self.assertEqual(len(self._detect_calls), 2)
        self._mock_fsm.transition_for_worker.assert_called_with("default", "success")

    def test_fallback_declined_state(self):
        """Fallback: after primary outcome fails, second attempt returns declined."""
        state, _total = self._run_with_fallback("declined")
        self.assertEqual(state.name, "declined")
        self.assertEqual(len(self._detect_calls), 2)
        self._mock_fsm.transition_for_worker.assert_called_with("default", "declined")

    def test_fallback_vbv_3ds_state(self):
        """Fallback: after primary outcome fails, second attempt returns vbv_3ds."""
        state, _total = self._run_with_fallback("vbv_3ds")
        self.assertEqual(state.name, "vbv_3ds")
        self.assertEqual(len(self._detect_calls), 2)
        self._mock_fsm.transition_for_worker.assert_called_with("default", "vbv_3ds")

    def test_fallback_ui_lock_state(self):
        """Fallback: after primary outcome fails, second attempt returns ui_lock."""
        state, _total = self._run_with_fallback("ui_lock")
        self.assertEqual(state.name, "ui_lock")
        self.assertEqual(len(self._detect_calls), 2)
        self._mock_fsm.transition_for_worker.assert_called_with("default", "ui_lock")

    def test_fallback_invalid_transition_error_does_not_crash(self):
        """Fallback InvalidTransitionError must be logged as warning and not propagate."""
        task = _make_task()
        detect_calls = []

        def _detect_side_effect():
            detect_calls.append(1)
            if len(detect_calls) == 1:
                raise RuntimeError("primary outcome failed")
            return "success"

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.side_effect = _detect_side_effect
            mock_fsm.transition_for_worker.side_effect = InvalidTransitionError("bad")
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_watchdog.wait_for_total.return_value = 49.99
            state, _total = run_payment_step(task)
        self.assertIsNone(state)

    def test_primary_ui_lock_transitions(self):
        """Primary resolver path: ui_lock is forwarded to the FSM."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.time.sleep"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.return_value = "ui_lock"
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            mock_fsm.get_current_state_for_worker.return_value = State("ui_lock")
            mock_watchdog.wait_for_total.return_value = 49.99
            state, _total = run_payment_step(task)
        self.assertEqual(state.name, "ui_lock")
        mock_fsm.transition_for_worker.assert_called_once_with("default", "ui_lock")

    def test_primary_persistent_ui_busy_skips_fsm_transition(self):
        """Primary path: if ``ui_busy`` never settles, no FSM transition is
        attempted (so the non-FSM ``ui_busy`` value is never pushed into the
        FSM and InvalidTransitionError is avoided)."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.time.sleep"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.return_value = "ui_busy"
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_watchdog.wait_for_total.return_value = 49.99
            state, _total = run_payment_step(task)
        mock_fsm.transition_for_worker.assert_not_called()
        self.assertIsNone(state)

    def test_fallback_ui_lock_from_resolver(self):
        """Fallback path: ui_lock from the resolver is forwarded to the FSM."""
        task = _make_task()
        # Primary outcome raises so its FSM transition is skipped; fallback
        # then sees ui_lock.
        detect_calls = {"n": 0}

        def _detect():
            detect_calls["n"] += 1
            if detect_calls["n"] == 1:
                raise RuntimeError("primary outcome failed")
            return "ui_lock"

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.time.sleep"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.side_effect = _detect
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_watchdog.wait_for_total.return_value = 49.99
            state, _total = run_payment_step(task)
        self.assertEqual(state.name, "ui_lock")
        mock_fsm.transition_for_worker.assert_called_once_with("default", "ui_lock")

    def test_fallback_not_called_when_primary_succeeds(self):
        """When primary wait_for_post_submit_outcome succeeds, get_current_state returns non-None → no fallback."""
        task = _make_task()
        detect_call_count = []
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value.wait_for_post_submit_outcome.side_effect = (
                lambda: (detect_call_count.append(1), "success")[1]
            )
            mock_fsm.transition_for_worker.return_value = State("success")
            # Primary state is non-None → fallback must NOT run
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            mock_watchdog.wait_for_total.return_value = 49.99
            run_payment_step(task)
        # wait_for_post_submit_outcome should be called exactly once (primary path only)
        self.assertEqual(len(detect_call_count), 1)
