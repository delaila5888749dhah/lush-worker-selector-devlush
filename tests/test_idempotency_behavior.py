"""Unit tests for idempotency mark_completed behavior in run_cycle (Issue #114, P0-6).

Verifies that:
  - mark_completed is called exactly once only when action == "complete".
  - mark_completed is NOT called for any non-success outcome
    (retry, retry_new_card, abort_cycle, await_3ds, declined).
  - release_inflight is still called (via finally) regardless of outcome.
  - A declined task remains retryable across cycles (is_duplicate stays False).

Strategy: patch run_payment_step and handle_outcome to control the action
returned, then assert whether mark_completed / release_inflight were called.
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import modules.cdp.main as _cdp_main
from modules.common.types import CardInfo, CycleContext, State, WorkerTask
from modules.fsm.main import (
    cleanup_worker,
    reset_registry,
)
from integration.orchestrator import (
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _submitted_task_ids,
    run_cycle,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_ID = "idm-test-worker"

# A fake state that handle_outcome would normally produce
_FAKE_STATE = State("success")
_FAKE_TOTAL = "50.00"


def _make_task(task_id: str = "task-idm-001") -> WorkerTask:
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="12",
        exp_year="2030",
        cvv="123",
    )
    return WorkerTask(
        task_id=task_id,
        recipient_email="test@example.com",
        amount=50,
        primary_card=card,
        order_queue=(card,),
    )


def _make_store_mock(is_duplicate: bool = False) -> MagicMock:
    store = MagicMock()
    store.is_duplicate.return_value = is_duplicate
    return store


def _make_billing_mock() -> MagicMock:
    billing = MagicMock()
    profile = MagicMock()
    profile.zip_code = "90210"
    billing.select_profile.return_value = profile
    return billing


# ---------------------------------------------------------------------------
# Base class — patches run_payment_step and handle_outcome at the module level
# ---------------------------------------------------------------------------

class _IdmBase(unittest.TestCase):
    """Base class that patches the inner steps of run_cycle.

    Subclasses set ``_handle_outcome_return`` to control what action
    handle_outcome returns.  run_payment_step is patched to return a
    synthetic (State, total) tuple without touching a real driver.
    """

    _handle_outcome_return = "complete"

    def setUp(self):
        reset_registry()
        cleanup_worker(_WORKER_ID)
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()

    def tearDown(self):
        cleanup_worker(_WORKER_ID)

    def _run(self, action_return=None, task_id: str = "task-idm-001"):
        """Run one cycle with mocked inner steps; return (action, store_mock)."""
        if action_return is None:
            action_return = self._handle_outcome_return

        task = _make_task(task_id=task_id)
        store_mock = _make_store_mock()
        billing_mock = _make_billing_mock()

        fake_state = State("success")

        with patch("integration.orchestrator.billing", billing_mock), \
             patch("integration.orchestrator._get_idempotency_store",
                   return_value=store_mock), \
             patch("integration.orchestrator.run_payment_step",
                   return_value=(fake_state, _FAKE_TOTAL)), \
             patch("integration.orchestrator.handle_outcome",
                   return_value=action_return), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._notify_success"):
            result = run_cycle(task, worker_id=_WORKER_ID)

        action = result[0] if isinstance(result, tuple) else result
        return action, store_mock


# ---------------------------------------------------------------------------
# Test 1: Success → mark_completed IS called
# ---------------------------------------------------------------------------

class TestMarkCompletedOnSuccess(_IdmBase):
    """Test 1: action == 'complete' → mark_completed called exactly once."""

    _handle_outcome_return = "complete"

    def test_success_calls_mark_completed(self):
        action, store = self._run()
        self.assertEqual(action, "complete")
        store.mark_completed.assert_called_once_with("task-idm-001")

    def test_success_mark_completed_receives_correct_task_id(self):
        task_id = "task-unique-abc123"
        _action, store = self._run(task_id=task_id)
        store.mark_completed.assert_called_once_with(task_id)

    def test_success_release_inflight_still_called(self):
        """release_inflight must always be called (in finally block)."""
        _action, store = self._run()
        store.release_inflight.assert_called()


# ---------------------------------------------------------------------------
# Test 2: Declined / abort_cycle → mark_completed NOT called
# ---------------------------------------------------------------------------

class TestMarkCompletedNotCalledOnDeclined(_IdmBase):
    """Test 2: non-success outcomes → mark_completed must NOT be called."""

    def test_declined_does_not_call_mark_completed(self):
        """Declined outcome must not mark the task as completed."""
        action, store = self._run(action_return="retry_new_card")
        self.assertNotEqual(action, "complete")
        store.mark_completed.assert_not_called()

    def test_abort_cycle_does_not_call_mark_completed(self):
        """abort_cycle outcome must not mark the task as completed."""
        action, store = self._run(action_return="abort_cycle")
        self.assertEqual(action, "abort_cycle")
        store.mark_completed.assert_not_called()

    def test_declined_still_calls_release_inflight(self):
        """release_inflight must still be called via the finally block."""
        _action, store = self._run(action_return="retry_new_card")
        store.release_inflight.assert_called()

    def test_abort_cycle_still_calls_release_inflight(self):
        """release_inflight must be called even for abort_cycle."""
        _action, store = self._run(action_return="abort_cycle")
        store.release_inflight.assert_called()


# ---------------------------------------------------------------------------
# Test 3: Retry → mark_completed NOT called
# ---------------------------------------------------------------------------

class TestMarkCompletedNotCalledOnRetry(_IdmBase):
    """Test 3: 'retry' outcome → mark_completed must NOT be called.

    With the P0-2 retry loop enabled, persistent 'retry' outcomes are capped at
    2 attempts and then converted to 'abort_cycle', so the final action is
    'abort_cycle' rather than 'retry'.  The core invariant (mark_completed is
    never called for a non-success outcome) remains unchanged.
    """

    def test_retry_does_not_call_mark_completed(self):
        """retry outcome (capped to abort_cycle by retry loop) → mark_completed must be skipped."""
        action, store = self._run(action_return="retry")
        # Retry loop: handle_outcome returns "retry" twice → abort_cycle
        self.assertEqual(action, "abort_cycle")
        store.mark_completed.assert_not_called()

    def test_retry_releases_inflight(self):
        """release_inflight is called even on retry outcome."""
        _action, store = self._run(action_return="retry")
        store.release_inflight.assert_called()


# ---------------------------------------------------------------------------
# Test 4: await_3ds → mark_completed NOT called
# ---------------------------------------------------------------------------

class TestMarkCompletedNotCalledOnAwait3ds(_IdmBase):
    """Test 4: 'await_3ds' outcome → mark_completed must NOT be called."""

    def test_await_3ds_does_not_call_mark_completed(self):
        """await_3ds → mark_completed must be skipped."""
        action, store = self._run(action_return="await_3ds")
        self.assertEqual(action, "await_3ds")
        store.mark_completed.assert_not_called()

    def test_await_3ds_releases_inflight(self):
        """release_inflight is called even on await_3ds outcome."""
        _action, store = self._run(action_return="await_3ds")
        store.release_inflight.assert_called()


# ---------------------------------------------------------------------------
# Test 5: Cross-cycle retry after declined
# ---------------------------------------------------------------------------

class TestCrossCycleRetryAfterDeclined(_IdmBase):
    """Test 5: declined in cycle 1 must not block cycle 2 (is_duplicate stays False)."""

    def test_declined_task_is_not_blocked_on_second_cycle(self):
        """Cycle 1 declined → mark_completed NOT called → cycle 2 can run."""
        task_id = "task-cross-cycle-T1"
        task = _make_task(task_id=task_id)
        billing_mock = _make_billing_mock()

        # Shared store that tracks all calls
        store = MagicMock()
        store.is_duplicate.return_value = False
        fake_state = State("success")

        # ---- Cycle 1: declined ----
        with patch("integration.orchestrator.billing", billing_mock), \
             patch("integration.orchestrator._get_idempotency_store",
                   return_value=store), \
             patch("integration.orchestrator.run_payment_step",
                   return_value=(fake_state, _FAKE_TOTAL)), \
             patch("integration.orchestrator.handle_outcome",
                   return_value="retry"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._notify_success"):
            action1, _state1, _total1 = run_cycle(task, worker_id=_WORKER_ID)

        self.assertIn(
            action1,
            ("retry", "abort_cycle"),
            "Cycle 1 must not complete (retry or abort_cycle with retry loop)",
        )
        # mark_completed must NOT have been called after cycle 1
        store.mark_completed.assert_not_called()

        # ---- Cycle 2: same task_id, store not marking completed → still retryable ----
        # is_duplicate still returns False (no bogus mark_completed happened)
        store.is_duplicate.return_value = False
        cleanup_worker(_WORKER_ID)

        with patch("integration.orchestrator.billing", billing_mock), \
             patch("integration.orchestrator._get_idempotency_store",
                   return_value=store), \
             patch("integration.orchestrator.run_payment_step",
                   return_value=(fake_state, _FAKE_TOTAL)), \
             patch("integration.orchestrator.handle_outcome",
                   return_value="complete"), \
             patch("integration.orchestrator.initialize_cycle"), \
             patch("integration.orchestrator._notify_success"):
            action2, _state2, _total2 = run_cycle(task, worker_id=_WORKER_ID)

        # Cycle 2 must NOT be blocked and must succeed
        self.assertEqual(action2, "complete", "Cycle 2 must succeed")
        store.mark_completed.assert_called_once_with(task_id)

    def test_is_duplicate_false_after_declined_allows_retry(self):
        """Confirm that after a declined cycle, mark_completed was never called."""
        task_id = "task-cross-retry-T2"
        _action, store = self._run(action_return="retry", task_id=task_id)
        # mark_completed must never have been called for a retry/declined task
        store.mark_completed.assert_not_called()
        # is_duplicate was never indirectly set True (no bogus mark_completed)
        # → the real store would still allow the task on the next cycle
        self.assertEqual(store.mark_completed.call_count, 0)


if __name__ == "__main__":
    unittest.main()

