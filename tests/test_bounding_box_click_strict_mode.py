"""Phase 3A Task 3 — bounding_box_click strict-mode gates all 4 branches.

Strict mode (the GivexDriver default) must raise :class:`CDPClickError`
on **every** fallback branch — rect fetch failure, zero-size/falsy rect,
missing persona RNG, and CDP dispatch failure — instead of silently
falling back to Selenium ``.click()`` (which emits ``isTrusted=False``
and defeats anti-detect).  Non-strict mode (``strict=False``) preserves
the legacy ``.click()`` fallback for test/debug contexts.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver
from modules.common.exceptions import CDPClickError


def _rect():
    return {"left": 100.0, "top": 200.0, "width": 50.0, "height": 30.0}


def _make_persona(seed=42):
    # Minimal persona stub that satisfies the driver-init contract:
    # both BehaviorStateMachine wiring and TemporalModel consult
    # ``persona._seed`` / ``persona._rnd``.
    import random
    persona = MagicMock()
    persona._seed = seed
    persona._rnd = random.Random(seed)
    persona.night_penalty_factor = 0.0
    persona.get_typo_probability.return_value = 0.0
    return persona


def _make_driver(rect=None):
    d = MagicMock()
    el = MagicMock()
    d.find_elements.return_value = [el]
    d.execute_script.return_value = rect if rect is not None else _rect()
    return d, el


class BoundingBoxClickStrictModeTests(unittest.TestCase):

    def test_bounding_box_click_default_is_strict(self):
        d, _ = _make_driver()
        gd = GivexDriver(d)
        self.assertTrue(gd._strict)  # pylint: disable=protected-access

    # Branch 1 — rect fetch raises
    def test_strict_raises_on_rect_fetch_failure(self):
        d, el = _make_driver()
        d.execute_script.side_effect = RuntimeError("getBoundingClientRect failed")
        gd = GivexDriver(d, persona=_make_persona(), strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    def test_non_strict_falls_back_on_rect_fetch_failure(self):
        d, el = _make_driver()
        d.execute_script.side_effect = RuntimeError("boom")
        gd = GivexDriver(d, persona=_make_persona(), strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#x")
        el.click.assert_called_once()

    # Branch 2 — zero-size rect
    def test_strict_raises_on_zero_size_rect(self):
        d, el = _make_driver(rect={"left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0})
        gd = GivexDriver(d, persona=_make_persona(), strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    def test_non_strict_falls_back_on_zero_size_rect(self):
        d, el = _make_driver(rect={"left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0})
        gd = GivexDriver(d, persona=_make_persona(), strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#x")
        el.click.assert_called_once()

    def test_strict_raises_on_falsy_rect(self):
        d, el = _make_driver()
        d.execute_script.return_value = None
        gd = GivexDriver(d, persona=_make_persona(), strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    def test_strict_raises_on_negative_width_rect(self):
        """CSS transform scale(-1) can yield negative width — must still raise in strict."""
        d, el = _make_driver(rect={"left": 10.0, "top": 10.0, "width": -40.0, "height": 20.0})
        gd = GivexDriver(d, persona=_make_persona(), strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    def test_strict_raises_on_negative_height_rect(self):
        d, el = _make_driver(rect={"left": 10.0, "top": 10.0, "width": 40.0, "height": -20.0})
        gd = GivexDriver(d, persona=_make_persona(), strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    def test_strict_raises_on_missing_rect_keys(self):
        """Rect missing required keys is treated as invalid."""
        d, el = _make_driver(rect={"left": 10.0, "top": 10.0})  # no width/height
        gd = GivexDriver(d, persona=_make_persona(), strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    # Branch 3 — persona RNG missing
    def test_strict_raises_on_missing_rng(self):
        d, el = _make_driver()
        gd = GivexDriver(d, persona=None, strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    def test_non_strict_falls_back_on_missing_rng(self):
        d, el = _make_driver()
        gd = GivexDriver(d, persona=None, strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#x")
        el.click.assert_called_once()

    # Branch 4 — CDP dispatch failure
    def test_strict_raises_on_cdp_dispatch_failure(self):
        d, el = _make_driver()
        d.execute_cdp_cmd.side_effect = RuntimeError("dispatch failed")
        gd = GivexDriver(d, persona=_make_persona(), strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#x")
        el.click.assert_not_called()

    def test_non_strict_falls_back_on_cdp_dispatch_failure(self):
        d, el = _make_driver()
        d.execute_cdp_cmd.side_effect = RuntimeError("dispatch failed")
        gd = GivexDriver(d, persona=_make_persona(), strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#x")
        el.click.assert_called_once()

    def test_non_strict_falls_back_in_all_4_branches(self):
        """Regression: strict=False preserves legacy .click() fallback everywhere."""
        scenarios = [
            # (setup_fn, description)
            (lambda d: setattr(d, "execute_script", MagicMock(side_effect=RuntimeError("rect"))),
             "rect_fetch"),
            (lambda d: setattr(d, "execute_script", MagicMock(return_value={"left": 0, "top": 0, "width": 0, "height": 0})),
             "zero_rect"),
        ]
        for setup, name in scenarios:
            with self.subTest(branch=name):
                d, el = _make_driver()
                setup(d)
                gd = GivexDriver(d, persona=_make_persona(), strict=False)
                with patch("time.sleep"):
                    gd.bounding_box_click("#x")
                el.click.assert_called_once()

        # Missing RNG (branch 3): persona=None
        d, el = _make_driver()
        gd = GivexDriver(d, persona=None, strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#x")
        el.click.assert_called_once()

        # CDP dispatch failure (branch 4)
        d, el = _make_driver()
        d.execute_cdp_cmd.side_effect = RuntimeError("cdp")
        gd = GivexDriver(d, persona=_make_persona(), strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#x")
        el.click.assert_called_once()


if __name__ == "__main__":
    unittest.main()
