"""Tests for modules.rollout.scheduler."""

import math
import time
import unittest
from unittest.mock import MagicMock, patch

from modules.rollout import main as rollout
from modules.rollout import scheduler


class SchedulerModuleResetMixin:
    def setUp(self):
        scheduler.reset()
        rollout.reset()

    def tearDown(self):
        scheduler.reset()
        rollout.reset()


class TestClampInterval(SchedulerModuleResetMixin, unittest.TestCase):
    def test_clamp_interval_falls_back_to_default_for_non_numeric(self):
        self.assertEqual(scheduler._clamp_interval("bad"), 300.0)

    def test_clamp_interval_clamps_out_of_range_values(self):
        self.assertEqual(scheduler._clamp_interval(0), 1.0)
        self.assertEqual(scheduler._clamp_interval(999999), 86400.0)
        self.assertEqual(scheduler._clamp_interval(math.inf), 1.0)


class TestLifecycle(SchedulerModuleResetMixin, unittest.TestCase):
    @patch("modules.rollout.scheduler.threading.Thread")
    def test_start_uses_clamped_interval(self, mock_thread):
        thread = MagicMock()
        thread.is_alive.return_value = False
        mock_thread.return_value = thread

        started = scheduler.start(interval=0)

        self.assertTrue(started)
        mock_thread.assert_called_once()
        self.assertEqual(mock_thread.call_args.kwargs["args"], (1.0,))
        thread.start.assert_called_once()

    @patch("modules.rollout.scheduler.threading.Thread")
    def test_start_returns_false_when_thread_is_alive(self, mock_thread):
        thread = MagicMock()
        thread.is_alive.return_value = True
        mock_thread.return_value = thread
        scheduler._scheduler_thread = thread

        self.assertFalse(scheduler.start())
        mock_thread.assert_not_called()


class TestSchedulerLoop(SchedulerModuleResetMixin, unittest.TestCase):
    def _run_once(self):
        with patch.object(
            scheduler._stop_event,
            "wait",
            side_effect=lambda timeout: scheduler._stop_event.set() or True,
        ):
            scheduler._stop_event.clear()
            scheduler._scheduler_loop(1.0)

    def test_scheduler_loop_sets_stable_since_when_stable(self):
        scheduler.configure(lambda: True)

        self._run_once()

        self.assertIsNotNone(scheduler._stable_since)

    def test_scheduler_loop_resets_stable_since_when_unstable(self):
        scheduler._stable_since = time.monotonic() - 10.0
        scheduler.configure(lambda: False)

        self._run_once()

        self.assertIsNone(scheduler._stable_since)

    @patch("modules.rollout.scheduler.rollout.try_scale_up")
    @patch("modules.rollout.scheduler.rollout.can_scale_up", return_value=True)
    def test_scheduler_loop_advances_once_when_window_is_eligible(
        self,
        _mock_can_scale_up,
        mock_try_scale_up,
    ):
        scheduler.configure(lambda: True)
        scheduler._stable_since = (
            time.monotonic() - scheduler.STABLE_DURATION_SECONDS - 1.0
        )
        mock_try_scale_up.return_value = (3, "scaled_up", [])

        self._run_once()

        mock_try_scale_up.assert_called_once_with()
        self.assertIsNone(scheduler._stable_since)


class TestAdvanceStep(SchedulerModuleResetMixin, unittest.TestCase):
    @patch("modules.rollout.scheduler.rollout.try_scale_up", return_value=(3, "scaled_up", []))
    def test_advance_step_resets_stable_since_after_scale_up(self, _mock_try_scale_up):
        scheduler._stable_since = time.monotonic()

        success, reason = scheduler.advance_step()

        self.assertTrue(success)
        self.assertIn("3 workers", reason)
        self.assertIsNone(scheduler._stable_since)

    def test_get_status_reports_eligible_window(self):
        scheduler._stable_since = (
            time.monotonic() - scheduler.STABLE_DURATION_SECONDS - 1.0
        )

        status = scheduler.get_status()

        self.assertTrue(status["advance_eligible"])
        self.assertEqual(status["seconds_until_advance"], 0.0)

    def test_get_status_reports_remaining_window_before_eligibility(self):
        scheduler._stable_since = time.monotonic()

        status = scheduler.get_status()

        self.assertFalse(status["advance_eligible"])
        self.assertGreater(status["seconds_until_advance"], 0.0)


if __name__ == "__main__":
    unittest.main()
