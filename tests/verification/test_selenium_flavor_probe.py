"""Unit tests for integration.runtime.probe_cdp_listener_support (U-06).

Two cases:
 1. A driver with a callable add_cdp_listener passes silently.
 2. A driver without add_cdp_listener raises RuntimeError with the expected
    operator message.
"""
import unittest

from integration.runtime import probe_cdp_listener_support


class _GoodDriver:
    """Mock driver with a callable add_cdp_listener."""

    def add_cdp_listener(self, event, callback):  # pragma: no cover
        pass


class _BadDriverMissing:
    """Mock driver without add_cdp_listener attribute."""


class _BadDriverNonCallable:
    """Mock driver where add_cdp_listener is not callable."""
    add_cdp_listener = "not-callable"


class TestProbeCdpListenerSupport(unittest.TestCase):

    def test_passes_for_callable_add_cdp_listener(self):
        """probe raises nothing when driver has callable add_cdp_listener."""
        probe_cdp_listener_support(_GoodDriver())  # must not raise

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


if __name__ == "__main__":
    unittest.main()
