"""Tests for close_extra_tabs (C2 — Tab Janitor)."""
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, URL_GEO_CHECK, close_extra_tabs


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


class TestTabJanitorWiredBeforeGeoCheck(unittest.TestCase):
    """Verify that _run_tab_janitor executes before geo check in production path."""

    def test_janitor_runs_before_geo_check(self):
        """close_extra_tabs and about:blank navigate before get(URL_GEO_CHECK)."""
        selenium = MagicMock()
        selenium.window_handles = ["H0", "H1"]
        body_el = MagicMock()
        body_el.text = '{"country": "US"}'
        selenium.find_element.return_value = body_el

        call_order = []

        original_close_extra_tabs = close_extra_tabs

        def tracking_close_extra_tabs(drv):
            call_order.append("close_extra_tabs")
            return original_close_extra_tabs(drv)

        def tracking_get(url):
            call_order.append(("get", url))

        selenium.get.side_effect = tracking_get

        gd = GivexDriver(selenium)

        with patch("modules.cdp.driver.close_extra_tabs", side_effect=tracking_close_extra_tabs), \
             patch("time.sleep"):
            gd.preflight_geo_check()

        self.assertIn("close_extra_tabs", call_order)
        self.assertIn(("get", "about:blank"), call_order)
        self.assertIn(("get", URL_GEO_CHECK), call_order)

        janitor_idx = call_order.index("close_extra_tabs")
        blank_idx = call_order.index(("get", "about:blank"))
        geo_idx = call_order.index(("get", URL_GEO_CHECK))

        self.assertLess(janitor_idx, geo_idx, "close_extra_tabs must run before geo check")
        self.assertLess(blank_idx, geo_idx, "about:blank navigate must run before geo check")

    def test_janitor_sleep_called_before_geo_check(self):
        """time.sleep(2) is called by _run_tab_janitor before get(URL_GEO_CHECK)."""
        selenium = MagicMock()
        selenium.window_handles = ["H0"]
        body_el = MagicMock()
        body_el.text = '{"country": "US"}'
        selenium.find_element.return_value = body_el

        sleep_calls = []

        def tracking_sleep(secs):
            sleep_calls.append(secs)

        gd = GivexDriver(selenium)
        with patch("time.sleep", side_effect=tracking_sleep):
            gd.preflight_geo_check()

        self.assertIn(2, sleep_calls, "time.sleep(2) must be called by _run_tab_janitor")
        get_calls = [c[0][0] for c in selenium.get.call_args_list]
        self.assertIn(URL_GEO_CHECK, get_calls)


if __name__ == "__main__":
    unittest.main()
