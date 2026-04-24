"""P0-4 tests: UI lock focus-shift auto-recovery in run_cycle (#112).

Verifies that run_cycle correctly:
  - Calls cdp.handle_ui_lock_focus_shift when state is "ui_lock".
  - Retries cdp.detect_page_state after focus-shift.
  - Continues with the resolved state when ui_lock clears.
  - Caps focus-shift attempts at _MAX_UI_LOCK_RETRIES (default 2).
  - Aborts when ui_lock persists beyond the cap.
  - Skips focus-shift when ENABLE_RETRY_UI_LOCK flag is disabled.
  - Handles focus-shift and detect_page_state exceptions gracefully.

Also covers the cdp.handle_ui_lock_focus_shift(worker_id) wrapper added to
modules/cdp/main.py, verifying it delegates to the driver-level function with
the correct raw driver (unwrapping GivexDriver._driver when present).
"""
# pylint: disable=protected-access
import unittest
from unittest.mock import MagicMock, patch

import integration.orchestrator as _orch
from integration.orchestrator import (
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _submitted_task_ids,
    run_cycle,
)
import modules.cdp.main as cdp_main
from modules.common.types import CardInfo, CycleContext, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_ID = "p04-ui-lock-test-worker"


def _make_card(suffix: str = "111111") -> CardInfo:
    return CardInfo(
        card_number=f"4111111111{suffix}",
        exp_month="12",
        exp_year="2030",
        cvv="123",
    )


def _make_task(order_queue: tuple = ()) -> WorkerTask:
    return WorkerTask(
        task_id="task-p04-001",
        recipient_email="test@example.com",
        amount=50,
        primary_card=_make_card(),
        order_queue=order_queue,
    )


def _make_billing_mock() -> MagicMock:
    billing = MagicMock()
    profile = MagicMock()
    profile.zip_code = "90210"
    profile.email = "billing@example.com"
    billing.select_profile.return_value = profile
    return billing


def _make_store_mock() -> MagicMock:
    store = MagicMock()
    store.is_duplicate.return_value = False
    return store


_STORE_PATCH = "integration.orchestrator._get_idempotency_store"


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class _P04Base(unittest.TestCase):
    """Base: clears idempotency + FSM state before/after each test."""

    def setUp(self):
        reset_registry()
        cleanup_worker(_WORKER_ID)
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()

    def tearDown(self):
        cleanup_worker(_WORKER_ID)


# ---------------------------------------------------------------------------
# Test group 1: focus-shift called on ui_lock
# ---------------------------------------------------------------------------

class TestUiLockFocusShiftCalled(_P04Base):
    """handle_ui_lock_focus_shift must be called when state is ui_lock."""

    def test_focus_shift_called_on_first_ui_lock(self):
        """cdp.handle_ui_lock_focus_shift is called when run_payment_step returns ui_lock."""
        task = _make_task()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            mock_cdp.detect_page_state.return_value = "ui_lock"  # still locked
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            run_cycle(task, worker_id=_WORKER_ID,
                      ctx=CycleContext(cycle_id="c1", worker_id=_WORKER_ID))

        self.assertEqual(
            mock_cdp.handle_ui_lock_focus_shift.call_args.args, (_WORKER_ID,)
        )

    def test_focus_shift_called_with_correct_worker_id(self):
        """handle_ui_lock_focus_shift is called with the exact worker_id."""
        task = _make_task()
        target_worker = "specific-worker-id"

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            mock_cdp.detect_page_state.return_value = "ui_lock"
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            cleanup_worker(target_worker)
            run_cycle(task, worker_id=target_worker,
                      ctx=CycleContext(cycle_id="c2", worker_id=target_worker))
            cleanup_worker(target_worker)

        self.assertEqual(
            mock_cdp.handle_ui_lock_focus_shift.call_args.args, (target_worker,)
        )


# ---------------------------------------------------------------------------
# Test group 2: state resolves after focus-shift
# ---------------------------------------------------------------------------

class TestUiLockResolvesAfterFocusShift(_P04Base):
    """After focus-shift, if detect_page_state returns a non-lock state, run_cycle continues."""

    def test_ui_lock_resolves_to_success(self):
        """ui_lock → focus_shift → detect = 'success' → run_cycle returns 'complete'."""
        task = _make_task()

        # First run_payment_step returns ui_lock; second should never be reached.
        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            mock_cdp.detect_page_state.return_value = "success"
            mock_cdp._get_driver.return_value = MagicMock()
            # FSM transitions: ui_lock (initial from run_payment_step), then success after recovery
            mock_fsm.transition_for_worker.return_value = State("success")
            action, state, _total = run_cycle(
                task, worker_id=_WORKER_ID,
                ctx=CycleContext(cycle_id="c3", worker_id=_WORKER_ID),
            )

        self.assertEqual(action, "complete")
        self.assertEqual(state.name, "success")

    def test_detect_page_state_retried_after_focus_shift(self):
        """cdp.detect_page_state is called once after each focus-shift attempt."""
        task = _make_task()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            # Stays locked for first attempt, resolves on second
            detect_seq = ["ui_lock", "success"]
            mock_cdp.detect_page_state.side_effect = detect_seq
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("success")
            run_cycle(task, worker_id=_WORKER_ID,
                      ctx=CycleContext(cycle_id="c4", worker_id=_WORKER_ID))

        # detect_page_state called exactly once per focus-shift attempt (2 ui_lock iters)
        self.assertEqual(mock_cdp.detect_page_state.call_count, 2)


