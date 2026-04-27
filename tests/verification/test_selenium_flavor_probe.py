"""Unit tests for integration.runtime.probe_cdp_listener_support (U-06).

Cases:
 1. A driver with a callable add_cdp_listener passes silently.
 2. A driver without add_cdp_listener raises RuntimeError with the expected
    operator message.
 3. With ALLOW_DOM_ONLY_WATCHDOG=1, a driver without add_cdp_listener does
    NOT raise; the probe returns False and logs a WARNING describing the
    degraded mode (issue F2 audit).
"""
# pylint: disable=too-few-public-methods,no-self-use
import logging
import os
import unittest
from unittest.mock import patch

from integration.runtime import (
    is_dom_only_watchdog_allowed,
    probe_cdp_listener_support,
)


class _GoodDriver:
    """Mock driver with a callable add_cdp_listener."""

    def add_cdp_listener(self, event, callback):  # pragma: no cover
        """Mock listener registration; no-op."""


class _BadDriverMissing:
    """Mock driver without add_cdp_listener attribute."""


class _BadDriverNonCallable:
    """Mock driver where add_cdp_listener is not callable."""
    add_cdp_listener = "not-callable"


class TestProbeCdpListenerSupport(unittest.TestCase):
    """U-06 unit tests for the startup probe helper."""

    def setUp(self):
        # Ensure ALLOW_DOM_ONLY_WATCHDOG is not leaked from another test.
        self._prev_env = os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)

    def tearDown(self):
        os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)
        if self._prev_env is not None:
            os.environ["ALLOW_DOM_ONLY_WATCHDOG"] = self._prev_env

    def test_passes_for_callable_add_cdp_listener(self):
        """probe returns True when driver has callable add_cdp_listener."""
        self.assertTrue(probe_cdp_listener_support(_GoodDriver()))

    def test_raises_for_missing_attribute(self):
        """probe raises RuntimeError when add_cdp_listener is absent."""
        with self.assertRaises(RuntimeError) as ctx:
            probe_cdp_listener_support(_BadDriverMissing())
        self.assertIn("add_cdp_listener", str(ctx.exception))
        self.assertIn("selenium-wire", str(ctx.exception))

    def test_raises_for_non_callable_attribute(self):
        """probe raises RuntimeError when add_cdp_listener is not callable."""
        with self.assertRaises(RuntimeError) as ctx:
            probe_cdp_listener_support(_BadDriverNonCallable())
        self.assertIn("add_cdp_listener", str(ctx.exception))

    def test_error_message_mentions_pinned_version(self):
        """Error message must reference the pinned version so operators know what to install."""
        with self.assertRaises(RuntimeError) as ctx:
            probe_cdp_listener_support(_BadDriverMissing())
        self.assertIn("5.1.0", str(ctx.exception))

    def test_error_message_mentions_fallback_env(self):
        """Error message must point operators at the documented fallback opt-in."""
        with self.assertRaises(RuntimeError) as ctx:
            probe_cdp_listener_support(_BadDriverMissing())
        self.assertIn("ALLOW_DOM_ONLY_WATCHDOG", str(ctx.exception))

    def test_fallback_env_returns_false_with_warning(self):
        """ALLOW_DOM_ONLY_WATCHDOG=1 turns the missing-hook error into a WARNING."""
        with patch.dict(os.environ, {"ALLOW_DOM_ONLY_WATCHDOG": "1"}):
            with self.assertLogs("integration.runtime", level=logging.WARNING) as cm:
                result = probe_cdp_listener_support(_BadDriverMissing())
        self.assertFalse(result)
        joined = "\n".join(cm.output)
        self.assertIn("ALLOW_DOM_ONLY_WATCHDOG", joined)
        self.assertIn("Phase A", joined)
        self.assertIn("Phase C", joined)

    def test_fallback_env_accepts_true_yes(self):
        """ALLOW_DOM_ONLY_WATCHDOG accepts 1/true/yes (case-insensitive)."""
        for value in ("1", "true", "TRUE", "yes", "YES"):
            with patch.dict(os.environ, {"ALLOW_DOM_ONLY_WATCHDOG": value}):
                self.assertTrue(is_dom_only_watchdog_allowed(), f"value={value!r}")
                # And probe must not raise:
                self.assertFalse(probe_cdp_listener_support(_BadDriverMissing()))

    def test_fallback_env_unset_or_false_keeps_strict_mode(self):
        """Unset / 0 / false / no => strict mode (probe raises)."""
        for value in ("", "0", "false", "no"):
            env = {} if value == "" else {"ALLOW_DOM_ONLY_WATCHDOG": value}
            os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)
            with patch.dict(os.environ, env, clear=False):
                self.assertFalse(is_dom_only_watchdog_allowed(), f"value={value!r}")
                with self.assertRaises(RuntimeError):
                    probe_cdp_listener_support(_BadDriverMissing())


if __name__ == "__main__":
    unittest.main()
