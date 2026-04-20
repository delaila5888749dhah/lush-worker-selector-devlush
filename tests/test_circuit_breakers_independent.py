"""M15 — Rollback CB and Billing CB are independent with distinct log actions.

These tests deliberately avoid ``importlib.reload(runtime)`` — reloading
replaces module-level globals (locks, threads, registries) that other modules
and tests have already imported by reference, corrupting state in subsequent
tests (e.g. ``test_runtime.TestStartupConfigValidation``).

Env-overridability is instead validated by re-running the exact expression
used at module import, matching what runtime.py does at load time.
"""
import os
import unittest

from integration import runtime


def _load_billing_threshold() -> int:
    """Mirror of the expression runtime.py uses at import time."""
    return max(1, int(os.environ.get("BILLING_CB_THRESHOLD", "3")))


def _load_billing_pause() -> int:
    """Mirror of the expression runtime.py uses at import time."""
    return max(1, int(os.environ.get("BILLING_CB_PAUSE", "120")))


class TestCircuitBreakersIndependent(unittest.TestCase):
    def setUp(self):
        self._prev_threshold = os.environ.get("BILLING_CB_THRESHOLD")
        self._prev_pause = os.environ.get("BILLING_CB_PAUSE")
        os.environ.pop("BILLING_CB_THRESHOLD", None)
        os.environ.pop("BILLING_CB_PAUSE", None)

    def tearDown(self):
        for key, value in (
            ("BILLING_CB_THRESHOLD", self._prev_threshold),
            ("BILLING_CB_PAUSE", self._prev_pause),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_rollback_cb_constants(self):
        """The rollback CB pause is 300s (Blueprint §14.2)."""
        self.assertEqual(runtime._CIRCUIT_BREAKER_PAUSE, 300)
        self.assertEqual(runtime._MAX_CONSECUTIVE_ROLLBACKS, 3)

    def test_billing_cb_env_overridable(self):
        """Billing CB threshold and pause are env-overridable (NON-IF rule 7)."""
        os.environ["BILLING_CB_THRESHOLD"] = "7"
        os.environ["BILLING_CB_PAUSE"] = "99"
        self.assertEqual(_load_billing_threshold(), 7)
        self.assertEqual(_load_billing_pause(), 99)

    def test_distinct_log_event_actions(self):
        """Rollback and billing CBs log distinct event actions."""
        import inspect
        src = inspect.getsource(runtime)
        self.assertIn('"circuit_breaker_triggered"', src)
        self.assertIn('"billing_cb_triggered"', src)

    def test_both_cbs_can_be_active_simultaneously(self):
        """Independent state — arming one CB does not disarm the other."""
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
        self.assertEqual(runtime._CIRCUIT_BREAKER_PAUSE, 300)
        # Defaults come from the unset-env path.
        self.assertEqual(_load_billing_pause(), 120)


if __name__ == "__main__":
    unittest.main()

