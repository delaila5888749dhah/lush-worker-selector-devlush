"""Tests for TransientMonitor wiring in GivexDriver.submit_purchase.

Covers Blueprint §6 Fork 3 follow-up (Issue #194): the active-poll monitor
must be started before the submit click and cancelled in a finally block so
that late-appearing VBV/3DS iframes visible *during* the submit window are
captured and logged via the monitor metric path.
"""

import time
import threading
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, SEL_COMPLETE_PURCHASE, SEL_VBV_IFRAME
from modules.common.exceptions import SelectorTimeoutError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_selenium_mock(has_purchase_button=True):
    """Minimal Selenium mock whose find_elements optionally returns a button."""
    d = MagicMock()
    d.current_url = "https://example.com/checkout"
    btn = MagicMock()

    def _find_elements(_method, selector):
        if has_purchase_button and selector.strip() == SEL_COMPLETE_PURCHASE.strip():
            return [btn]
        return []

    d.find_elements.side_effect = _find_elements
    d.execute_script.return_value = {
        "left": 100, "top": 200, "width": 120, "height": 40,
    }
    return d


def _make_driver(has_purchase_button=True):
    """Build a GivexDriver with stubbed hesitation and deterministic RNG."""
    selenium = _make_selenium_mock(has_purchase_button)
    gd = GivexDriver(selenium)
    rnd = MagicMock()
    rnd.uniform.return_value = 0.0
    gd._rnd = rnd
    gd._hesitate_before_submit = MagicMock()
    return gd, selenium


# ---------------------------------------------------------------------------
# Wiring unit tests (TransientMonitor is mocked)
# ---------------------------------------------------------------------------

class TestSubmitPurchaseTransientMonitorWiring(unittest.TestCase):
    """TransientMonitor is correctly wired into submit_purchase (Issue #194)."""

    @patch("modules.monitor.main.TransientMonitor")
    def test_monitor_instantiated_on_submit(self, MockMonitor):
        """TransientMonitor must be constructed during submit_purchase."""
        gd, _ = _make_driver()
        gd.submit_purchase()
        MockMonitor.assert_called_once()

    @patch("modules.monitor.main.TransientMonitor")
    def test_monitor_started_on_submit(self, MockMonitor):
        """TransientMonitor.start() must be called during submit_purchase."""
        mock_instance = MockMonitor.return_value
        gd, _ = _make_driver()
        gd.submit_purchase()
        mock_instance.start.assert_called_once()

    @patch("modules.monitor.main.TransientMonitor")
    def test_monitor_cancelled_after_click(self, MockMonitor):
        """monitor.cancel() must be called after bounding_box_click returns."""
        mock_instance = MockMonitor.return_value
        gd, _ = _make_driver()
        gd.submit_purchase()
        mock_instance.cancel.assert_called_once()

    @patch("modules.monitor.main.TransientMonitor")
    def test_monitor_cancelled_even_when_click_raises(self, MockMonitor):
        """monitor.cancel() must be called even if bounding_box_click raises."""
        mock_instance = MockMonitor.return_value
        # No purchase button → bounding_box_click raises SelectorTimeoutError.
        gd, _ = _make_driver(has_purchase_button=False)
        with self.assertRaises(SelectorTimeoutError):
            gd.submit_purchase()
        mock_instance.cancel.assert_called_once()

    @patch("modules.monitor.main.TransientMonitor")
    def test_monitor_interval_is_half_second(self, MockMonitor):
        """TransientMonitor must be constructed with interval=0.5 s."""
        captured = {}

        def record_init(detector, interval=0.5, on_detect=None):
            captured["interval"] = interval
            return MagicMock()

        MockMonitor.side_effect = record_init
        gd, _ = _make_driver()
        gd.submit_purchase()
        self.assertAlmostEqual(captured.get("interval", -1), 0.5)

    @patch("modules.monitor.main.TransientMonitor")
    def test_monitor_detector_uses_vbv_iframe_selector(self, MockMonitor):
        """The injected detector must probe find_elements for VBV iframe."""
        captured_detector = []

        def record_init(detector, interval=0.5, on_detect=None):
            captured_detector.append(detector)
            return MagicMock()

        MockMonitor.side_effect = record_init
        gd, selenium = _make_driver()
        gd.submit_purchase()

        self.assertEqual(len(captured_detector), 1, "TransientMonitor was not instantiated")
        detector = captured_detector[0]

        # Reconfigure selenium so VBV iframe selector returns a result.
        first_vbv = SEL_VBV_IFRAME.split(",")[0].strip()

        def _find_with_vbv(_method, selector):
            return [MagicMock()] if selector.strip() == first_vbv else []

        selenium.find_elements.side_effect = _find_with_vbv
        self.assertTrue(detector(), "detector must return True when VBV iframe present")

        # Detector must return False when no VBV iframe is present.
        selenium.find_elements.side_effect = lambda _m, _s: []
        self.assertFalse(detector(), "detector must return False when no VBV iframe")

    @patch("modules.monitor.main.TransientMonitor")
    def test_monitor_start_before_click(self, MockMonitor):
        """monitor.start() must be called before the submit click fires."""
        mock_instance = MockMonitor.return_value
        call_order = []

        mock_instance.start.side_effect = lambda: call_order.append("start")

        gd, selenium = _make_driver()

        def record_click(cmd, params):
            if (
                cmd == "Input.dispatchMouseEvent"
                and params.get("type") == "mousePressed"
            ):
                call_order.append("click")

        selenium.execute_cdp_cmd.side_effect = record_click
        gd.submit_purchase()

        self.assertIn("start", call_order)
        if "click" in call_order:
            self.assertLess(
                call_order.index("start"),
                call_order.index("click"),
                "monitor.start() must precede the mousePressed event",
            )


