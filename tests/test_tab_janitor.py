"""Tests for close_extra_tabs (C2 — Tab Janitor)."""
import unittest
from unittest.mock import MagicMock

from modules.cdp.driver import close_extra_tabs


class _FakeDriver:
    def __init__(self, handles, close_error_on=None):
        self.window_handles = list(handles)
        self.switch_to = MagicMock()
        self._close_error_on = close_error_on
        self._current = handles[0] if handles else None
        self.switched = []
        self.closed = []

        def _switch(h):
            self._current = h
            self.switched.append(h)

        self.switch_to.window.side_effect = _switch

    def close(self):
        self.closed.append(self._current)
        if self._close_error_on and self._current == self._close_error_on:
            raise RuntimeError("simulated close failure")


class TestCloseExtraTabs(unittest.TestCase):
    def test_close_extra_tabs_keeps_main(self):
        drv = _FakeDriver(["H0", "H1", "H2", "H3"])
        closed = close_extra_tabs(drv)
        self.assertEqual(closed, 3)
        self.assertEqual(drv.closed, ["H1", "H2", "H3"])
        # Switched to H1, H2, H3, then back to H0.
        self.assertEqual(drv.switched, ["H1", "H2", "H3", "H0"])

    def test_close_extra_tabs_handles_close_exception(self):
        drv = _FakeDriver(["H0", "H1", "H2"], close_error_on="H1")
        closed = close_extra_tabs(drv)
        # H1 failed, H2 closed => 1 closed.
        self.assertEqual(closed, 1)
        # Always switches back to main.
        self.assertEqual(drv.switched[-1], "H0")

    def test_close_extra_tabs_zero_extras_noop(self):
        drv = _FakeDriver(["only"])
        closed = close_extra_tabs(drv)
        self.assertEqual(closed, 0)
        self.assertEqual(drv.closed, [])
        self.assertEqual(drv.switched, [])

    def test_close_extra_tabs_empty_handles_noop(self):
        drv = _FakeDriver([])
        self.assertEqual(close_extra_tabs(drv), 0)


if __name__ == "__main__":
    unittest.main()
