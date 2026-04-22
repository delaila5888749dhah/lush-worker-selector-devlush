"""Tests for integration/rollout_scheduler.py — rollout scheduler module."""
import time
import unittest
import warnings
from unittest.mock import patch

from modules.monitor import main as monitor
from modules.rollout import main as rollout
import integration.rollout_scheduler as sched

_HEALTHY_METRICS = {
    "success_rate": 0.95,
    "error_rate": 0.01,
    "restarts_last_hour": 0,
    "memory_usage_bytes": 0,
    "baseline_success_rate": None,
    "success_count": 95,
    "error_count": 1,
}
_BAD_METRICS = {
    "success_rate": 0.50,
    "error_rate": 0.10,
    "restarts_last_hour": 5,
    "memory_usage_bytes": 0,
    "baseline_success_rate": None,
    "success_count": 50,
    "error_count": 10,
}


class SchedulerResetMixin:
    def setUp(self):
        sched.reset()
        rollout.reset()
        monitor.reset()
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)

    def tearDown(self):
        sched.reset()
        rollout.reset()
        monitor.reset()


# ── Lifecycle ─────────────────────────────────────────────────────


class TestSchedulerLifecycle(SchedulerResetMixin, unittest.TestCase):
    """start_scheduler(), stop_scheduler(), reset()"""

    def test_start_returns_true_when_not_running(self):
        result = sched.start_scheduler(interval=60.0)
        self.assertTrue(result)

    def test_start_returns_false_when_already_running(self):
        sched.start_scheduler(interval=60.0)
        result = sched.start_scheduler(interval=60.0)
        self.assertFalse(result)

    def test_stop_returns_true_when_running(self):
        sched.start_scheduler(interval=60.0)
        result = sched.stop_scheduler(timeout=5.0)
        self.assertTrue(result)

    def test_stop_returns_false_when_not_running(self):
        result = sched.stop_scheduler(timeout=1.0)
        self.assertFalse(result)

    def test_reset_clears_state(self):
        sched.start_scheduler(interval=60.0)
        sched._stable_since = time.monotonic()
        sched.reset()
        self.assertIsNone(sched._stable_since)
        self.assertFalse(sched.get_scheduler_status()["running"])


# ── Status ────────────────────────────────────────────────────────


class TestSchedulerStatus(SchedulerResetMixin, unittest.TestCase):
    """get_scheduler_status() contract"""

    def test_status_keys_present(self):
        status = sched.get_scheduler_status()
        expected_keys = {
            "running", "current_step", "current_workers",
            "next_workers", "stable_since", "seconds_until_advance",
            "advance_eligible", "rollout_complete",
        }
        self.assertEqual(set(status.keys()), expected_keys)

    def test_status_not_running_initially(self):
        status = sched.get_scheduler_status()
        self.assertFalse(status["running"])

    def test_rollout_complete_false_initially(self):
        status = sched.get_scheduler_status()
        self.assertFalse(status["rollout_complete"])

    def test_advance_eligible_false_initially(self):
        status = sched.get_scheduler_status()
        self.assertFalse(status["advance_eligible"])


# ── advance_step ──────────────────────────────────────────────────


class TestAdvanceStep(SchedulerResetMixin, unittest.TestCase):
    """advance_step() manual trigger"""

    @patch("modules.rollout.main.try_scale_up")
    def test_advance_step_when_healthy(self, mock_scale_up):
        mock_scale_up.return_value = (3, "scaled_up", [])
        success, reason = sched.advance_step()
        self.assertTrue(success)
        self.assertIn("3 workers", reason)

    def test_advance_step_at_max_returns_false(self):
        for _ in range(len(rollout.SCALE_STEPS) - 1):
            rollout.try_scale_up()
        success, reason = sched.advance_step()
        self.assertFalse(success)
        self.assertEqual(reason, "at max step")

    @patch("modules.rollout.main.try_scale_up")
    def test_advance_step_rolls_back_on_unhealthy(self, mock_scale_up):
        mock_scale_up.return_value = (1, "rollback", ["error rate 10% exceeds 5%"])
        success, reason = sched.advance_step()
        self.assertFalse(success)
        self.assertIn("rollback", reason)
        self.assertIn("error rate", reason)


# ── Stability tracking ────────────────────────────────────────────


