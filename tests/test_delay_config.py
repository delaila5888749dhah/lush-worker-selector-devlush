"""Focused tests for delay config validation invariants."""
import unittest

import modules.delay.config as config


class TestDelayConfigValidation(unittest.TestCase):
    """Delay config validate_config raises DelayConfigError on invariant violations."""

    def test_validate_config_raises_delay_config_error_for_step_budget_violation(self):
        """validate_config raises DelayConfigError when step-budget invariant is violated."""
        original_max_step_delay = config.MAX_STEP_DELAY
        original_watchdog_headroom = config.WATCHDOG_HEADROOM
        try:
            config.MAX_STEP_DELAY = 100.0
            config.WATCHDOG_HEADROOM = 3.0
            with self.assertRaises(config.DelayConfigError):
                config.validate_config()
        finally:
            config.MAX_STEP_DELAY = original_max_step_delay
            config.WATCHDOG_HEADROOM = original_watchdog_headroom


if __name__ == "__main__":
    unittest.main()
