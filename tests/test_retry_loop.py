"""Integration tests for the P0-2 retry loop in run_cycle (#110).

Verifies that run_cycle correctly:
  - Loops through card swaps when cards are declined.
  - Breaks on "complete" with the expected swap_count.
  - Returns "abort_cycle" when all cards are exhausted.
  - Caps "retry" (ui_lock) at 2 attempts then aborts.
  - Respects the ENABLE_RETRY_LOOP=0 fallback.
  - Calls driver.clear_card_fields_cdp() and driver.fill_card_fields() on each swap.
"""
# pylint: disable=protected-access
# Tests intentionally access orchestrator private attributes (`_get_driver`,
# `_ENABLE_RETRY_LOOP`, idempotency dicts) to mock/override behaviour —
# matching the convention used by tests/test_idempotency_behavior.py.

import unittest
from unittest.mock import MagicMock, call, patch

import integration.orchestrator as _orch
from integration.orchestrator import (
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _submitted_task_ids,
    run_cycle,
)
from modules.common.types import CardInfo, CycleContext, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_ID = "retry-loop-test-worker"


def _make_card(suffix: str) -> CardInfo:
    return CardInfo(
        card_number=f"4111111111{suffix}",
        exp_month="12",
        exp_year="2030",
        cvv="123",
    )


def _make_task(
        primary_card: CardInfo,
        order_queue: tuple,
        task_id: str = "task-retry-loop-001",
) -> WorkerTask:
    return WorkerTask(
        task_id=task_id,
        recipient_email="test@example.com",
        amount=50,
        primary_card=primary_card,
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

class _RetryLoopBase(unittest.TestCase):
    """Base class: clears idempotency + FSM state before each test."""

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
# Test group 1: happy-path retry loop
# ---------------------------------------------------------------------------

class TestRetryLoopHappyPath(_RetryLoopBase):
    """3 cards — 2 declined → 3rd succeeds; swap_count must equal 2."""

    def test_three_cards_two_declined_third_success(self):
        """Cards 1+2 declined; card 3 success → action='complete', swap_count==2."""
        card1 = _make_card("111111")
        card2 = _make_card("222222")
        card3 = _make_card("333333")
        task = _make_task(primary_card=card1, order_queue=(card2, card3))

        # State sequence: declined, declined, success
        state_seq = [State("declined"), State("declined"), State("success")]

        def _fake_rps(*_args, **_kwargs):
            return state_seq.pop(0), "50.00"

        ctx = CycleContext(cycle_id="rlt-cycle-1", worker_id=_WORKER_ID)
        mock_driver = MagicMock()

        with patch("integration.orchestrator.run_payment_step", side_effect=_fake_rps), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = mock_driver
            action, _state, _total = run_cycle(task, worker_id=_WORKER_ID, ctx=ctx)

        self.assertEqual(action, "complete")
        self.assertEqual(ctx.swap_count, 2)

    def test_swap_cdp_calls_made_for_each_declined(self):
        """clear_card_fields_cdp + fill_card_fields called once per declined card."""
        card1 = _make_card("111111")
        card2 = _make_card("222222")
        card3 = _make_card("333333")
        task = _make_task(primary_card=card1, order_queue=(card2, card3))

        state_seq = [State("declined"), State("declined"), State("success")]

        def _fake_rps(*_args, **_kwargs):
            return state_seq.pop(0), "50.00"

        ctx = CycleContext(cycle_id="rlt-cycle-2", worker_id=_WORKER_ID)
        mock_driver = MagicMock()

        with patch("integration.orchestrator.run_payment_step", side_effect=_fake_rps), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = mock_driver
            run_cycle(task, worker_id=_WORKER_ID, ctx=ctx)

        # Two card swaps → clear + fill called twice each
        self.assertEqual(mock_driver.clear_card_fields_cdp.call_count, 2)
        self.assertEqual(mock_driver.fill_card_fields.call_count, 2)
        # Correct cards were filled in order
        self.assertEqual(mock_driver.fill_card_fields.call_args_list[0], call(card2))
        self.assertEqual(mock_driver.fill_card_fields.call_args_list[1], call(card3))


# ---------------------------------------------------------------------------
# Test group 2: all cards exhausted
# ---------------------------------------------------------------------------

class TestRetryLoopAllCardsExhausted(_RetryLoopBase):
    """3 cards — all 3 declined → action must be 'abort_cycle'."""

    def test_three_cards_all_declined_abort(self):
        """All 3 cards declined → action='abort_cycle'."""
        card1 = _make_card("111111")
        card2 = _make_card("222222")
        card3 = _make_card("333333")
        task = _make_task(primary_card=card1, order_queue=(card2, card3))

        state_seq = [State("declined"), State("declined"), State("declined")]

        def _fake_rps(*_args, **_kwargs):
            return state_seq.pop(0), "50.00"

        ctx = CycleContext(cycle_id="rlt-cycle-3", worker_id=_WORKER_ID)
        mock_driver = MagicMock()

        with patch("integration.orchestrator.run_payment_step", side_effect=_fake_rps), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = mock_driver
            action, _state, _total = run_cycle(task, worker_id=_WORKER_ID, ctx=ctx)

        self.assertEqual(action, "abort_cycle")

    def test_all_declined_swap_count_equals_queue_length(self):
        """swap_count equals len(order_queue) when all cards are exhausted."""
        card1 = _make_card("111111")
        card2 = _make_card("222222")
        card3 = _make_card("333333")
        task = _make_task(primary_card=card1, order_queue=(card2, card3))

        state_seq = [State("declined"), State("declined"), State("declined")]

        def _fake_rps(*_args, **_kwargs):
            return state_seq.pop(0), "50.00"

        ctx = CycleContext(cycle_id="rlt-cycle-4", worker_id=_WORKER_ID)
        mock_driver = MagicMock()

        with patch("integration.orchestrator.run_payment_step", side_effect=_fake_rps), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = mock_driver
            run_cycle(task, worker_id=_WORKER_ID, ctx=ctx)

        # Tried all 2 backup cards
        self.assertEqual(ctx.swap_count, 2)


# ---------------------------------------------------------------------------
# Test group 3: ui_lock retry cap
# ---------------------------------------------------------------------------

class TestRetryLoopUiLockCap(_RetryLoopBase):
    """ui_lock ('retry') is capped at 2 attempts then aborts."""

    def test_two_retries_become_abort_cycle(self):
        """'retry' returned twice → loop aborts with 'abort_cycle'."""
        card1 = _make_card("111111")
        task = _make_task(primary_card=card1, order_queue=())

        # ui_lock always returns "retry" → loop must cap and abort
        ui_lock_state = State("ui_lock")

        def _fake_rps(*_args, **_kwargs):
            return ui_lock_state, "0.00"

        ctx = CycleContext(cycle_id="rlt-cycle-5", worker_id=_WORKER_ID)
        mock_driver = MagicMock()

        with patch("integration.orchestrator.run_payment_step", side_effect=_fake_rps), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch(_STORE_PATCH, return_value=_make_store_mock()), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = mock_driver
            # Use real handle_outcome (ui_lock → "retry")
            action, _state, _total = run_cycle(task, worker_id=_WORKER_ID, ctx=ctx)

        self.assertEqual(action, "abort_cycle")


# ---------------------------------------------------------------------------
# Test group 4: terminal break conditions
# ---------------------------------------------------------------------------

class TestRetryLoopTerminalBreak(_RetryLoopBase):
    """Loop must break immediately on 'complete' and 'await_3ds'."""

    def test_complete_breaks_loop_immediately(self):
        """'complete' on first attempt → loop exits; mark_completed IS called."""
        card1 = _make_card("111111")
        task = _make_task(primary_card=card1, order_queue=(card1,))

        store = _make_store_mock()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("success"), "50.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch("integration.orchestrator._get_idempotency_store", return_value=store), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator.cdp"):
            action, _state, _total = run_cycle(task, worker_id=_WORKER_ID)

        self.assertEqual(action, "complete")
        store.mark_completed.assert_called_once()

    def test_await_3ds_breaks_loop(self):
        """'await_3ds' → loop exits immediately; mark_completed NOT called."""
        card1 = _make_card("111111")
        task = _make_task(primary_card=card1, order_queue=(card1,))

        store = _make_store_mock()

        with patch("integration.orchestrator.run_payment_step",
                   return_value=(State("vbv_3ds"), "0.00")), \
             patch("integration.orchestrator.billing", _make_billing_mock()), \
             patch("integration.orchestrator._get_idempotency_store", return_value=store), \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._alerting"), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            # vbv_3ds → handle_outcome tries driver.handle_vbv_challenge(); mock it
            mock_drv = MagicMock()
            mock_drv.handle_vbv_challenge.return_value = False
            mock_cdp._get_driver.return_value = mock_drv
            action, _state, _total = run_cycle(task, worker_id=_WORKER_ID)

        self.assertEqual(action, "await_3ds")
        store.mark_completed.assert_not_called()