class TestStabilityTracking(SchedulerResetMixin, unittest.TestCase):
    """_stable_since tracking logic"""

    @patch("modules.monitor.main.get_metrics", return_value=_BAD_METRICS)
    def test_stability_resets_on_bad_metrics(self, _mock_m):
        sched._stable_since = time.monotonic() - 100
        sched.start_scheduler(interval=1.0)
        time.sleep(1.5)
        sched.stop_scheduler(timeout=2.0)
        self.assertIsNone(sched._stable_since)

    def test_advance_eligible_after_stable_duration(self):
        past = time.monotonic() - (sched.STABLE_DURATION_SECONDS + 1)
        sched._stable_since = past
        status = sched.get_scheduler_status()
        self.assertTrue(status["advance_eligible"])
        self.assertIsNotNone(status["seconds_until_advance"])
        self.assertLessEqual(status["seconds_until_advance"], 0.0)

    @patch("modules.monitor.main.get_metrics", return_value=_HEALTHY_METRICS)
    def test_stable_since_set_on_healthy_metrics(self, _mock_m):
        sched.start_scheduler(interval=1.0)
        time.sleep(1.5)
        sched.stop_scheduler(timeout=2.0)
        self.assertIsNotNone(sched._stable_since)


# ── Scheduler loop ────────────────────────────────────────────────


class TestSchedulerLoop(SchedulerResetMixin, unittest.TestCase):
    """_scheduler_loop integration"""

    @patch("modules.rollout.main.try_scale_up")
    @patch("modules.monitor.main.get_metrics", return_value=_HEALTHY_METRICS)
    def test_loop_advances_when_stable(self, _m, mock_scale_up):
        mock_scale_up.return_value = (3, "scaled_up", [])
        sched._stable_since = (
            time.monotonic() - (sched.STABLE_DURATION_SECONDS + 10)
        )
        sched.start_scheduler(interval=1.0)
        time.sleep(1.5)
        sched.stop_scheduler(timeout=2.0)
        mock_scale_up.assert_called()

    @patch("modules.rollout.main.force_rollback")
    @patch("modules.monitor.main.get_metrics", return_value=_BAD_METRICS)
    def test_loop_rolls_back_on_high_error_rate(self, _m, mock_rb):
        mock_rb.return_value = 1
        sched.start_scheduler(interval=1.0)
        time.sleep(1.5)
        sched.stop_scheduler(timeout=2.0)
        mock_rb.assert_called()

    @patch("modules.rollout.main.try_scale_up")
    @patch("modules.monitor.main.get_metrics", return_value=_HEALTHY_METRICS)
    def test_loop_does_not_advance_before_stable_duration(self, _m, mock_su):
        sched.start_scheduler(interval=1.0)
        time.sleep(1.5)
        sched.stop_scheduler(timeout=2.0)
        mock_su.assert_not_called()

    def test_loop_stops_on_stop_event(self):
        sched.start_scheduler(interval=60.0)
        self.assertTrue(sched.get_scheduler_status()["running"])
        stopped = sched.stop_scheduler(timeout=5.0)
        self.assertTrue(stopped)
        self.assertFalse(sched.get_scheduler_status()["running"])


# ── Rollout complete ──────────────────────────────────────────────


class TestRolloutComplete(SchedulerResetMixin, unittest.TestCase):
    """rollout_complete flag derived from rollout step index"""

    def test_rollout_complete_at_max_step(self):
        for _ in range(len(rollout.SCALE_STEPS) - 1):
            rollout.try_scale_up()
        self.assertEqual(rollout.get_current_step_index(),
                         len(rollout.SCALE_STEPS) - 1)
        status = sched.get_scheduler_status()
        self.assertTrue(status["rollout_complete"])

    def test_status_shows_not_complete_at_step_zero(self):
        self.assertEqual(rollout.get_current_step_index(), 0)
        status = sched.get_scheduler_status()
        self.assertFalse(status["rollout_complete"])


# ── Deprecation signalling ────────────────────────────────────────


class TestDeprecationSignalling(unittest.TestCase):
    """Each public API must emit DeprecationWarning (shim signalling)."""

    def setUp(self):
        sched.reset()
        rollout.reset()
        monitor.reset()

    def tearDown(self):
        sched.reset()

    def test_start_scheduler_emits_deprecation_warning(self):
        """start_scheduler emits DeprecationWarning on the shim call path."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sched.start_scheduler(interval=60.0)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))

    def test_stop_scheduler_emits_deprecation_warning(self):
        """stop_scheduler emits DeprecationWarning on the shim call path."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sched.stop_scheduler(timeout=1.0)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))

    def test_get_scheduler_status_emits_deprecation_warning(self):
        """get_scheduler_status emits DeprecationWarning on the shim call path."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sched.get_scheduler_status()
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))

    def test_advance_step_emits_deprecation_warning(self):
        """advance_step emits DeprecationWarning on the shim call path."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sched.advance_step()
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))

    def test_reset_emits_deprecation_warning(self):
        """reset emits DeprecationWarning on the shim call path."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sched.reset()
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))


if __name__ == "__main__":
    unittest.main()
