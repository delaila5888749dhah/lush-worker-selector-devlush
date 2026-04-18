"""Tests for integration/rollout_scheduler.py — deprecation shim.

The legacy scheduler has been absorbed by ``integration.runtime``; this
module is now a thin backward-compatibility layer that emits a
``DeprecationWarning`` on every call.
"""
import unittest
import warnings

import integration.rollout_scheduler as sched


class TestRolloutSchedulerShim(unittest.TestCase):
    """Validate the shim's public surface & deprecation signalling."""

    def test_start_scheduler_emits_deprecation_warning_and_returns_false(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = sched.start_scheduler(interval=60.0)
        self.assertFalse(result)
        self.assertTrue(any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ))

    def test_stop_scheduler_returns_false_when_never_started(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = sched.stop_scheduler(timeout=1.0)
        self.assertFalse(result)
        self.assertTrue(any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ))

    def test_get_scheduler_status_returns_dict_with_legacy_keys(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            status = sched.get_scheduler_status()
        self.assertTrue(any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ))
        expected_keys = {
            "running", "current_step", "current_workers",
            "next_workers", "stable_since", "seconds_until_advance",
            "advance_eligible", "rollout_complete",
        }
        self.assertEqual(set(status.keys()), expected_keys)
        self.assertFalse(status["running"])
        self.assertFalse(status["advance_eligible"])

    def test_advance_step_emits_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            success, reason = sched.advance_step()
        self.assertFalse(success)
        self.assertEqual(reason, "deprecated")
        self.assertTrue(any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ))

    def test_reset_is_noop_safe(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertIsNone(sched.reset())
        self.assertTrue(any(
            issubclass(w.category, DeprecationWarning) for w in caught
        ))


if __name__ == "__main__":
    unittest.main()
