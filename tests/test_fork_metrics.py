"""Tests for per-branch FSM fork metrics (Phase 4 audit [H3])."""
import unittest
from unittest.mock import MagicMock, patch

from modules.monitor import main as monitor
from integration import orchestrator


def _reset_fork_counters():
    """Zero all fork counters while holding the dedicated lock."""
    with monitor._fork_counters_lock:  # pylint: disable=protected-access
        for key in monitor._fork_counters:  # pylint: disable=protected-access
            monitor._fork_counters[key] = 0  # pylint: disable=protected-access


class TestMonitorForkCounters(unittest.TestCase):

    def setUp(self):
        _reset_fork_counters()

    def tearDown(self):
        _reset_fork_counters()

    def test_monitor_fork_counters_increment(self):
        """record_fork increments the per-branch counter; get_fork_metrics returns snapshot."""
        for _ in range(3):
            monitor.record_fork("success")
        for _ in range(2):
            monitor.record_fork("declined")
        monitor.record_fork("vbv_3ds")
        monitor.record_fork("vbv_cancelled")
        monitor.record_fork("ui_lock")
        monitor.record_fork("abort_cycle")

        snapshot = monitor.get_fork_metrics()
        self.assertEqual(snapshot["fork_success"], 3)
        self.assertEqual(snapshot["fork_declined"], 2)
        self.assertEqual(snapshot["fork_vbv_3ds"], 1)
        self.assertEqual(snapshot["fork_vbv_cancelled"], 1)
        self.assertEqual(snapshot["fork_ui_lock"], 1)
        self.assertEqual(snapshot["fork_abort_cycle"], 1)

    def test_get_metrics_exposes_all_six_fork_counters(self):
        """get_metrics() must expose all 6 fork_* keys (acceptance criterion)."""
        metrics = monitor.get_metrics()
        for key in (
            "fork_success", "fork_declined", "fork_vbv_3ds",
            "fork_vbv_cancelled", "fork_ui_lock", "fork_abort_cycle",
        ):
            self.assertIn(key, metrics, f"{key} missing from get_metrics() output")

    def test_record_fork_unknown_branch_is_logged_not_counted(self):
        with self.assertLogs(monitor._logger, level="WARNING"):
            monitor.record_fork("bogus_branch")
        snapshot = monitor.get_fork_metrics()
        # Unknown branches must not silently inflate any existing counter.
        self.assertNotIn("fork_bogus_branch", snapshot)
        self.assertTrue(all(v == 0 for v in snapshot.values()))


class _State:
    def __init__(self, name):
        self.name = name


class TestOrchestratorHandleOutcomeRecordsForkMetrics(unittest.TestCase):
    """handle_outcome must record exactly one fork metric per call."""

    def setUp(self):
        _reset_fork_counters()

    def tearDown(self):
        _reset_fork_counters()

    def test_success_branch_records_once(self):
        orchestrator.handle_outcome(_State("success"), [], worker_id="w-1")
        self.assertEqual(monitor.get_fork_metrics()["fork_success"], 1)

    def test_ui_lock_branch_records_once(self):
        orchestrator.handle_outcome(_State("ui_lock"), [], worker_id="w-1")
        self.assertEqual(monitor.get_fork_metrics()["fork_ui_lock"], 1)

    def test_declined_branch_records_once(self):
        orchestrator.handle_outcome(_State("declined"), [], worker_id="w-1")
        self.assertEqual(monitor.get_fork_metrics()["fork_declined"], 1)

    def test_vbv_cancelled_branch_records_once(self):
        orchestrator.handle_outcome(_State("vbv_cancelled"), [], worker_id="w-1")
        self.assertEqual(monitor.get_fork_metrics()["fork_vbv_cancelled"], 1)

    def test_abort_cycle_branch_records_once(self):
        """When handle_outcome returns 'abort_cycle', fork_abort_cycle increments."""
        ctx = MagicMock()
        ctx.swap_count = 99  # force _ctx_next_swap_card → None
        with patch.object(orchestrator, "_ctx_next_swap_card", return_value=None):
            result = orchestrator.handle_outcome(
                _State("declined"), [], worker_id="w-1", ctx=ctx,
            )
        self.assertEqual(result, "abort_cycle")
        snapshot = monitor.get_fork_metrics()
        # Declined counted once (entry), abort_cycle counted once (exit).
        self.assertEqual(snapshot["fork_declined"], 1)
        self.assertEqual(snapshot["fork_abort_cycle"], 1)

    def test_vbv_3ds_branch_records_once(self):
        """vbv_3ds entry counts once even when internal driver call fails."""
        with patch.object(orchestrator.cdp, "_get_driver",
                          side_effect=RuntimeError("no driver")):
            orchestrator.handle_outcome(_State("vbv_3ds"), [], worker_id="w-1")
        self.assertEqual(monitor.get_fork_metrics()["fork_vbv_3ds"], 1)


if __name__ == "__main__":
    unittest.main()
