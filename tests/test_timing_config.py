"""Tests for the centralized timing config layer (modules/delay/config.py)."""
import importlib
import os
import unittest
from unittest.mock import patch


class TestDefaultConstantsValid(unittest.TestCase):
    def test_default_constants_valid(self):
        """validate_config() must not raise with default values."""
        from modules.delay.config import validate_config
        validate_config()  # should not raise


class TestEnvOverrides(unittest.TestCase):
    def test_env_override_min_typing_delay(self):
        """DELAY_MIN_TYPING_DELAY env var propagates to config.MIN_TYPING_DELAY."""
        import modules.delay.config as config
        with patch.dict(os.environ, {"DELAY_MIN_TYPING_DELAY": "0.1"}):
            importlib.reload(config)
            try:
                self.assertAlmostEqual(config.MIN_TYPING_DELAY, 0.1)
            finally:
                importlib.reload(config)  # restore defaults

    def test_env_override_max_step_delay(self):
        """DELAY_MAX_STEP_DELAY env var propagates to config.MAX_STEP_DELAY."""
        import modules.delay.config as config
        with patch.dict(os.environ, {"DELAY_MAX_STEP_DELAY": "6.0"}):
            importlib.reload(config)
            try:
                self.assertAlmostEqual(config.MAX_STEP_DELAY, 6.0)
            finally:
                importlib.reload(config)  # restore defaults


class TestInvalidConfigRaises(unittest.TestCase):
    def test_invalid_typing_delay_order_raises(self):
        """MIN_TYPING_DELAY > MAX_TYPING_DELAY must raise ValueError."""
        import modules.delay.config as config
        original_min = config.MIN_TYPING_DELAY
        try:
            config.MIN_TYPING_DELAY = 2.0  # > MAX_TYPING_DELAY (1.8)
            with self.assertRaises(ValueError) as ctx:
                config.validate_config()
            self.assertIn("MIN_TYPING_DELAY", str(ctx.exception))
        finally:
            config.MIN_TYPING_DELAY = original_min

    def test_invalid_watchdog_invariant_raises(self):
        """MAX_STEP_DELAY + WATCHDOG_HEADROOM > 10.0 must raise ValueError."""
        import modules.delay.config as config
        original_msd = config.MAX_STEP_DELAY
        original_wh = config.WATCHDOG_HEADROOM
        try:
            config.MAX_STEP_DELAY = 8.0
            config.WATCHDOG_HEADROOM = 3.0
            with self.assertRaises(ValueError) as ctx:
                config.validate_config()
            self.assertIn("WATCHDOG", str(ctx.exception))
        finally:
            config.MAX_STEP_DELAY = original_msd
            config.WATCHDOG_HEADROOM = original_wh

    def test_invalid_typo_rate_raises(self):
        """TYPO_RATE_MIN > TYPO_RATE_MAX must raise ValueError."""
        import modules.delay.config as config
        original_min = config.TYPO_RATE_MIN
        try:
            config.TYPO_RATE_MIN = 0.10  # > TYPO_RATE_MAX (0.05)
            with self.assertRaises(ValueError) as ctx:
                config.validate_config()
            self.assertIn("TYPO_RATE", str(ctx.exception))
        finally:
            config.TYPO_RATE_MIN = original_min


class TestConfigImportedByEngine(unittest.TestCase):
    def test_config_imported_by_engine(self):
        """DelayEngine must use MAX_STEP_DELAY from config, not a hardcoded literal."""
        import modules.delay.engine as engine_mod
        import modules.delay.config as config_mod
        # The values must match
        self.assertEqual(engine_mod.MAX_STEP_DELAY, config_mod.MAX_STEP_DELAY)
        self.assertEqual(engine_mod.MAX_HESITATION_DELAY, config_mod.MAX_HESITATION_DELAY)
        self.assertEqual(engine_mod.WATCHDOG_HEADROOM, config_mod.WATCHDOG_HEADROOM)
        # Verify no local constant definition in engine source
        import inspect
        source = inspect.getsource(engine_mod)
        self.assertNotIn("MAX_STEP_DELAY = ", source)

    def test_config_imported_by_persona(self):
        """PersonaProfile must use MIN_TYPING_DELAY from config, not a hardcoded literal."""
        import modules.delay.persona as persona_mod
        import modules.delay.config as config_mod
        self.assertEqual(persona_mod.MIN_TYPING_DELAY, config_mod.MIN_TYPING_DELAY)
        self.assertEqual(persona_mod.MAX_TYPING_DELAY, config_mod.MAX_TYPING_DELAY)
        # Verify no local constant definition in persona source
        import inspect
        source = inspect.getsource(persona_mod)
        self.assertNotIn("MIN_TYPING_DELAY = ", source)


class TestWatchdogInvariant(unittest.TestCase):
    def test_watchdog_invariant_max_step_plus_headroom(self):
        from modules.delay.config import MAX_STEP_DELAY, WATCHDOG_HEADROOM
        _WATCHDOG_TIMEOUT = 10.0
        self.assertLessEqual(
            MAX_STEP_DELAY + WATCHDOG_HEADROOM,
            _WATCHDOG_TIMEOUT,
            f"MAX_STEP_DELAY({MAX_STEP_DELAY}) + WATCHDOG_HEADROOM({WATCHDOG_HEADROOM})"
            f" must be <= _WATCHDOG_TIMEOUT({_WATCHDOG_TIMEOUT})",
        )


if __name__ == "__main__":
    unittest.main()
