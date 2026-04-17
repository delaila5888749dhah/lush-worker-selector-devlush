"""Tests for modules/cdp/keyboard.py — CDP key dispatch, typo simulation.

Covers:
- adjacent_char: determinism, fallback for unknown chars, neighbor validity.
- type_value: CDP key dispatch, typo injection, correction cycle,
  burst delays, field-aware typo caps, determinism, strict-mode path.
"""

import random
import string
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.keyboard import (
    adjacent_char, type_value, _ADJACENT, _MAX_TYPO_RATE, _FIELD_TYPO_CAP,
    _DOM_CODE_MAP, _VK_MAP, _SHIFT_REQUIRED,
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

    def test_fallback_success_emits_warning(self):
        """CDP fail → send_keys success emits WARNING with fallback context."""
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        el = MagicMock()  # pylint: disable=invalid-name
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.keyboard", level="WARNING") as log_cm:
                type_value(drv, el, "x", _rnd(), strict=False)
        self.assertTrue(any("fell back to send_keys" in msg for msg in log_cm.output))

    def test_strict_warns_on_cdp_and_fallback_failure(self):
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        el = MagicMock()
        el.send_keys.side_effect = RuntimeError("fallback gone")
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.keyboard", level="WARNING") as cm:
                type_value(drv, el, "x", _rnd(), strict=True)
        self.assertTrue(any("dispatch completely failed" in msg for msg in cm.output))

    def test_non_strict_does_not_warn(self):
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        el = MagicMock()
        el.send_keys.side_effect = RuntimeError("fallback gone")
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.keyboard", level="DEBUG"):
                type_value(drv, el, "x", _rnd(), strict=False)


class TestTypeValueEngineIntegration(unittest.TestCase):
    """type_value routes delays through engine.accumulate_delay when provided."""

    def _make_engine(self, headroom=7.0):
        """Return a mock engine with configurable headroom."""
        eng = MagicMock()
        remaining = [headroom]
        def _accum(d):
            actual = min(d, max(0.0, remaining[0]))
            remaining[0] -= actual
            return actual
        eng.accumulate_delay.side_effect = _accum
        eng.is_delay_permitted.return_value = True
        return eng

    def test_engine_accumulates_per_char_delay(self):
        """Per-character delays are routed through engine.accumulate_delay."""
        drv = _mock_driver()
        el = MagicMock()
        eng = self._make_engine()
        with patch("time.sleep"):
            type_value(drv, el, "abc", _rnd(), typo_rate=0.0, engine=eng)
        # 3 chars × 1 accumulate_delay call each = 3 calls
        self.assertEqual(eng.accumulate_delay.call_count, 3)

    def test_engine_accumulates_typo_hesitation(self):
        """Typo hesitation delay also goes through engine.accumulate_delay."""
        drv = _mock_driver()
        el = MagicMock()
        eng = self._make_engine()
        rnd = _rnd(0)
        rnd.random = lambda: 0.0  # Force typo on every char
        with patch("time.sleep"):
            type_value(drv, el, "a", rnd, typo_rate=1.0,
                       field_kind="text", engine=eng)
        # Typo hesitation + per-char delay = at least 2 calls
        self.assertGreaterEqual(eng.accumulate_delay.call_count, 2)

    def test_engine_headroom_exhausted_stops_sleeping(self):
        """When engine headroom is exhausted, sleeps receive 0.0."""
        drv = _mock_driver()
        el = MagicMock()
        eng = self._make_engine(headroom=0.0)  # No headroom at all
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            type_value(drv, el, "abc", _rnd(), typo_rate=0.0, engine=eng)
        # All sleeps should be 0.0 since engine returns 0.0
        self.assertTrue(all(d == 0.0 for d in slept))

    def test_no_engine_uses_raw_delays(self):
        """Without engine, delays are used directly (backward compatible)."""
        drv = _mock_driver()
        el = MagicMock()
        delays = [0.1, 0.2]
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            type_value(drv, el, "ab", _rnd(), typo_rate=0.0,
                       delays=delays, engine=None)
        self.assertIn(0.1, slept)
        self.assertIn(0.2, slept)

    def test_engine_partial_headroom_clamps_delay(self):
        """Engine with limited headroom clamps delay to remaining budget."""
        drv = _mock_driver()
        el = MagicMock()
        eng = self._make_engine(headroom=0.08)  # Only 0.08s left
        delays = [0.1, 0.1]  # Each requests 0.1s
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            type_value(drv, el, "ab", _rnd(), typo_rate=0.0,
                       delays=delays, engine=eng)
        # First gets 0.08 (clamped), second gets 0.0 (exhausted)
        self.assertAlmostEqual(slept[0], 0.08, places=4)
        self.assertAlmostEqual(slept[1], 0.0, places=4)

    def test_engine_not_permitted_skips_all_delays(self):
        """When engine.is_delay_permitted() is False, all sleeps are 0.0."""
        drv = _mock_driver()
        el = MagicMock()
        eng = self._make_engine()
        eng.is_delay_permitted.return_value = False
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            type_value(drv, el, "abc", _rnd(), typo_rate=0.0, engine=eng)
        # All sleeps should be 0.0 since delay is not permitted
        self.assertTrue(all(d == 0.0 for d in slept))
        # accumulate_delay should NOT be called in a non-permitted context
        eng.accumulate_delay.assert_not_called()


class TestCDPKeyEventCodeField(unittest.TestCase):
    """_dispatch() sets correct 'code' field in CDP Input.dispatchKeyEvent."""

    def _cdp_events(self, ch):
        """Type a single character and return list of CDP event dicts dispatched."""
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            type_value(drv, el, ch, _rnd(), typo_rate=0.0)
        return [call[0][1] for call in drv.execute_cdp_cmd.call_args_list
                if call[0][0] == "Input.dispatchKeyEvent"]

    def test_code_not_empty_for_lowercase_letters(self):
        for ch in "abcdefghijklmnopqrstuvwxyz":
            for event in self._cdp_events(ch):
                self.assertNotEqual(event["code"], "", f"code empty for '{ch}'")

    def test_code_not_empty_for_uppercase_letters(self):
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            for event in self._cdp_events(ch):
                self.assertNotEqual(event["code"], "", f"code empty for '{ch}'")

    def test_code_not_empty_for_digits(self):
        for ch in "0123456789":
            for event in self._cdp_events(ch):
                self.assertNotEqual(event["code"], "", f"code empty for '{ch}'")

    def test_code_not_empty_for_special_chars(self):
        for ch in "!@#$%^&*()":
            for event in self._cdp_events(ch):
                self.assertNotEqual(event["code"], "", f"code empty for '{ch}'")

    def test_letter_code_matches_key_prefix(self):
        for ch in "aA":
            for event in self._cdp_events(ch):
                self.assertEqual(event["code"], "KeyA")

    def test_digit_code_matches_digit_prefix(self):
        for event in self._cdp_events('1'):
            self.assertEqual(event["code"], "Digit1")

    def test_space_code(self):
        for event in self._cdp_events(' '):
            self.assertEqual(event["code"], "Space")

    def test_period_code(self):
        for event in self._cdp_events('.'):
            self.assertEqual(event["code"], "Period")


class TestCDPWindowsVirtualKeyCode(unittest.TestCase):
    """_dispatch() sends correct windowsVirtualKeyCode for each character."""

    def _vk(self, ch):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            type_value(drv, el, ch, _rnd(), typo_rate=0.0)
        events = [call[0][1] for call in drv.execute_cdp_cmd.call_args_list
                  if call[0][0] == "Input.dispatchKeyEvent"]
        return events[0]["windowsVirtualKeyCode"] if events else None

    def test_vk_bang_is_49(self):
        self.assertEqual(self._vk('!'), 49)

    def test_vk_at_is_50(self):
        self.assertEqual(self._vk('@'), 50)

    def test_vk_uppercase_A_is_65(self):
        self.assertEqual(self._vk('A'), 65)

    def test_vk_lowercase_a_is_65(self):
        self.assertEqual(self._vk('a'), 65)

    def test_vk_digit_1_is_49(self):
        self.assertEqual(self._vk('1'), 49)

    def test_vk_underscore_is_189(self):
        self.assertEqual(self._vk('_'), 189)

    def test_vk_plus_is_187(self):
        self.assertEqual(self._vk('+'), 187)


class TestCDPModifiersAndIsKeypad(unittest.TestCase):
    """_dispatch() sets modifiers=8 for shifted chars, 0 for normal; isKeypad=False."""

    def _event(self, ch):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            type_value(drv, el, ch, _rnd(), typo_rate=0.0)
        events = [call[0][1] for call in drv.execute_cdp_cmd.call_args_list
                  if call[0][0] == "Input.dispatchKeyEvent"]
        return events[0] if events else {}

    def test_shift_modifier_for_exclamation(self):
        self.assertEqual(self._event('!')["modifiers"], 8)

    def test_shift_modifier_for_uppercase(self):
        self.assertEqual(self._event('A')["modifiers"], 8)

    def test_no_shift_for_lowercase(self):
        self.assertEqual(self._event('a')["modifiers"], 0)

    def test_no_shift_for_digit(self):
        self.assertEqual(self._event('5')["modifiers"], 0)

    def test_iskeypad_false(self):
        self.assertFalse(self._event('a')["isKeypad"])
        self.assertFalse(self._event('1')["isKeypad"])

    def test_all_printable_ascii_produce_string_code(self):
        """type_value on all printable ASCII never crashes; code is always str."""
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            type_value(drv, el, string.printable, _rnd(), typo_rate=0.0)
        for call in drv.execute_cdp_cmd.call_args_list:
            if call[0][0] == "Input.dispatchKeyEvent":
                self.assertIsInstance(call[0][1]["code"], str)


class TestCardCvvPiiMasking(unittest.TestCase):
    """Phase-5 gap: fallback log paths must mask per-character PAN/CVV digits.

    ``_dispatch`` logs ``ch`` on both the send_keys-fallback WARNING and
    the completely-failed DEBUG/WARNING path. For sensitive field kinds
    (``card_number``, ``cvv``), those characters must never appear in log
    records; instead they are masked to ``'*'``.
    """

    def _type_with_failing_cdp(self, field_kind, value, *, strict=False,
                               fail_fallback=False):
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        el = MagicMock()
        if fail_fallback:
            el.send_keys.side_effect = RuntimeError("fallback gone")
        with patch("time.sleep"):
            level = "DEBUG" if (fail_fallback and not strict) else "WARNING"
            with self.assertLogs("modules.cdp.keyboard", level=level) as cm:
                type_value(drv, el, value, _rnd(), typo_rate=0.0,
                           field_kind=field_kind, strict=strict)
        return cm.output

    def _assert_digits_masked(self, log_output, value):
        joined = "\n".join(log_output)
        for digit in set(value):
            # Allow the digit to appear only as part of strict=False/True
            # suffix; actual PAN chars must be redacted. Verify no quoted
            # digit literal (e.g. "'4'") leaks into the log line.
            self.assertNotIn(f"'{digit}'", joined,
                             f"PAN/CVV digit '{digit}' leaked into log")
        # A masked representation must be present for the fallback path.
        self.assertIn("'*'", joined)

    def test_card_number_fallback_log_masks_digits(self):
        pan = "4111111111111111"
        output = self._type_with_failing_cdp("card_number", pan)
        self._assert_digits_masked(output, pan)

    def test_cvv_fallback_log_masks_digits(self):
        cvv = "123"
        output = self._type_with_failing_cdp("cvv", cvv)
        self._assert_digits_masked(output, cvv)

    def test_card_number_total_failure_log_masks_digits(self):
        pan = "4111111111111111"
        output = self._type_with_failing_cdp(
            "card_number", pan, strict=True, fail_fallback=True,
        )
        self._assert_digits_masked(output, pan)

    def test_non_sensitive_field_preserves_char_in_log(self):
        """text/name fields keep char in log for debuggability."""
        drv = _mock_driver()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        el = MagicMock()
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.keyboard", level="WARNING") as cm:
                type_value(drv, el, "A", _rnd(), typo_rate=0.0,
                           field_kind="name")
        joined = "\n".join(cm.output)
        self.assertIn("'A'", joined)



    """amount field type cap is 0.0 — never inject typos."""

    def test_amount_has_zero_typo_cap(self):
        self.assertEqual(_FIELD_TYPO_CAP.get("amount"), 0.0)

    def test_amount_field_no_typos_at_high_rate(self):
        drv = _mock_driver()
        el = MagicMock()
        with patch("time.sleep"):
            result = type_value(drv, el, "99.99", _rnd(), typo_rate=1.0,
                                field_kind="amount")
        self.assertEqual(result["typos_injected"], 0)
        self.assertAlmostEqual(result["eff_typo_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