# ---------------------------------------------------------------------------
# Test group 5: feature flag ENABLE_RETRY_LOOP=0
# ---------------------------------------------------------------------------

class TestRetryLoopFeatureFlag(_RetryLoopBase):
    """ENABLE_RETRY_LOOP=0 restores single-shot behaviour."""

    def test_flag_off_returns_retry_directly(self):
        """With ENABLE_RETRY_LOOP=0, run_cycle returns 'retry' without looping."""
        card1 = _make_card("111111")
        task = _make_task(primary_card=card1, order_queue=(card1,))

        original_flag = _orch._ENABLE_RETRY_LOOP
        try:
            _orch._ENABLE_RETRY_LOOP = False

            with patch("integration.orchestrator.run_payment_step",
                       return_value=(State("ui_lock"), "0.00")), \
                 patch("integration.orchestrator.billing", _make_billing_mock()), \
                 patch(_STORE_PATCH, return_value=_make_store_mock()), \
                 patch("integration.orchestrator._notify_success"), \
                 patch("integration.orchestrator.initialize_cycle"), \
                 patch("integration.orchestrator.cdp"):
                action, _state, _total = run_cycle(task, worker_id=_WORKER_ID)

            self.assertEqual(action, "retry")
        finally:
            _orch._ENABLE_RETRY_LOOP = original_flag


if __name__ == "__main__":
    unittest.main()