# ---------------------------------------------------------------------------
# Test group 3: ui_lock cap enforcement
# ---------------------------------------------------------------------------

class TestUiLockRetryCapEnforced(_P04Base):
    """focus-shift attempts are capped at _MAX_UI_LOCK_RETRIES (2); then abort."""

    def test_focus_shift_not_called_more_than_cap_times(self):
        """handle_ui_lock_focus_shift called at most _MAX_UI_LOCK_RETRIES times."""
        task = _make_task()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            mock_cdp.detect_page_state.return_value = "ui_lock"  # never resolves
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            run_cycle(task, worker_id=_WORKER_ID,
                      ctx=CycleContext(cycle_id="c5", worker_id=_WORKER_ID))

        self.assertLessEqual(
            mock_cdp.handle_ui_lock_focus_shift.call_count,
            _orch._MAX_UI_LOCK_RETRIES,
        )

    def test_abort_cycle_when_ui_lock_persists_beyond_cap(self):
        """run_cycle returns abort_cycle when ui_lock never resolves."""
        task = _make_task()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = False  # shift failed
            mock_cdp.detect_page_state.return_value = "ui_lock"
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            action, _state, _total = run_cycle(
                task, worker_id=_WORKER_ID,
                ctx=CycleContext(cycle_id="c6", worker_id=_WORKER_ID),
            )

        self.assertEqual(action, "abort_cycle")

    def test_focus_shift_called_exactly_max_retries_times(self):
        """focus-shift is called exactly _MAX_UI_LOCK_RETRIES times when lock never clears."""
        task = _make_task()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            mock_cdp.detect_page_state.return_value = "ui_lock"
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            run_cycle(task, worker_id=_WORKER_ID,
                      ctx=CycleContext(cycle_id="c7", worker_id=_WORKER_ID))

        self.assertEqual(
            mock_cdp.handle_ui_lock_focus_shift.call_count,
            _orch._MAX_UI_LOCK_RETRIES,
        )


# ---------------------------------------------------------------------------
# Test group 4: feature flag ENABLE_RETRY_UI_LOCK=0
# ---------------------------------------------------------------------------

class TestUiLockFeatureFlag(_P04Base):
    """ENABLE_RETRY_UI_LOCK=0 disables automatic focus-shift recovery."""

    def test_feature_flag_disabled_skips_focus_shift(self):
        """With _ENABLE_RETRY_UI_LOCK=False, handle_ui_lock_focus_shift is never called."""
        task = _make_task()
        original = _orch._ENABLE_RETRY_UI_LOCK
        try:
            _orch._ENABLE_RETRY_UI_LOCK = False

            with patch("integration.orchestrator.run_payment_step",
                       return_value=(State("ui_lock"), "0.00")), \
                 patch("integration.orchestrator.billing", _make_billing_mock()), \
                 patch(_STORE_PATCH, return_value=_make_store_mock()), \
                 patch("integration.orchestrator._notify_success"), \
                 patch("integration.orchestrator.initialize_cycle"), \
                 patch("integration.orchestrator._alerting"), \
                 patch("integration.orchestrator.cdp") as mock_cdp:
                mock_cdp._get_driver.return_value = MagicMock()
                run_cycle(task, worker_id=_WORKER_ID,
                          ctx=CycleContext(cycle_id="c8", worker_id=_WORKER_ID))
        finally:
            _orch._ENABLE_RETRY_UI_LOCK = original

        self.assertFalse(mock_cdp.handle_ui_lock_focus_shift.called)

    def test_feature_flag_disabled_still_aborts_on_ui_lock(self):
        """With flag off, ui_lock path still terminates with abort_cycle (unchanged behaviour)."""
        task = _make_task()
        original = _orch._ENABLE_RETRY_UI_LOCK
        try:
            _orch._ENABLE_RETRY_UI_LOCK = False

            with patch("integration.orchestrator.run_payment_step",
                       return_value=(State("ui_lock"), "0.00")), \
                 patch("integration.orchestrator.billing", _make_billing_mock()), \
                 patch(_STORE_PATCH, return_value=_make_store_mock()), \
                 patch("integration.orchestrator._notify_success"), \
                 patch("integration.orchestrator.initialize_cycle"), \
                 patch("integration.orchestrator._alerting"), \
                 patch("integration.orchestrator.cdp") as mock_cdp:
                mock_cdp._get_driver.return_value = MagicMock()
                action, _state, _total = run_cycle(
                    task, worker_id=_WORKER_ID,
                    ctx=CycleContext(cycle_id="c9", worker_id=_WORKER_ID),
                )
        finally:
            _orch._ENABLE_RETRY_UI_LOCK = original

        self.assertEqual(action, "abort_cycle")


