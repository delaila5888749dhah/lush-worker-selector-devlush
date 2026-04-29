"""Tests for close_extra_tabs (C2 — Tab Janitor)."""
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, URL_GEO_CHECK, close_extra_tabs


class _FakeDriver:
    def __init__(self, handles, close_error_on=None, urls=None):
        self.window_handles = list(handles)
        self.switch_to = MagicMock()
        self._close_error_on = close_error_on
        self._current = handles[0] if handles else None
        self.switched = []
        self.closed = []
        # Per-handle URL map; defaults to "" (treated as real content).
        self._urls = urls or {}

        def _switch(h):
            self._current = h
            self.switched.append(h)

        self.switch_to.window.side_effect = _switch

    @property
    def current_url(self):
        return self._urls.get(self._current, "")

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
        # Switched to probe each handle, then closed H1/H2/H3, then back to H0.
        self.assertIn("H0", drv.switched)
        self.assertEqual(drv.switched[-1], "H0")

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

    def test_close_extra_tabs_empty_handles_noop(self):
        drv = _FakeDriver([])
        self.assertEqual(close_extra_tabs(drv), 0)

    def test_close_extra_tabs_handles0_is_devtools_keeps_real_tab(self):
        """When handles[0] is a devtools:// target, keep the first real tab."""
        urls = {
            "DT": "devtools://devtools/bundled/devtools_app.html?targetType=tab",
            "REAL1": "https://example.com/",
            "REAL2": "about:blank",
        }
        drv = _FakeDriver(["DT", "REAL1", "REAL2"], urls=urls)
        closed = close_extra_tabs(drv)
        # Only REAL2 should be closed; DT is internal and REAL1 is the main.
        self.assertEqual(closed, 1)
        self.assertIn("REAL2", drv.closed)
        self.assertNotIn("REAL1", drv.closed)
        self.assertNotIn("DT", drv.closed)
        # Final switch back to the real main tab.
        self.assertEqual(drv.switched[-1], "REAL1")

    def test_close_extra_tabs_all_devtools_returns_zero_with_warning(self):
        """When all handles are devtools://, return 0 and log a warning."""
        urls = {
            "DT1": "devtools://devtools/bundled/inspector.html",
            "DT2": "devtools://devtools/bundled/inspector.html",
        }
        drv = _FakeDriver(["DT1", "DT2"], urls=urls)
        with self.assertLogs("modules.cdp.driver", level="WARNING") as cm:
            closed = close_extra_tabs(drv)
        self.assertEqual(closed, 0)
        self.assertEqual(drv.closed, [])
        self.assertTrue(any("no real content windows" in msg for msg in cm.output))

    def test_close_extra_tabs_chrome_scheme_treated_as_internal(self):
        """chrome:// handles are treated as internal and never closed."""
        urls = {
            "CHROME": "chrome://newtab/",
            "REAL": "https://example.com/",
        }
        drv = _FakeDriver(["CHROME", "REAL"], urls=urls)
        closed = close_extra_tabs(drv)
        # REAL is selected as main; CHROME is internal so nothing is closed.
        self.assertEqual(closed, 0)
        self.assertNotIn("CHROME", drv.closed)
        self.assertNotIn("REAL", drv.closed)
        self.assertEqual(drv.switched[-1], "REAL")


class TestTabJanitorWiredBeforeGeoCheck(unittest.TestCase):
    """Verify that _run_tab_janitor executes before geo check in production path."""

    def test_janitor_runs_before_geo_check(self):
        """close_extra_tabs and about:blank navigate before get(URL_GEO_CHECK)."""
        selenium = MagicMock()
        selenium.window_handles = ["H0", "H1"]
        selenium.current_url = "https://example.com/"
        body_el = MagicMock()
        body_el.text = '{"country": "US"}'
        selenium.find_element.return_value = body_el

        call_order = []

        def tracking_get(url):
            """Record each browser navigation in call order."""
            call_order.append(("get", url))

        selenium.get.side_effect = tracking_get

        givex_driver = GivexDriver(selenium)

        with patch("modules.cdp.driver.close_extra_tabs", wraps=close_extra_tabs) as mock_janitor, \
             patch("time.sleep"):
            givex_driver.preflight_geo_check()

        mock_janitor.assert_called_once()
        self.assertIn(("get", "about:blank"), call_order)
        self.assertIn(("get", URL_GEO_CHECK), call_order)

        blank_idx = call_order.index(("get", "about:blank"))
        geo_idx = call_order.index(("get", URL_GEO_CHECK))

        self.assertLess(blank_idx, geo_idx, "about:blank navigate must run before geo check")

    def test_janitor_sleep_called_before_geo_check(self):
        """time.sleep(2) is called by _run_tab_janitor before get(URL_GEO_CHECK)."""
        selenium = MagicMock()
        selenium.window_handles = ["H0"]
        selenium.current_url = "https://example.com/"
        body_el = MagicMock()
        body_el.text = '{"country": "US"}'
        selenium.find_element.return_value = body_el

        sleep_calls = []

        def tracking_sleep(secs):
            """Record each janitor sleep duration."""
            sleep_calls.append(secs)

        givex_driver = GivexDriver(selenium)
        with patch("time.sleep", side_effect=tracking_sleep):
            givex_driver.preflight_geo_check()

        self.assertIn(2, sleep_calls, "time.sleep(2) must be called by _run_tab_janitor")
        get_calls = [c[0][0] for c in selenium.get.call_args_list]
        self.assertIn(URL_GEO_CHECK, get_calls)


if __name__ == "__main__":
    unittest.main()