# ---------------------------------------------------------------------------
# Functional tests (real TransientMonitor, fast interval)
# ---------------------------------------------------------------------------

class TestSubmitPurchaseVBVDetectionFunctional(unittest.TestCase):
    """Real TransientMonitor detects VBV iframe that appears during the submit
    click window (Issue #194 functional coverage)."""

    def setUp(self):
        from modules.monitor.main import reset as monitor_reset
        monitor_reset()

    def tearDown(self):
        from modules.monitor.main import reset as monitor_reset
        monitor_reset()

    def test_real_monitor_detects_vbv_during_submit(self):
        """VBV iframe visible during bounding_box_click must be logged by monitor."""
        from modules.monitor.main import TransientMonitor, get_metrics

        selenium = _make_selenium_mock(has_purchase_button=True)
        gd = GivexDriver(selenium)
        rnd = MagicMock()
        rnd.uniform.return_value = 0.0
        gd._rnd = rnd
        gd._hesitate_before_submit = MagicMock()

        appeared = threading.Event()
        first_vbv = SEL_VBV_IFRAME.split(",")[0].strip()

        def _find_elements_side_effect(_method, selector):
            sel = selector.strip()
            if sel == SEL_COMPLETE_PURCHASE.strip():
                return [MagicMock()]
            if sel == first_vbv and appeared.is_set():
                return [MagicMock()]
            return []

        selenium.find_elements.side_effect = _find_elements_side_effect

        # Replace bounding_box_click with a stub that sets VBV visible and
        # sleeps briefly so the monitor (fast interval) can poll and detect.
        def _fake_click(selector):
            appeared.set()       # VBV "appears" as soon as click fires
            time.sleep(0.3)      # Hold open the window for monitor to poll

        gd.bounding_box_click = _fake_click

        # Subclass with a fast interval so the test completes quickly.
        class _FastMonitor(TransientMonitor):
            def __init__(self, detector, interval=0.5, on_detect=None):
                super().__init__(detector, interval=0.05, on_detect=on_detect)

        with patch("modules.monitor.main.TransientMonitor", _FastMonitor):
            gd.submit_purchase()

        self.assertGreater(
            get_metrics()["vbv_detections"],
            0,
            "TransientMonitor did not record any VBV detection",
        )

    def test_no_false_detection_without_vbv(self):
        """Monitor must not record a detection when no VBV iframe is present."""
        from modules.monitor.main import TransientMonitor, get_metrics

        selenium = _make_selenium_mock(has_purchase_button=True)
        gd = GivexDriver(selenium)
        rnd = MagicMock()
        rnd.uniform.return_value = 0.0
        gd._rnd = rnd
        gd._hesitate_before_submit = MagicMock()

        # No VBV iframe — find_elements always returns [] for non-button selectors.

        class _FastMonitor(TransientMonitor):
            def __init__(self, detector, interval=0.5, on_detect=None):
                super().__init__(detector, interval=0.05, on_detect=on_detect)

        with patch("modules.monitor.main.TransientMonitor", _FastMonitor):
            gd.submit_purchase()

        self.assertEqual(
            get_metrics()["vbv_detections"],
            0,
            "Monitor must not record false VBV detections",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
