"""P1-3 E2E tests: "Thank you" popup detection — multi-language + shadow DOM.

Covers:
  - EN and VN pattern detection (all patterns, body text)
  - URL-based confirmation detection
  - Shadow-DOM traversal (shadow_root=True path)
  - Log trace verification (debug messages on match / no-match)
  - Error / graceful-fallback paths
  - Pattern-constant sanity checks (non-empty, lowercase, combined set)

This is the clean, conflict-free implementation of issue P1-3 on branch
fix/P1-3-thank-you-popup.  It complements the orchestrator-level tests in
test_orchestrator_p12_clear_refill.py without duplicating them.
"""
# pylint: disable=protected-access
import logging
import unittest
from unittest.mock import MagicMock

from modules.cdp.driver import (
    SEL_CONFIRMATION_EL,
    THANK_YOU_TEXT_PATTERNS_DEFAULT,
    THANK_YOU_TEXT_PATTERNS_EN,
    THANK_YOU_TEXT_PATTERNS_VN,
    URL_CONFIRM_FRAGMENTS,
    detect_popup_thank_you,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_driver(url: str = "", body_text: str = "", script_return: str = ""):
    """Build a minimal mock driver pair (wrapper + base) for unit tests.

    Args:
        url: Value returned by base.current_url.
        body_text: Text returned by the <body> element.
        script_return: String returned by base.execute_script (shadow DOM).

    Returns:
        (wrapper, base) — wrapper has ``._driver = base``.
    """
    base = MagicMock()
    base.current_url = url
    body_el = MagicMock()
    body_el.text = body_text
    base.find_element.return_value = body_el
    base.execute_script.return_value = script_return

    wrapper = MagicMock()
    wrapper._driver = base
    return wrapper, base


# ---------------------------------------------------------------------------
# 1 — Pattern-constant sanity checks
# ---------------------------------------------------------------------------

class TestThankYouPatternConstants(unittest.TestCase):
    """Validate that EN/VN Thank You pattern constants satisfy invariants."""

    def test_en_patterns_non_empty(self):
        self.assertGreater(len(THANK_YOU_TEXT_PATTERNS_EN), 0)

    def test_vn_patterns_non_empty(self):
        self.assertGreater(len(THANK_YOU_TEXT_PATTERNS_VN), 0)

    def test_default_includes_all_en_patterns(self):
        for pat in THANK_YOU_TEXT_PATTERNS_EN:
            with self.subTest(pat=pat):
                self.assertIn(pat, THANK_YOU_TEXT_PATTERNS_DEFAULT)

    def test_default_includes_all_vn_patterns(self):
        for pat in THANK_YOU_TEXT_PATTERNS_VN:
            with self.subTest(pat=pat):
                self.assertIn(pat, THANK_YOU_TEXT_PATTERNS_DEFAULT)

    def test_all_patterns_lowercase(self):
        for pat in THANK_YOU_TEXT_PATTERNS_DEFAULT:
            with self.subTest(pat=pat):
                self.assertEqual(pat, pat.lower(), f"Pattern not lowercase: {pat!r}")

    def test_url_confirm_fragments_non_empty(self):
        self.assertGreater(len(URL_CONFIRM_FRAGMENTS), 0)

    def test_url_fragments_are_strings(self):
        for frag in URL_CONFIRM_FRAGMENTS:
            with self.subTest(frag=frag):
                self.assertIsInstance(frag, str)


# ---------------------------------------------------------------------------
# 2 — URL-based detection
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouURL(unittest.TestCase):
    """Verify URL-fragment-based detection path."""

    def test_each_confirm_fragment_triggers_true(self):
        for frag in URL_CONFIRM_FRAGMENTS:
            with self.subTest(frag=frag):
                wrapper, _ = _make_driver(url=f"https://store.example.com{frag}")
                self.assertTrue(detect_popup_thank_you(wrapper))

    def test_fragment_embedded_in_longer_url_triggers_true(self):
        wrapper, _ = _make_driver(
            url="https://store.example.com/order-confirmation?id=12345"
        )
        self.assertTrue(detect_popup_thank_you(wrapper))

    def test_unrelated_url_does_not_trigger(self):
        wrapper, _ = _make_driver(url="https://store.example.com/payment.html")
        self.assertFalse(detect_popup_thank_you(wrapper))

    def test_empty_url_falls_through_to_text_check(self):
        # Empty URL → fall through; body contains a match → True
        wrapper, _ = _make_driver(
            url="",
            body_text="thank you for your order",
        )
        self.assertTrue(detect_popup_thank_you(wrapper))


# ---------------------------------------------------------------------------
# 3 — Body-text EN detection
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouEN(unittest.TestCase):
    """Verify that all English thank-you patterns are detected in body text."""

    def test_each_en_pattern_detected(self):
        for pat in THANK_YOU_TEXT_PATTERNS_EN:
            with self.subTest(pat=pat):
                wrapper, _ = _make_driver(
                    url="https://store.example.com/payment.html",
                    body_text=f"Transaction complete. {pat.capitalize()}. Please keep this receipt.",
                )
                self.assertTrue(
                    detect_popup_thank_you(wrapper),
                    f"Expected True for EN pattern: {pat!r}",
                )

    def test_en_pattern_case_insensitive_match(self):
        """Uppercase body text should still match (lowercased before comparison)."""
        wrapper, _ = _make_driver(
            url="https://store.example.com/payment.html",
            body_text="THANK YOU FOR YOUR ORDER — PLEASE CHECK YOUR EMAIL.",
        )
        self.assertTrue(detect_popup_thank_you(wrapper))

    def test_partial_en_phrase_does_not_match(self):
        """A substring that is NOT a full pattern should not match."""
        wrapper, _ = _make_driver(
            url="https://store.example.com/payment.html",
            body_text="thank you",  # not a full pattern
        )
        self.assertFalse(detect_popup_thank_you(wrapper))


# ---------------------------------------------------------------------------
# 4 — Body-text VN detection
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouVN(unittest.TestCase):
    """Verify that all Vietnamese thank-you patterns are detected in body text."""

    def test_each_vn_pattern_detected(self):
        for pat in THANK_YOU_TEXT_PATTERNS_VN:
            with self.subTest(pat=pat):
                wrapper, _ = _make_driver(
                    url="https://store.example.com/payment.html",
                    body_text=f"Giao dịch của bạn đã hoàn thành. {pat}.",
                )
                self.assertTrue(
                    detect_popup_thank_you(wrapper),
                    f"Expected True for VN pattern: {pat!r}",
                )

    def test_vn_pattern_mixed_with_english_ui_text(self):
        """Page with Vietnamese success phrase embedded in English UI matches."""
        wrapper, _ = _make_driver(
            url="https://store.example.com/payment.html",
            body_text="Cart | Checkout | thanh toán thành công | Back to home",
        )
        self.assertTrue(detect_popup_thank_you(wrapper))


# ---------------------------------------------------------------------------
# 5 — Shadow-DOM traversal (P1-3 core requirement)
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouShadowDOM(unittest.TestCase):
    """Verify shadow-DOM traversal path (shadow_root=True).

    The shadow text is returned by execute_script; body text is empty so we
    can isolate the shadow-root code path.
    """

    def _make_shadow_driver(self, shadow_text: str, selector: str = SEL_CONFIRMATION_EL):
        """Driver mock where body is empty but execute_script returns shadow text."""
        wrapper, base = _make_driver(
            url="https://store.example.com/payment.html",
            body_text="",
            script_return=shadow_text,
        )
        return wrapper, base

    def test_en_pattern_in_shadow_dom_detected(self):
        for pat in THANK_YOU_TEXT_PATTERNS_EN:
            with self.subTest(pat=pat):
                wrapper, _ = self._make_shadow_driver(
                    shadow_text=f"Custom web component — {pat} — footer text"
                )
                self.assertTrue(
                    detect_popup_thank_you(wrapper, shadow_root=True),
                    f"shadow_root=True must detect EN pattern: {pat!r}",
                )

    def test_vn_pattern_in_shadow_dom_detected(self):
        for pat in THANK_YOU_TEXT_PATTERNS_VN:
            with self.subTest(pat=pat):
                wrapper, _ = self._make_shadow_driver(
                    shadow_text=f"Nội dung xác nhận. {pat}. Cảm ơn bạn."
                )
                self.assertTrue(
                    detect_popup_thank_you(wrapper, shadow_root=True),
                    f"shadow_root=True must detect VN pattern: {pat!r}",
                )

    def test_shadow_dom_not_checked_when_flag_false(self):
        """When shadow_root=False (default), execute_script should NOT be called."""
        wrapper, base = self._make_shadow_driver(
            shadow_text="thank you for your order"
        )
        # Body text is empty → default path returns False without calling execute_script
        result = detect_popup_thank_you(wrapper, shadow_root=False)
        self.assertFalse(result)
        base.execute_script.assert_not_called()

    def test_shadow_dom_uses_correct_selector(self):
        """execute_script should receive the expected CSS selector argument."""
        custom_sel = ".confirmation-banner"
        wrapper, base = self._make_shadow_driver(
            shadow_text="thank you for your order",
            selector=custom_sel,
        )
        detect_popup_thank_you(wrapper, shadow_root=True, selector=custom_sel)
        call_args = base.execute_script.call_args
        # Second positional argument to execute_script is the selector
        self.assertEqual(call_args[0][1], custom_sel)

    def test_shadow_dom_default_selector_is_confirmation_el(self):
        """Without explicit selector, the confirmation-element selector is used."""
        wrapper, base = self._make_shadow_driver(
            shadow_text="order confirmed"
        )
        detect_popup_thank_you(wrapper, shadow_root=True)
        call_args = base.execute_script.call_args
        self.assertEqual(call_args[0][1], SEL_CONFIRMATION_EL)

    def test_shadow_dom_raises_returns_false_gracefully(self):
        """execute_script raising must not propagate — returns False."""
        wrapper, base = _make_driver(
            url="https://store.example.com/payment.html",
            body_text="",
        )
        base.execute_script.side_effect = Exception("JS error")
        result = detect_popup_thank_you(wrapper, shadow_root=True)
        self.assertFalse(result)

    def test_body_match_takes_priority_over_shadow_dom(self):
        """Body text matching should return True before shadow DOM is checked."""
        wrapper, base = _make_driver(
            url="https://store.example.com/payment.html",
            body_text="order confirmed",
            script_return="",  # shadow DOM is empty
        )
        self.assertTrue(detect_popup_thank_you(wrapper, shadow_root=True))
        # execute_script must NOT have been called (body match short-circuits)
        base.execute_script.assert_not_called()


# ---------------------------------------------------------------------------
# 6 — Mixed multi-language (EN + VN on same page)
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouMultiLang(unittest.TestCase):
    """Verify correct detection when EN and VN text appears on the same page."""

    def test_page_with_both_languages_triggers_true(self):
        wrapper, _ = _make_driver(
            url="https://store.example.com/payment.html",
            body_text=(
                "Your transaction is complete. "
                "thank you for your purchase. "
                "Cảm ơn bạn đã đặt hàng."
            ),
        )
        self.assertTrue(detect_popup_thank_you(wrapper))

    def test_custom_single_language_patterns_override_default(self):
        """Passing explicit patterns must override the default EN+VN set."""
        wrapper, _ = _make_driver(
            url="https://store.example.com/payment.html",
            body_text="bestellung erfolgreich",  # German — not in default set
        )
        self.assertTrue(
            detect_popup_thank_you(wrapper, patterns=("bestellung erfolgreich",))
        )
        # With default patterns, should NOT match
        self.assertFalse(detect_popup_thank_you(wrapper))


# ---------------------------------------------------------------------------
# 7 — Error-handling / edge cases
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouEdgeCases(unittest.TestCase):
    """Verify graceful behaviour on DOM / driver errors."""

    def test_returns_false_on_empty_page(self):
        wrapper, _ = _make_driver(url="https://store.example.com/payment.html")
        self.assertFalse(detect_popup_thank_you(wrapper))

    def test_current_url_raises_falls_through_to_body_text(self):
        class _BrokenURLDriver:
            @property
            def current_url(self):
                raise AttributeError("no URL available")

            def find_element(self, by, value, *args, **kwargs):  # pylint: disable=unused-argument
                m = MagicMock()
                m.text = "order confirmed"
                return m

            execute_script = MagicMock(return_value="")

        result = detect_popup_thank_you(_BrokenURLDriver())
        self.assertTrue(result)

    def test_find_element_body_raises_returns_false(self):
        base = MagicMock()
        base.current_url = "https://store.example.com/payment.html"
        base.find_element.side_effect = Exception("DOM not ready")
        self.assertFalse(detect_popup_thank_you(base))

    def test_raw_driver_without_wrapper_works(self):
        """Passing a raw (unwrapped) driver without ._driver attribute."""

        class _RawDriver:
            """Minimal Selenium-like driver without a ._driver wrapper layer."""

            current_url = "https://store.example.com/payment.html"

            def find_element(self, by, value, *args, **kwargs):  # pylint: disable=unused-argument
                m = MagicMock()
                m.text = "payment successful"
                return m

            execute_script = MagicMock(return_value="")

        # Note: pass _RawDriver instance directly — no ._driver wrapper
        self.assertTrue(detect_popup_thank_you(_RawDriver()))


# ---------------------------------------------------------------------------
# 8 — Log trace / coverage verification
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouLogTrace(unittest.TestCase):
    """Verify that debug log messages are emitted on match and no-match paths."""

    def _capture_logs(self, level=logging.DEBUG):
        """Context manager that captures log records from modules.cdp.driver."""
        logger = logging.getLogger("modules.cdp.driver")
        orig_level = logger.level
        logger.setLevel(level)
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        cap = _Capture()
        logger.addHandler(cap)
        return records, cap, logger, orig_level

    def _cleanup_logs(self, logger, cap, orig_level):
        logger.removeHandler(cap)
        logger.setLevel(orig_level)

    def test_url_match_emits_debug_log(self):
        records, cap, logger, orig_level = self._capture_logs()
        try:
            wrapper, _ = _make_driver(url="https://store.example.com/order-confirmation")
            detect_popup_thank_you(wrapper)
        finally:
            self._cleanup_logs(logger, cap, orig_level)

        messages = [r.getMessage() for r in records]
        self.assertTrue(
            any("URL match" in m for m in messages),
            f"Expected 'URL match' in log messages; got: {messages}",
        )

    def test_body_text_match_emits_debug_log(self):
        records, cap, logger, orig_level = self._capture_logs()
        try:
            wrapper, _ = _make_driver(
                url="https://store.example.com/payment.html",
                body_text="order confirmed",
            )
            detect_popup_thank_you(wrapper)
        finally:
            self._cleanup_logs(logger, cap, orig_level)

        messages = [r.getMessage() for r in records]
        self.assertTrue(
            any("MATCH" in m for m in messages),
            f"Expected 'MATCH' in log messages; got: {messages}",
        )

    def test_no_match_emits_no_signal_log(self):
        records, cap, logger, orig_level = self._capture_logs()
        try:
            wrapper, _ = _make_driver(
                url="https://store.example.com/payment.html",
                body_text="Please enter your credit card details.",
            )
            detect_popup_thank_you(wrapper)
        finally:
            self._cleanup_logs(logger, cap, orig_level)

        messages = [r.getMessage() for r in records]
        self.assertTrue(
            any("no thank-you signal" in m for m in messages),
            f"Expected 'no thank-you signal' in log messages; got: {messages}",
        )

    def test_shadow_dom_match_emits_shadow_dom_log(self):
        records, cap, logger, orig_level = self._capture_logs()
        try:
            wrapper, _ = _make_driver(
                url="https://store.example.com/payment.html",
                body_text="",
                script_return="order confirmed",
            )
            detect_popup_thank_you(wrapper, shadow_root=True)
        finally:
            self._cleanup_logs(logger, cap, orig_level)

        messages = [r.getMessage() for r in records]
        self.assertTrue(
            any("shadow-DOM MATCH" in m for m in messages),
            f"Expected 'shadow-DOM MATCH' in log messages; got: {messages}",
        )


if __name__ == "__main__":
    unittest.main()
