"""M15 — Rollback CB and Billing CB are independent with distinct log actions."""
import unittest

from integration import runtime


class TestCircuitBreakersIndependent(unittest.TestCase):
    def test_rollback_cb_constants(self):
        """The rollback CB pause is 300s (Blueprint §14.2)."""
        self.assertEqual(runtime._CIRCUIT_BREAKER_PAUSE, 300)
        self.assertEqual(runtime._MAX_CONSECUTIVE_ROLLBACKS, 3)

    def test_billing_cb_env_overridable(self):
        """Billing CB threshold and pause are env-overridable (NON-IF rule 7)."""
        import importlib, os
        prev_threshold = os.environ.get("BILLING_CB_THRESHOLD")
        prev_pause = os.environ.get("BILLING_CB_PAUSE")
        try:
            os.environ["BILLING_CB_THRESHOLD"] = "7"
            os.environ["BILLING_CB_PAUSE"] = "99"
            reloaded = importlib.reload(runtime)
            self.assertEqual(reloaded._BILLING_CB_THRESHOLD, 7)
            self.assertEqual(reloaded._BILLING_CB_PAUSE, 99)
        finally:
            if prev_threshold is None:
                os.environ.pop("BILLING_CB_THRESHOLD", None)
            else:
                os.environ["BILLING_CB_THRESHOLD"] = prev_threshold
            if prev_pause is None:
                os.environ.pop("BILLING_CB_PAUSE", None)
            else:
                os.environ["BILLING_CB_PAUSE"] = prev_pause
            importlib.reload(runtime)

    def test_distinct_log_event_actions(self):
        """Rollback and billing CBs log distinct event actions."""
        import inspect
        src = inspect.getsource(runtime)
        self.assertIn('"circuit_breaker_triggered"', src)
        self.assertIn('"billing_cb_triggered"', src)

    def test_both_cbs_can_be_active_simultaneously(self):
        """Independent state — arming one CB does not disarm the other."""
        # Arm the billing CB by setting its throttled-until timestamp.
        import time as _time
        with runtime._lock:
            prev_billing = runtime._billing_throttled_until
            prev_consecutive = runtime._consecutive_rollbacks
            runtime._billing_throttled_until = _time.monotonic() + 120
            runtime._consecutive_rollbacks = 2  # one below CB threshold
        try:
            status = runtime.get_status()
            self.assertTrue(status["billing_throttled"])
            self.assertEqual(status["consecutive_rollbacks"], 2)
        finally:
            with runtime._lock:
                runtime._billing_throttled_until = prev_billing
                runtime._consecutive_rollbacks = prev_consecutive

    def test_cb_pause_durations_distinct(self):
        """Rollback CB pause (300s) differs from billing CB pause (120s default)."""
        import importlib, os
        prev_threshold = os.environ.get("BILLING_CB_THRESHOLD")
        prev_pause = os.environ.get("BILLING_CB_PAUSE")
        try:
            os.environ.pop("BILLING_CB_THRESHOLD", None)
            os.environ.pop("BILLING_CB_PAUSE", None)
            reloaded = importlib.reload(runtime)
            self.assertEqual(reloaded._CIRCUIT_BREAKER_PAUSE, 300)
            self.assertEqual(reloaded._BILLING_CB_PAUSE, 120)
        finally:
            if prev_threshold is not None:
                os.environ["BILLING_CB_THRESHOLD"] = prev_threshold
            if prev_pause is not None:
                os.environ["BILLING_CB_PAUSE"] = prev_pause
            importlib.reload(runtime)


if __name__ == "__main__":
    unittest.main()
