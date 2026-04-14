"""Tests for modules/cdp/keyboard.py — CDP key dispatch, typo simulation.

Covers:
- adjacent_char: determinism, fallback for unknown chars, neighbor validity.
- type_value: CDP key dispatch, typo injection, correction cycle,
  burst delays, field-aware typo caps, determinism, strict-mode path.
"""

import random
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.keyboard import (
    adjacent_char, type_value, _ADJACENT, _BACKSPACE, _MAX_TYPO_RATE, _FIELD_TYPO_CAP,
)


def _rnd(seed: int = 42) -> random.Random:
    return random.Random(seed)


def _mock_driver():
    """Return a mock driver whose execute_cdp_cmd succeeds by default."""
    return MagicMock()


class TestAdjacentChar(unittest.TestCase):
    """adjacent_char returns a valid neighbor or the original char."""

    def test_known_char_returns_neighbor(self):
        neighbors = _ADJACENT['a']
        result = adjacent_char('a', _rnd())
        self.assertIn(result, neighbors)

    def test_unknown_char_returns_self(self):
        self.assertEqual(adjacent_char('@', _rnd()), '@')

    def test_digit_returns_neighbor(self):
        neighbors = _ADJACENT['5']
        result = adjacent_char('5', _rnd())
        self.assertIn(result, neighbors)

    def test_case_insensitive_lookup(self):
        neighbors = _ADJACENT['a']
        result = adjacent_char('A', _rnd())
        self.assertIn(result, neighbors)

    def test_deterministic_under_fixed_seed(self):
        r1 = adjacent_char('s', _rnd(0))
        r2 = adjacent_char('s', _rnd(0))
        self.assertEqual(r1, r2)

    def test_different_seeds_may_differ(self):
        results = {adjacent_char('f', _rnd(seed)) for seed in range(20)}
        self.assertGreater(len(results), 1)


class TestTypeValueBasic(unittest.TestCase):
    """type_value dispatches each character via CDP key events."""

    def test_chars_dispatched_per_character(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "hello", _rnd(), typo_rate=0.0)
        self.assertEqual(result["typed_chars"], 5)
        # Each char dispatches keyDown + keyUp = 2 CDP calls per char.
        self.assertGreaterEqual(drv.execute_cdp_cmd.call_count, 10)

    def test_returns_mode_cdp_key(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "abc", _rnd(), typo_rate=0.0)
        self.assertEqual(result["mode"], "cdp_key")

    def test_element_cleared_before_typing(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            type_value(drv, el, "x", _rnd(), typo_rate=0.0)
        el.clear.assert_called_once()

    def test_no_typos_when_typo_rate_zero(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "test123", _rnd(), typo_rate=0.0)
        self.assertEqual(result["typos_injected"], 0)
        self.assertEqual(result["corrections_made"], 0)

    def test_delays_used_when_provided(self):
        drv = _mock_driver()
        el = MagicMock()
        delays = [0.1, 0.2, 0.3]
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            type_value(drv, el, "abc", _rnd(), typo_rate=0.0, delays=delays)
        self.assertIn(0.1, slept)
        self.assertIn(0.2, slept)
        self.assertIn(0.3, slept)

    def test_fallback_delay_when_delays_none(self):
        drv = _mock_driver()
        el = MagicMock()
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            type_value(drv, el, "ab", _rnd(), typo_rate=0.0, delays=None)
        self.assertTrue(all(d == 0.05 for d in slept))

    def test_falls_back_to_send_keys_when_cdp_fails(self):
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP unavailable")
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "ab", _rnd(), typo_rate=0.0)
        self.assertEqual(result["typed_chars"], 2)
        # Falls back to send_keys.
        self.assertEqual(el.send_keys.call_count, 2)


class TestTypeValueTypo(unittest.TestCase):
    """type_value injects typo + correction cycle when typo triggers."""

    def _run_with_high_typo_rate(self, value="a"):
        drv = _mock_driver()
        el = MagicMock()
        # Use a rng that returns 0.0 so every char triggers a typo.
        rnd = _rnd(0)
        rnd.random = lambda: 0.0  # Force every char to trigger
        with patch("time.sleep"):
            result = type_value(drv, el, value, rnd, typo_rate=1.0, field_kind="text")
        return drv, el, result

    def test_typo_injected_at_rate_one(self):
        _drv, _el, result = self._run_with_high_typo_rate("a")
        self.assertGreater(result["typos_injected"], 0)

    def test_correction_follows_typo(self):
        _drv, _el, result = self._run_with_high_typo_rate("a")
        self.assertEqual(result["corrections_made"], result["typos_injected"])

    def test_deterministic_typo_under_fixed_seed(self):
        _, _, res1 = self._run_with_high_typo_rate("s")
        _, _, res2 = self._run_with_high_typo_rate("s")
        self.assertEqual(res1["typos_injected"], res2["typos_injected"])


class TestFieldAwareTypoPolicy(unittest.TestCase):
    """Field-kind parameter controls effective typo rate."""

    def test_cvv_has_zero_typo(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "123", _rnd(), typo_rate=1.0, field_kind="cvv")
        self.assertEqual(result["typos_injected"], 0)
        self.assertAlmostEqual(result["eff_typo_rate"], 0.0)

    def test_card_number_caps_typo_rate(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "4111111111111111", _rnd(),
                                typo_rate=0.5, field_kind="card_number")
        self.assertLessEqual(result["eff_typo_rate"], _FIELD_TYPO_CAP["card_number"])

    def test_text_respects_max_typo_rate(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "abc", _rnd(), typo_rate=0.99, field_kind="text")
        self.assertLessEqual(result["eff_typo_rate"], _MAX_TYPO_RATE)


class TestTypeValueBurstDelays(unittest.TestCase):
    """type_value uses burst-style delays when a delays list is provided."""

    def test_burst_delays_grouped(self):
        drv = _mock_driver()
        el = MagicMock()
        delays = [0.04] * 4 + [0.8] + [0.04] * 4 + [0.8] + [0.04] * 4 + [0.8] + [0.04] * 4
        value = "1234567890123456"
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            type_value(drv, el, value, _rnd(), typo_rate=0.0, delays=delays)
        long_delays = [d for d in slept if d >= 0.8]
        self.assertGreaterEqual(len(long_delays), 3)


class TestTypeValueStrictMode(unittest.TestCase):
    """type_value logs warning on failures when strict=True."""

    def test_strict_warns_on_cdp_and_fallback_failure(self):
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        el = MagicMock()
        el.send_keys.side_effect = RuntimeError("fallback gone")
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.keyboard", level="WARNING") as cm:
                type_value(drv, el, "x", _rnd(), strict=True)
        self.assertTrue(any("dispatch failed" in msg for msg in cm.output))

    def test_non_strict_does_not_warn(self):
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        el = MagicMock()
        el.send_keys.side_effect = RuntimeError("fallback gone")
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.keyboard", level="DEBUG"):
                type_value(drv, el, "x", _rnd(), strict=False)


if __name__ == "__main__":
    unittest.main()
