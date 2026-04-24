"""P3-D3 — bounding_box_click strict mode must raise ClickDispatchError on
all four fallback branches instead of silently falling back to Selenium's
plain ``.click()`` (which would emit an ``isTrusted=false`` mouse event
detectable by anti-fraud heuristics).

Parametrised over the four failure modes:
  1. getBoundingClientRect raises
  2. rect is falsy (None / empty dict)
  3. rnd helper is unavailable
  4. CDP dispatch itself fails

Plus a regression test confirming that non-strict mode still performs the
plain ``.click()`` fallback — we must not silently break any caller that
opts out of strict mode.
"""

import random
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver
from modules.common.exceptions import ClickDispatchError


def _make_driver(rect=None, script_raises=False):
    drv = MagicMock()
    drv.find_elements.return_value = [MagicMock()]
    if script_raises:
        drv.execute_script.side_effect = RuntimeError("script failed")
    else:
        drv.execute_script.return_value = rect
    return drv


class BoundingBoxClickStrictRaises(unittest.TestCase):
    """Strict mode must raise ClickDispatchError on all four fallback branches."""

    def _run_and_expect_raise(self, gd, drv):
        with patch.object(gd, "_ghost_move_to"):
            with self.assertRaises(ClickDispatchError):
                gd.bounding_box_click("#btn")
        # Selenium .click() must NEVER be called in strict mode.
        drv.find_elements.return_value[0].click.assert_not_called()

    def test_strict_raises_on_rect_missing_exception(self):
        """Branch 1: getBoundingClientRect raises → ClickDispatchError."""
        drv = _make_driver(script_raises=True)
        gd = GivexDriver(drv)  # strict=True by default
        gd._rnd = random.Random(0)
        self._run_and_expect_raise(gd, drv)

    def test_strict_raises_on_rect_falsy(self):
        """Branch 2: rect is falsy (None) → ClickDispatchError."""
        drv = _make_driver(rect=None)
        gd = GivexDriver(drv)
        gd._rnd = random.Random(0)
        self._run_and_expect_raise(gd, drv)

    def test_strict_raises_on_rnd_missing(self):
        """Branch 3: _rnd is None → ClickDispatchError."""
        rect = {"left": 10, "top": 20, "width": 200, "height": 80}
        drv = _make_driver(rect=rect)
        gd = GivexDriver(drv)
        gd._rnd = None
        self._run_and_expect_raise(gd, drv)

    def test_strict_raises_on_cdp_dispatch_failure(self):
        """Branch 4: Input.dispatchMouseEvent raises → ClickDispatchError."""
        rect = {"left": 10, "top": 20, "width": 200, "height": 80}
        drv = _make_driver(rect=rect)
        drv.execute_cdp_cmd.side_effect = RuntimeError("cdp boom")
        gd = GivexDriver(drv)
        gd._rnd = random.Random(0)
        self._run_and_expect_raise(gd, drv)


class BoundingBoxClickNonStrictStillFallsBack(unittest.TestCase):
    """Regression: non-strict mode MUST still perform the .click() fallback."""

    def test_non_strict_falls_back_on_rect_missing(self):
        drv = _make_driver(script_raises=True)
        gd = GivexDriver(drv, strict=False)
        gd._rnd = random.Random(0)
        with patch.object(gd, "_ghost_move_to"):
            gd.bounding_box_click("#btn")
        drv.find_elements.return_value[0].click.assert_called_once()

    def test_non_strict_falls_back_on_rect_falsy(self):
        drv = _make_driver(rect=None)
        gd = GivexDriver(drv, strict=False)
        gd._rnd = random.Random(0)
        with patch.object(gd, "_ghost_move_to"):
            gd.bounding_box_click("#btn")
        drv.find_elements.return_value[0].click.assert_called_once()

    def test_non_strict_falls_back_on_rnd_missing(self):
        rect = {"left": 10, "top": 20, "width": 200, "height": 80}
        drv = _make_driver(rect=rect)
        gd = GivexDriver(drv, strict=False)
        gd._rnd = None
        with patch.object(gd, "_ghost_move_to"):
            gd.bounding_box_click("#btn")
        drv.find_elements.return_value[0].click.assert_called_once()

    def test_non_strict_falls_back_on_cdp_dispatch_failure(self):
        rect = {"left": 10, "top": 20, "width": 200, "height": 80}
        drv = _make_driver(rect=rect)
        drv.execute_cdp_cmd.side_effect = RuntimeError("cdp boom")
        gd = GivexDriver(drv, strict=False)
        gd._rnd = random.Random(0)
        with patch.object(gd, "_ghost_move_to"):
            gd.bounding_box_click("#btn")
        drv.find_elements.return_value[0].click.assert_called_once()


if __name__ == "__main__":
    unittest.main()
