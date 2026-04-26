"""Tests for per-branch FSM fork metrics — monitor sink only (Phase 4 audit [H3]).

Orchestrator-side coverage (handle_outcome → record_fork wiring) lives in the
sibling PR that introduces ``integration.orchestrator._record_fork_safe``.
"""
import unittest

from modules.monitor import main as monitor

EXPECTED_FORK_KEYS = {
    "fork_success",
    "fork_declined",
    "fork_vbv_3ds",
    "fork_vbv_cancelled",
    "fork_ui_lock",
    "fork_abort_cycle",
}


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
        for key in EXPECTED_FORK_KEYS:
            self.assertIn(key, metrics, f"{key} missing from get_metrics() output")
            self.assertEqual(metrics[key], 0)

    def test_reset_clears_fork_counters(self):
        """monitor.reset() must restore fork counters to their initial zero state."""
        monitor.record_fork("success")
        monitor.record_fork("ui_lock")
        monitor.reset()

        snapshot = monitor.get_fork_metrics()
        self.assertEqual(set(snapshot), EXPECTED_FORK_KEYS)
        self.assertTrue(all(v == 0 for v in snapshot.values()))

    def test_record_fork_unknown_branch_is_logged_not_counted(self):
        with self.assertLogs(monitor._logger, level="WARNING"):
            monitor.record_fork("bogus_branch")
        snapshot = monitor.get_fork_metrics()
        # Unknown branches must not silently inflate any existing counter.
        self.assertNotIn("fork_bogus_branch", snapshot)
        self.assertEqual(set(snapshot), EXPECTED_FORK_KEYS)
        self.assertTrue(all(v == 0 for v in snapshot.values()))

    def test_get_metrics_exposes_audit_named_aliases(self):
        """get_metrics() exposes ``declined_count`` and ``vbv_cancelled_count``
        as aliases of the corresponding fork counters (Phase 4 audit [P4-H3]).
        """
        # Initial snapshot: both aliases present and zero.
        metrics = monitor.get_metrics()
        self.assertIn("declined_count", metrics)
        self.assertIn("vbv_cancelled_count", metrics)
        self.assertEqual(metrics["declined_count"], 0)
        self.assertEqual(metrics["vbv_cancelled_count"], 0)

        # After recording fork outcomes, aliases must equal the fork_* keys.
        for _ in range(2):
            monitor.record_fork("declined")
        for _ in range(3):
            monitor.record_fork("vbv_cancelled")

        metrics = monitor.get_metrics()
        self.assertEqual(metrics["declined_count"], metrics["fork_declined"])
        self.assertEqual(
            metrics["vbv_cancelled_count"], metrics["fork_vbv_cancelled"]
        )
        self.assertEqual(metrics["declined_count"], 2)
        self.assertEqual(metrics["vbv_cancelled_count"], 3)


if __name__ == "__main__":
    unittest.main()
