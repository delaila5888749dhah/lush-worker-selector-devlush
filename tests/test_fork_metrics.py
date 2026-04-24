"""Phase 4 [H3] — per-branch FSM-fork counter tests.

Covers:
* ``modules.monitor.main.record_fork`` / ``get_fork_metrics``
* ``handle_outcome`` / ``run_cycle`` wiring — one counter increment per
  branch dispatch, exactly once per cycle outcome.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from modules.monitor import main as monitor
from modules.common.types import State


class TestForkCounterSurface(unittest.TestCase):
    def setUp(self):
        monitor.reset()

    def test_increment_success(self):
        monitor.record_fork("success")
        monitor.record_fork("success")
        monitor.record_fork("success")
        metrics = monitor.get_fork_metrics()
        self.assertEqual(metrics["fork_success"], 3)
        self.assertEqual(metrics["fork_declined"], 0)

    def test_increment_declined(self):
        monitor.record_fork("declined")
        monitor.record_fork("declined")
        self.assertEqual(monitor.get_fork_metrics()["fork_declined"], 2)

    def test_unknown_branch_logged_not_counted(self):
        # Unknown branch should not raise and not mutate any counter.
        before = monitor.get_fork_metrics()
        monitor.record_fork("nonexistent_branch")
        after = monitor.get_fork_metrics()
        self.assertEqual(before, after)

    def test_all_six_branches_exposed(self):
        metrics = monitor.get_fork_metrics()
        self.assertEqual(
            set(metrics),
            {
                "fork_success",
                "fork_declined",
                "fork_vbv_3ds",
                "fork_vbv_cancelled",
                "fork_ui_lock",
                "fork_abort_cycle",
            },
        )

    def test_get_metrics_merges_fork_counters(self):
        monitor.record_fork("success")
        metrics = monitor.get_metrics()
        # All six fork_* keys must be present in the canonical metrics dict.
        for key in (
            "fork_success",
            "fork_declined",
            "fork_vbv_3ds",
            "fork_vbv_cancelled",
            "fork_ui_lock",
            "fork_abort_cycle",
        ):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["fork_success"], 1)

    def test_reset_clears_fork_counters(self):
        monitor.record_fork("success")
        monitor.record_fork("declined")
        monitor.reset()
        self.assertEqual(
            monitor.get_fork_metrics(),
            {
                "fork_success": 0,
                "fork_declined": 0,
                "fork_vbv_3ds": 0,
                "fork_vbv_cancelled": 0,
                "fork_ui_lock": 0,
                "fork_abort_cycle": 0,
            },
        )


class TestHandleOutcomeRecordsForkMetrics(unittest.TestCase):
    """Integration: each handle_outcome branch records exactly one fork counter."""

    def setUp(self):
        monitor.reset()
        # Import inside setUp so patching is stable across test order.
        from integration import orchestrator  # noqa: WPS433
        self._orchestrator = orchestrator

    def test_success_records_fork_success(self):
        self._orchestrator.handle_outcome(State("success"), [])
        self.assertEqual(monitor.get_fork_metrics()["fork_success"], 1)

    def test_declined_records_fork_declined(self):
        # No ctx → returns "retry" without abort; counter still increments once.
        self._orchestrator.handle_outcome(State("declined"), [])
        self.assertEqual(monitor.get_fork_metrics()["fork_declined"], 1)
        self.assertEqual(monitor.get_fork_metrics()["fork_abort_cycle"], 0)

    def test_ui_lock_records_fork_ui_lock(self):
        self._orchestrator.handle_outcome(State("ui_lock"), [])
        self.assertEqual(monitor.get_fork_metrics()["fork_ui_lock"], 1)

    def test_vbv_3ds_records_fork_vbv_3ds(self):
        # Driver lookup can fail inside the branch — handler still logs and
        # returns "await_3ds"; the fork counter must be incremented first.
        with patch.object(self._orchestrator.cdp, "_get_driver",
                          side_effect=RuntimeError("no driver")):
            result = self._orchestrator.handle_outcome(State("vbv_3ds"), [])
        self.assertEqual(result, "await_3ds")
        self.assertEqual(monitor.get_fork_metrics()["fork_vbv_3ds"], 1)


if __name__ == "__main__":
    unittest.main()
