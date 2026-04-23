"""Tests for UI-lock retry metric counters in modules/monitor/main.py.

Covers the three new counters (retry / recovered / exhausted), their
exposure via get_metrics(), reset(), and the wiring from
integration.orchestrator.run_cycle's UI-lock retry loop.
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
from modules.common.types import CardInfo, CycleContext, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry
from modules.monitor import main as monitor


_WORKER_ID = "ui-lock-metrics-test-worker"


def _make_task() -> WorkerTask:
    return WorkerTask(
        task_id="task-uilm-001",
        recipient_email="t@example.com",
        amount=50,
        primary_card=CardInfo(
            card_number="4111111111111111",
            exp_month="12", exp_year="2030", cvv="123",
        ),
        order_queue=(),
    )


def _make_billing_mock() -> MagicMock:
    b = MagicMock()
    p = MagicMock()
    p.zip_code = "90210"
    p.email = "b@example.com"
    b.select_profile.return_value = p
    return b


def _make_store_mock() -> MagicMock:
    s = MagicMock()
    s.is_duplicate.return_value = False
    return s


_STORE_PATCH = "integration.orchestrator._get_idempotency_store"


class TestUiLockMonitorCounters(unittest.TestCase):
    """Unit tests for the three new counter functions."""

    def setUp(self):
        monitor.reset()

    def tearDown(self):
        monitor.reset()

    def test_record_ui_lock_retry_increments(self):
        monitor.record_ui_lock_retry()
        monitor.record_ui_lock_retry()
        self.assertEqual(monitor.get_metrics()["ui_lock_retry_count"], 2)

    def test_record_ui_lock_recovered_increments(self):
        monitor.record_ui_lock_recovered()
        self.assertEqual(monitor.get_metrics()["ui_lock_recovered_count"], 1)

    def test_record_ui_lock_exhausted_increments(self):
        monitor.record_ui_lock_exhausted()
        monitor.record_ui_lock_exhausted()
        self.assertEqual(monitor.get_metrics()["ui_lock_exhausted_count"], 2)

    def test_get_metrics_contains_ui_lock_keys(self):
        m = monitor.get_metrics()
        self.assertIn("ui_lock_retry_count", m)
        self.assertIn("ui_lock_recovered_count", m)
        self.assertIn("ui_lock_exhausted_count", m)
        self.assertEqual(m["ui_lock_retry_count"], 0)
        self.assertEqual(m["ui_lock_recovered_count"], 0)
        self.assertEqual(m["ui_lock_exhausted_count"], 0)

    def test_reset_clears_ui_lock_counters(self):
        monitor.record_ui_lock_retry()
        monitor.record_ui_lock_recovered()
        monitor.record_ui_lock_exhausted()
        monitor.reset()
        m = monitor.get_metrics()
        self.assertEqual(m["ui_lock_retry_count"], 0)
        self.assertEqual(m["ui_lock_recovered_count"], 0)
        self.assertEqual(m["ui_lock_exhausted_count"], 0)


class TestUiLockMetricsWiring(unittest.TestCase):
    """Verify the orchestrator records metrics at the expected points."""

    def setUp(self):
        reset_registry()
        cleanup_worker(_WORKER_ID)
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()
        monitor.reset()

    def tearDown(self):
        cleanup_worker(_WORKER_ID)
        monitor.reset()

    def test_recovered_recorded_when_focus_shift_clears_lock(self):
        """ui_lock → focus_shift → detect=success → recorded retry+recovered."""
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
            mock_cdp.detect_page_state.return_value = "success"
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("success")
            run_cycle(task, worker_id=_WORKER_ID,
                      ctx=CycleContext(cycle_id="c-rec", worker_id=_WORKER_ID))

        m = monitor.get_metrics()
        self.assertEqual(m["ui_lock_retry_count"], 1)
        self.assertEqual(m["ui_lock_recovered_count"], 1)
        self.assertEqual(m["ui_lock_exhausted_count"], 0)

    def test_exhausted_recorded_when_lock_persists_past_cap(self):
        """Lock never clears → retry counter reaches MAX, exhausted recorded once."""
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
            mock_cdp.detect_page_state.return_value = "ui_lock"  # never clears
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.transition_for_worker.return_value = State("ui_lock")
            run_cycle(task, worker_id=_WORKER_ID,
                      ctx=CycleContext(cycle_id="c-exh", worker_id=_WORKER_ID))

        m = monitor.get_metrics()
        self.assertEqual(m["ui_lock_retry_count"], _orch._MAX_UI_LOCK_RETRIES)
        self.assertEqual(m["ui_lock_recovered_count"], 0)
        # Exhaustion must be recorded exactly once, even though the loop
        # may see the ui_lock state on multiple iterations.
        self.assertEqual(m["ui_lock_exhausted_count"], 1)

    def test_no_metrics_when_flag_disabled(self):
        """With _ENABLE_RETRY_UI_LOCK=False no UI-lock metrics are recorded."""
        task = _make_task()
        with patch.object(_orch, "_ENABLE_RETRY_UI_LOCK", False), \
             patch("integration.orchestrator.run_payment_step",
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
                      ctx=CycleContext(cycle_id="c-off", worker_id=_WORKER_ID))

        m = monitor.get_metrics()
        self.assertEqual(m["ui_lock_retry_count"], 0)
        self.assertEqual(m["ui_lock_recovered_count"], 0)
        self.assertEqual(m["ui_lock_exhausted_count"], 0)


if __name__ == "__main__":
    unittest.main()
