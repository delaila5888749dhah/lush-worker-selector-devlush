"""Phase 3A Task 3 — ``bounding_box_click`` strict mode on ALL fallback branches.

Verifies that every soft-fallback path in :meth:`GivexDriver.bounding_box_click`
is gated by ``self._strict``: in strict mode (the default), each path raises
:class:`CDPClickError` instead of falling back to Selenium native ``.click()``
(which would emit ``isTrusted=False`` and degrade anti-fraud quality).

Audit finding [D3].
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver
from modules.common.exceptions import CDPClickError


def _make_driver():
    d = MagicMock()
    d.current_url = "https://example.com"
    d.find_elements.return_value = [MagicMock()]
    return d


def _make_persona(seed: int = 42):
    """Build a real PersonaProfile with fixed seed for deterministic tests."""
    from modules.delay.persona import PersonaProfile  # noqa: PLC0415
    return PersonaProfile(seed)


class TestBoundingBoxClickStrictMode(unittest.TestCase):

    @staticmethod
    def _rect():
        return {"left": 100.0, "top": 200.0, "width": 80.0, "height": 30.0}

    # ── Branch 1: rect fetch failure ──────────────────────────────────────
    def test_bounding_box_click_strict_raises_on_rect_fetch_failure(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.side_effect = RuntimeError("rect boom")
        persona = _make_persona()
        gd = GivexDriver(selenium, persona=persona, strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#el")
        element.click.assert_not_called()

        # Non-strict: falls back to .click()
        selenium2 = _make_driver()
        element2 = MagicMock()
        selenium2.find_elements.return_value = [element2]
        selenium2.execute_script.side_effect = RuntimeError("rect boom")
        gd2 = GivexDriver(selenium2, persona=persona, strict=False)
        with patch("time.sleep"):
            gd2.bounding_box_click("#el")
        element2.click.assert_called_once()

    # ── Branch 2: zero-size / missing rect ────────────────────────────────
    def test_bounding_box_click_strict_raises_on_zero_size_rect(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = {"left": 0, "top": 0, "width": 0, "height": 0}
        persona = _make_persona()
        gd = GivexDriver(selenium, persona=persona, strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#el")
        element.click.assert_not_called()

    # ── Branch 3: persona RNG missing ─────────────────────────────────────
    def test_bounding_box_click_strict_raises_on_missing_rng(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        # No persona → self._rnd is None.
        gd = GivexDriver(selenium, strict=True)
        self.assertIsNone(gd._rnd)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#el")
        element.click.assert_not_called()

    # ── Branch 4: CDP dispatch failure ────────────────────────────────────
    def test_bounding_box_click_strict_raises_on_cdp_dispatch_failure(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        selenium.execute_cdp_cmd.side_effect = RuntimeError("cdp boom")
        persona = _make_persona()
        gd = GivexDriver(selenium, persona=persona, strict=True)
        with patch("time.sleep"):
            with self.assertRaises(CDPClickError):
                gd.bounding_box_click("#el")
        element.click.assert_not_called()

    # ── Non-strict regression: all 4 branches still fall back ─────────────
    def test_bounding_box_click_non_strict_falls_back_in_all_4_branches(self):
        scenarios = []

        def _scenario_rect_failure():
            sel = _make_driver()
            el = MagicMock()
            sel.find_elements.return_value = [el]
            sel.execute_script.side_effect = RuntimeError("boom")
            return sel, el

        def _scenario_zero_rect():
            sel = _make_driver()
            el = MagicMock()
            sel.find_elements.return_value = [el]
            sel.execute_script.return_value = {"left": 0, "top": 0, "width": 0, "height": 0}
            return sel, el

        def _scenario_no_rng():
            sel = _make_driver()
            el = MagicMock()
            sel.find_elements.return_value = [el]
            sel.execute_script.return_value = self._rect()
            return sel, el  # no persona at construction → _rnd is None

        def _scenario_cdp_dispatch_failure():
            sel = _make_driver()
            el = MagicMock()
            sel.find_elements.return_value = [el]
            sel.execute_script.return_value = self._rect()
            sel.execute_cdp_cmd.side_effect = RuntimeError("cdp boom")
            return sel, el

        scenarios = [
            ("rect_failure", _scenario_rect_failure, _make_persona()),
            ("zero_rect", _scenario_zero_rect, _make_persona()),
            ("no_rng", _scenario_no_rng, None),
            ("cdp_dispatch", _scenario_cdp_dispatch_failure, _make_persona()),
        ]

        for label, factory, persona in scenarios:
            with self.subTest(branch=label):
                selenium, element = factory()
                kwargs = {"strict": False}
                if persona is not None:
                    kwargs["persona"] = persona
                gd = GivexDriver(selenium, **kwargs)
                with patch("time.sleep"):
                    gd.bounding_box_click("#el")
                element.click.assert_called_once()

    def test_bounding_box_click_default_is_strict(self):
        """GivexDriver(...) without explicit strict=False defaults to strict."""
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        self.assertTrue(gd._strict)


if __name__ == "__main__":
    unittest.main()
