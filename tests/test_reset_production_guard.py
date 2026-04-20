"""M13 — reset() production guard."""
import unittest

from integration import runtime


class TestResetProductionGuard(unittest.TestCase):
    def setUp(self):
        # Force runtime into INIT so each test starts clean.
        with runtime._lock:
            runtime._state = "INIT"
            runtime._behavior_delay_enabled = True

    def tearDown(self):
        with runtime._lock:
            runtime._state = "INIT"
            runtime._behavior_delay_enabled = False

    def test_reset_raises_when_running_with_behavior(self):
        """reset() raises RuntimeError when state=RUNNING and delay enabled."""
        with runtime._lock:
            runtime._state = "RUNNING"
            runtime._behavior_delay_enabled = True
        with self.assertRaises(RuntimeError):
            runtime.reset()

    def test_reset_allowed_when_stopped(self):
        """reset() is allowed when the runtime is not running."""
        with runtime._lock:
            runtime._state = "STOPPED"
            runtime._behavior_delay_enabled = True
        runtime.reset()  # must not raise
        self.assertEqual(runtime.get_state(), "INIT")

    def test_reset_allowed_when_behavior_disabled(self):
        """reset() is allowed when behavior delay is disabled even if RUNNING.

        Disabled behavior delay is the test/CI mode; production must never
        disable it.
        """
        with runtime._lock:
            runtime._state = "RUNNING"
            runtime._behavior_delay_enabled = False
        runtime.reset()  # must not raise
        self.assertEqual(runtime.get_state(), "INIT")


if __name__ == "__main__":
    unittest.main()