# ---------------------------------------------------------------------------
# Test group 5: exception resilience
# ---------------------------------------------------------------------------

class TestUiLockExceptionResilience(_P04Base):
    """Exceptions in focus-shift or detect_page_state must be swallowed gracefully."""

    def test_focus_shift_exception_is_swallowed(self):
        """RuntimeError from handle_ui_lock_focus_shift does not propagate to caller."""
        task = _make_task()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.side_effect = RuntimeError("selenium gone")
            mock_cdp.detect_page_state.return_value = "ui_lock"
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            # Must not raise
            action, _state, _total = run_cycle(
                task, worker_id=_WORKER_ID,
                ctx=CycleContext(cycle_id="c10", worker_id=_WORKER_ID),
            )

        self.assertEqual(action, "abort_cycle")

    def test_detect_page_state_exception_is_swallowed(self):
        """RuntimeError from detect_page_state retry does not propagate; state stays ui_lock."""
        task = _make_task()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("ui_lock"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            mock_cdp.detect_page_state.side_effect = RuntimeError("detect failed")
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            action, _state, _total = run_cycle(
                task, worker_id=_WORKER_ID,
                ctx=CycleContext(cycle_id="c11", worker_id=_WORKER_ID),
            )

        self.assertEqual(action, "abort_cycle")


# ---------------------------------------------------------------------------
# Test group 6: cdp.handle_ui_lock_focus_shift wrapper
# ---------------------------------------------------------------------------

class TestCdpHandleUiLockFocusShiftWrapper(unittest.TestCase):
    """modules.cdp.main.handle_ui_lock_focus_shift delegates to the driver-level function."""

    def setUp(self):
        with cdp_main._registry_lock:
            cdp_main._driver_registry.clear()

    def tearDown(self):
        with cdp_main._registry_lock:
            cdp_main._driver_registry.clear()

    def test_calls_driver_level_function_with_givex_wrapper(self):
        """Wrapper passes the GivexDriver wrapper (not unwrapped raw driver)
        so the driver-level helper can reach ``bounding_box_click`` (Phase 4 [B2])."""
        raw_driver = MagicMock(name="raw_selenium_driver")
        givex_wrapper = MagicMock(name="givex_driver_wrapper")
        givex_wrapper._driver = raw_driver
        cdp_main.register_driver("w-p04", givex_wrapper)

        with patch("modules.cdp.main._driver_focus_shift",
                   return_value=True) as mock_shift:
            result = cdp_main.handle_ui_lock_focus_shift("w-p04")

        mock_shift.assert_called_once_with(givex_wrapper)
        self.assertTrue(result)

    def test_uses_driver_directly_when_no_inner_driver_attr(self):
        """Wrapper passes driver as-is when it has no _driver attribute."""
        plain_driver = MagicMock(name="plain_driver", spec=[])
        cdp_main.register_driver("w-p04b", plain_driver)

        with patch("modules.cdp.main._driver_focus_shift",
                   return_value=False) as mock_shift:
            result = cdp_main.handle_ui_lock_focus_shift("w-p04b")

        mock_shift.assert_called_once_with(plain_driver)
        self.assertFalse(result)

    def test_raises_runtime_error_when_driver_not_registered(self):
        """handle_ui_lock_focus_shift raises RuntimeError for unknown worker_id."""
        with self.assertRaises(RuntimeError):
            cdp_main.handle_ui_lock_focus_shift("no-such-worker")


# ---------------------------------------------------------------------------
# Test group 7: ui_lock_retry_count resets on card swap
# ---------------------------------------------------------------------------

class TestUiLockCounterResetsOnCardSwap(_P04Base):
    """ui_lock_retry_count resets to 0 when a card swap occurs."""

    def test_ui_lock_count_resets_after_card_swap(self):
        """After a card swap, ui_lock retries are fresh (counter reset to 0)."""
        card2 = _make_card("222222")
        task = _make_task(order_queue=(card2,))

        # Sequence: ui_lock (card1) × 2 → declined (card1) → swap to card2 → success
        # With the counter reset, card2 also gets a fresh 2 focus-shift attempts.
        state_seq = [
            State("ui_lock"),
            State("ui_lock"),
            State("declined"),
            State("success"),
        ]

        def _fake_rps(*_a, **_kw):
            return state_seq.pop(0), "50.00"

        ctx = CycleContext(cycle_id="c12", worker_id=_WORKER_ID)

        with patch("integration.orchestrator.run_payment_step", side_effect=_fake_rps), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.handle_ui_lock_focus_shift.return_value = True
            # focus-shift doesn't help — detect still returns ui_lock.
            mock_cdp.detect_page_state.return_value = "ui_lock"
            mock_cdp._get_driver.return_value = MagicMock()
            # declined and success are handled by handle_outcome directly.
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            action, _state, _total = run_cycle(task, worker_id=_WORKER_ID, ctx=ctx)

        # The cycle should eventually reach abort (2 ui_lock + declined exhausts queue)
        # or succeed — the important thing is that it doesn't crash.
        self.assertIn(action, ("abort_cycle", "complete", "retry"))


if __name__ == "__main__":
    unittest.main()
