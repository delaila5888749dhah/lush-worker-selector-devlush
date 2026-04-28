"""Tests for strict-mode dispatch policy in ``modules/cdp/keyboard.py``.

In strict mode, when CDP ``Input.dispatchKeyEvent`` fails, the keyboard
helper must raise :class:`modules.common.exceptions.CDPCommandError`
immediately rather than silently falling back to
``WebElement.send_keys``. The Selenium-native fallback emits events with
``isTrusted=False`` and is flaggable by anti-fraud heuristics — so it is
asymmetric with :meth:`bounding_box_click`'s strict-mode policy
(``CDPClickError``) which already raises.

In non-strict mode the legacy behavior (warn + ``send_keys`` fallback)
is preserved for backwards compatibility.
"""

import random
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.keyboard import type_value
from modules.common.exceptions import CDPCommandError, SessionFlaggedError


def _rnd(seed: int = 42) -> random.Random:
    return random.Random(seed)


class TestStrictDispatch(unittest.TestCase):
    """Strict-mode keyboard dispatch must raise on CDP failure."""

    def test_strict_raises_on_cdp_failure(self):
        """``strict=True`` + CDP failure → ``CDPCommandError``; no send_keys."""
        drv = MagicMock()
        cause = RuntimeError("card 4111111111111111 cvv=987 user@example.com")
        drv.execute_cdp_cmd.side_effect = cause
        el = MagicMock()
        with patch("time.sleep"):
            with self.assertRaises(CDPCommandError) as ctx:
                type_value(drv, el, "abc", _rnd(), strict=True)
        # Must be a subclass of SessionFlaggedError so the runtime treats
        # the failure as a flagged session (matching CDPClickError policy).
        self.assertIsInstance(ctx.exception, SessionFlaggedError)
        # Strict mode MUST NOT fall back to Selenium-native send_keys.
        el.send_keys.assert_not_called()
        # The error names the failing CDP method.
        self.assertEqual(ctx.exception.command, "Input.dispatchKeyEvent")
        self.assertIn("Input.dispatchKeyEvent", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, cause)
        self.assertNotIn("4111111111111111", ctx.exception.detail)
        self.assertNotIn("user@example.com", ctx.exception.detail)
        self.assertIn("[REDACTED-CARD]", ctx.exception.detail)
        self.assertIn("[REDACTED-CVV]", ctx.exception.detail)
        self.assertIn("[REDACTED-EMAIL]", ctx.exception.detail)

    def test_non_strict_falls_back(self):
        """``strict=False`` preserves legacy warn + send_keys fallback."""
        drv = MagicMock()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP unavailable")
        el = MagicMock()
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.keyboard", level="WARNING") as log_cm:
                result = type_value(drv, el, "ab", _rnd(), strict=False)
        # Each character was typed via the send_keys fallback.
        self.assertEqual(result["typed_chars"], 2)
        self.assertEqual(el.send_keys.call_count, 2)
        self.assertTrue(
            any("fell back to send_keys" in msg for msg in log_cm.output)
        )

    def test_strict_backspace_correction_also_raises(self):
        """The typo-correction backspace path also obeys strict mode.

        ``type_value`` dispatches ``_BACKSPACE`` after a wrong-key typo to
        emulate human correction (``modules/cdp/keyboard.py`` L146). That
        dispatch must raise in strict mode just like the regular char
        path — otherwise a CDP failure on the correction would silently
        degrade to ``isTrusted=False`` send_keys events.
        """
        drv = MagicMock()
        drv.execute_cdp_cmd.side_effect = RuntimeError("CDP unavailable")
        el = MagicMock()
        # Force the typo branch on every character so the correction
        # backspace path is exercised.
        rnd = _rnd(0)
        rnd.random = lambda: 0.0
        with patch("time.sleep"):
            with self.assertRaises(CDPCommandError):
                type_value(drv, el, "a", rnd, typo_rate=1.0,
                           field_kind="text", strict=True)
        el.send_keys.assert_not_called()


if __name__ == "__main__":
    unittest.main()
