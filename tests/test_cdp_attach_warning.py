"""Tests for attach-aware CDP listener probe warnings."""
import logging
import os
import unittest
from unittest.mock import patch

from integration.runtime import _is_attach_mode, probe_cdp_listener_support


class _DriverMissingListener:
    """Driver without add_cdp_listener."""


class _DriverWithListener:
    """Driver with callable add_cdp_listener."""

    def add_cdp_listener(self, *args, **kwargs):  # pragma: no cover
        """Mock listener registration; no-op."""


class _AttachDriver:
    """Driver with debuggerAddress capabilities, as in Chrome attach mode."""

    capabilities = {
        "goog:chromeOptions": {
            "debuggerAddress": "127.0.0.1:9222",
        },
    }


class TestCdpAttachWarning(unittest.TestCase):
    """Behavior matrix for attach-aware probe messages."""

    def setUp(self):
        self._prev_fallback = os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)
        self._prev_pool_mode = os.environ.pop("BITBROWSER_POOL_MODE", None)

    def tearDown(self):
        os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)
        os.environ.pop("BITBROWSER_POOL_MODE", None)
        if self._prev_fallback is not None:
            os.environ["ALLOW_DOM_ONLY_WATCHDOG"] = self._prev_fallback
        if self._prev_pool_mode is not None:
            os.environ["BITBROWSER_POOL_MODE"] = self._prev_pool_mode

    def test_listener_present_returns_true(self):
        self.assertTrue(probe_cdp_listener_support(_DriverWithListener()))

    def test_attach_mode_with_fallback_returns_false(self):
        with patch.dict(os.environ, {"ALLOW_DOM_ONLY_WATCHDOG": "1"}):
            with self.assertLogs("integration.runtime", level=logging.WARNING) as cm:
                result = probe_cdp_listener_support(_AttachDriver())

        self.assertFalse(result)
        joined = "\n".join(cm.output)
        self.assertIn("BitBrowser/attach driver", joined)
        self.assertIn("expected in attach mode", joined)
        self.assertIn("ALLOW_DOM_ONLY_WATCHDOG=1", joined)
        self.assertNotIn("re-install selenium-wire", joined)

    def test_attach_mode_without_fallback_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            probe_cdp_listener_support(_AttachDriver())

        message = str(ctx.exception)
        self.assertIn("BitBrowser/attach driver", message)
        self.assertIn("expected in attach mode", message)
        self.assertIn("ALLOW_DOM_ONLY_WATCHDOG=1", message)
        self.assertNotIn("selenium-wire", message)

    def test_local_mode_with_fallback_returns_false(self):
        with patch.dict(os.environ, {"ALLOW_DOM_ONLY_WATCHDOG": "1"}):
            with self.assertLogs("integration.runtime", level=logging.WARNING) as cm:
                result = probe_cdp_listener_support(_DriverMissingListener())

        self.assertFalse(result)
        joined = "\n".join(cm.output)
        self.assertIn("ALLOW_DOM_ONLY_WATCHDOG=1", joined)
        self.assertIn("DOM-polling fallback", joined)
        self.assertIn("local-launched Selenium", joined)
        self.assertIn("selenium-wire==5.1.0", joined)

    def test_local_mode_without_fallback_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            probe_cdp_listener_support(_DriverMissingListener())

        message = str(ctx.exception)
        self.assertIn("Install selenium-wire==5.1.0", message)
        self.assertIn("local-launched Selenium", message)
        self.assertIn("ALLOW_DOM_ONLY_WATCHDOG=1", message)

    def test_attach_mode_hint_overrides_env_heuristic(self):
        with patch.dict(os.environ, {"BITBROWSER_POOL_MODE": "1"}):
            self.assertFalse(
                _is_attach_mode(_DriverMissingListener(), hint=False)
            )

    def test_attach_mode_prefers_capabilities_over_env_heuristic(self):
        with patch.dict(os.environ, {"BITBROWSER_POOL_MODE": "0"}):
            self.assertTrue(_is_attach_mode(_AttachDriver()))

    def test_attach_mode_env_heuristic(self):
        with patch.dict(os.environ, {"BITBROWSER_POOL_MODE": "1"}):
            self.assertTrue(_is_attach_mode(_DriverMissingListener()))


if __name__ == "__main__":
    unittest.main()
