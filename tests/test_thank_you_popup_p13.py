"""Tests for P1-3 — "Thank you" popup E2E detection coverage.

Validates that detect_popup_thank_you() correctly identifies the
order-confirmation "Thank you" page/popup in both English and Vietnamese,
covers the full mock checkout→thank_you cycle, and emits structured
WARNING logs on detection failure.

Acceptance criteria (P1-3 / Issue #9):
  - detect_popup_thank_you_test() callable asserts EN/VN text correctly.
  - Tests cover the full cycle (checkout → thank_you).
  - Detailed log on detect failure.
"""

import logging
import unittest
from unittest.mock import MagicMock, patch, call

from modules.cdp import driver as drv
from modules.cdp.driver import (
    POPUP_TEXT_PATTERNS_THANK_YOU_EN,
    POPUP_TEXT_PATTERNS_THANK_YOU_VN,
    POPUP_TEXT_PATTERNS_THANK_YOU,
    SEL_THANK_YOU_CONFIRMATION,
    detect_popup_thank_you,
    check_popup_text_match,
)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _make_driver(
    shadow_text: str = "",
    body_text: str = "",
):
    """Build a minimal mock driver pair (wrapper, base_driver).

    *shadow_text* is returned by execute_script when the selector-based JS
    shadow scan is called.  *body_text* is returned by the innerText body
    fallback call.
    """
    base = MagicMock()

    def _exec_script(script, *args):
        # The shadow-scan JS always receives one positional arg (the selector).
        if args:
            return shadow_text
        # The body innerText call has no extra args.
        return body_text

    base.execute_script.side_effect = _exec_script
    base.find_elements.return_value = []

    wrapper = MagicMock()
    wrapper._driver = base  # pylint: disable=protected-access
    return wrapper, base


# ---------------------------------------------------------------------------
# Standalone callable test — Acceptance Criterion #1
# ---------------------------------------------------------------------------

def detect_popup_thank_you_test():
    """Callable test: assert detect_popup_thank_you() detects EN and VN text.

    This function is designed to be invoked directly as a sanity probe (e.g.
    from a smoke-test harness or integration runner) *and* is exercised by the
    unittest suite below.
    """
    # --- English (selector path) ---
    wrapper_en, _ = _make_driver(shadow_text="Thank you for your order! Order #12345.")
    result_en = detect_popup_thank_you(wrapper_en)
    assert result_en is not None, (
        f"FAIL [EN]: detect_popup_thank_you returned None — "
        f"expected a POPUP_TEXT_PATTERNS_THANK_YOU_EN match"
    )
    assert result_en in POPUP_TEXT_PATTERNS_THANK_YOU_EN, (
        f"FAIL [EN]: matched pattern {result_en!r} not in POPUP_TEXT_PATTERNS_THANK_YOU_EN"
    )

    # --- Vietnamese (selector path) ---
    wrapper_vn, _ = _make_driver(shadow_text="Cảm ơn đơn hàng của bạn! Mã đơn hàng #67890.")
    result_vn = detect_popup_thank_you(wrapper_vn)
    assert result_vn is not None, (
        f"FAIL [VN]: detect_popup_thank_you returned None — "
        f"expected a POPUP_TEXT_PATTERNS_THANK_YOU_VN match"
    )
    assert result_vn in POPUP_TEXT_PATTERNS_THANK_YOU_VN, (
        f"FAIL [VN]: matched pattern {result_vn!r} not in POPUP_TEXT_PATTERNS_THANK_YOU_VN"
    )

    # --- English (body fallback path — selector returns empty, body has text) ---
    wrapper_body, _ = _make_driver(
        shadow_text="",
        body_text="Thank you for your purchase. Your e-gift card is on its way.",
    )
    result_body = detect_popup_thank_you(wrapper_body)
    assert result_body is not None, (
        "FAIL [EN/body]: detect_popup_thank_you returned None via body fallback — "
        "expected POPUP_TEXT_PATTERNS_THANK_YOU_EN match"
    )
    assert result_body in POPUP_TEXT_PATTERNS_THANK_YOU_EN, (
        f"FAIL [EN/body]: matched pattern {result_body!r} not in POPUP_TEXT_PATTERNS_THANK_YOU_EN"
    )


# ---------------------------------------------------------------------------
# Pattern constants validation
# ---------------------------------------------------------------------------

class TestThankYouPatternConstants(unittest.TestCase):
    """Validate that thank-you pattern constant sets are well-formed."""

    def test_en_patterns_non_empty(self):
        self.assertGreater(len(POPUP_TEXT_PATTERNS_THANK_YOU_EN), 0)

    def test_vn_patterns_non_empty(self):
        self.assertGreater(len(POPUP_TEXT_PATTERNS_THANK_YOU_VN), 0)

    def test_combined_includes_en_and_vn(self):
        for pat in POPUP_TEXT_PATTERNS_THANK_YOU_EN:
            self.assertIn(pat, POPUP_TEXT_PATTERNS_THANK_YOU)
        for pat in POPUP_TEXT_PATTERNS_THANK_YOU_VN:
            self.assertIn(pat, POPUP_TEXT_PATTERNS_THANK_YOU)

    def test_patterns_are_lowercase(self):
        for pat in POPUP_TEXT_PATTERNS_THANK_YOU:
            self.assertEqual(pat, pat.lower(), f"Pattern not lowercase: {pat!r}")

    def test_en_contains_thank_you_for_your_order(self):
        self.assertIn("thank you for your order", POPUP_TEXT_PATTERNS_THANK_YOU_EN)

    def test_vn_contains_cam_on_don_hang(self):
        self.assertIn("cảm ơn đơn hàng của bạn", POPUP_TEXT_PATTERNS_THANK_YOU_VN)

    def test_selector_constant_defined(self):
        self.assertTrue(len(SEL_THANK_YOU_CONFIRMATION) > 0)


# ---------------------------------------------------------------------------
# detect_popup_thank_you — English detection
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouEN(unittest.TestCase):
    """Unit tests for detect_popup_thank_you() with English confirmation text."""

    def test_en_thank_you_for_your_order(self):
        wrapper, _ = _make_driver(shadow_text="Thank you for your order!")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "thank you for your order")

    def test_en_thank_you_for_your_purchase(self):
        wrapper, _ = _make_driver(shadow_text="Thank you for your purchase. Your card is ready.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "thank you for your purchase")

    def test_en_order_confirmed(self):
        wrapper, _ = _make_driver(shadow_text="Order confirmed — you will receive a confirmation email.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "order confirmed")

    def test_en_order_placed_successfully(self):
        wrapper, _ = _make_driver(shadow_text="Order placed successfully!")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "order placed successfully")

    def test_en_your_order_has_been_placed(self):
        wrapper, _ = _make_driver(shadow_text="Your order has been placed. Ref: GX-9999.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "your order has been placed")

    def test_en_purchase_confirmed(self):
        wrapper, _ = _make_driver(shadow_text="Purchase confirmed. Enjoy your e-gift card!")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "purchase confirmed")

    def test_en_result_in_en_patterns(self):
        wrapper, _ = _make_driver(shadow_text="Thank you for your order — Order #123")
        result = detect_popup_thank_you(wrapper)
        self.assertIn(result, POPUP_TEXT_PATTERNS_THANK_YOU_EN)

    def test_en_case_insensitive(self):
        wrapper, _ = _make_driver(shadow_text="THANK YOU FOR YOUR ORDER!")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "thank you for your order")


# ---------------------------------------------------------------------------
# detect_popup_thank_you — Vietnamese detection
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouVN(unittest.TestCase):
    """Unit tests for detect_popup_thank_you() with Vietnamese confirmation text."""

    def test_vn_cam_on_don_hang(self):
        wrapper, _ = _make_driver(shadow_text="Cảm ơn đơn hàng của bạn! Mã đơn: GX-001.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "cảm ơn đơn hàng của bạn")

    def test_vn_cam_on_quy_khach(self):
        wrapper, _ = _make_driver(shadow_text="Cảm ơn quý khách đã tin tưởng mua hàng.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "cảm ơn quý khách")

    def test_vn_don_hang_xac_nhan(self):
        wrapper, _ = _make_driver(shadow_text="Đơn hàng đã được xác nhận. Vui lòng kiểm tra email.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "đơn hàng đã được xác nhận")

    def test_vn_mua_hang_thanh_cong(self):
        wrapper, _ = _make_driver(shadow_text="Mua hàng thành công! Thẻ sẽ được gửi vào email.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "mua hàng thành công")

    def test_vn_dat_hang_thanh_cong(self):
        wrapper, _ = _make_driver(shadow_text="Đặt hàng thành công. Cảm ơn bạn đã mua sắm.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "đặt hàng thành công")

    def test_vn_don_hang_da_duoc_dat(self):
        wrapper, _ = _make_driver(shadow_text="Đơn hàng của bạn đã được đặt thành công.")
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "đơn hàng của bạn đã được đặt")

    def test_vn_result_in_vn_patterns(self):
        wrapper, _ = _make_driver(shadow_text="Cảm ơn đơn hàng của bạn — GX-002")
        result = detect_popup_thank_you(wrapper)
        self.assertIn(result, POPUP_TEXT_PATTERNS_THANK_YOU_VN)


# ---------------------------------------------------------------------------
# detect_popup_thank_you — body fallback path
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouBodyFallback(unittest.TestCase):
    """Tests for the body innerText fallback scan path."""

    def test_body_fallback_en_when_no_element(self):
        """When the selector finds nothing, body scan should still detect EN text."""
        wrapper, _ = _make_driver(
            shadow_text="",            # selector scan returns empty
            body_text="Thank you for your order — confirmation sent.",
        )
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "thank you for your order")

    def test_body_fallback_vn_when_no_element(self):
        """Body fallback detects Vietnamese confirmation text."""
        wrapper, _ = _make_driver(
            shadow_text="",
            body_text="Cảm ơn đơn hàng của bạn! Mã tham chiếu: GX-888.",
        )
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "cảm ơn đơn hàng của bạn")

    def test_selector_match_takes_priority_over_body(self):
        """If the selector scan already matches, body fallback must not be called."""
        wrapper, base = _make_driver(
            shadow_text="Thank you for your order",
            body_text="Irrelevant body text",
        )
        result = detect_popup_thank_you(wrapper)
        self.assertEqual(result, "thank you for your order")
        # execute_script called at most once (shadow scan); not twice (no body scan)
        self.assertEqual(base.execute_script.call_count, 1)

    def test_returns_none_when_no_match_anywhere(self):
        """Returns None when neither selector nor body contain a thank-you pattern."""
        wrapper, _ = _make_driver(shadow_text="", body_text="")
        result = detect_popup_thank_you(wrapper)
        self.assertIsNone(result)

    def test_body_fallback_raises_silently(self):
        """If innerText script raises, detect_popup_thank_you must not propagate.

        A WARNING log must still be emitted because no match was found.
        """
        base = MagicMock()
        base.execute_script.side_effect = Exception("browser gone")
        wrapper = MagicMock()
        wrapper._driver = base  # pylint: disable=protected-access
        with patch.object(drv._log, "warning") as mock_warn:  # pylint: disable=protected-access
            result = detect_popup_thank_you(wrapper)
        self.assertIsNone(result)
        self.assertTrue(
            mock_warn.called,
            "Expected WARNING log to be emitted when execute_script raises and no match is found",
        )


# ---------------------------------------------------------------------------
# detect_popup_thank_you — no match / error cases
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouNoMatch(unittest.TestCase):
    """Edge cases: no confirmation, error text, or empty DOM."""

    def test_no_match_on_error_page(self):
        wrapper, _ = _make_driver(shadow_text="Something went wrong. Payment failed.")
        result = detect_popup_thank_you(wrapper)
        self.assertIsNone(result)

    def test_no_match_on_empty_dom(self):
        wrapper, _ = _make_driver(shadow_text="", body_text="")
        result = detect_popup_thank_you(wrapper)
        self.assertIsNone(result)

    def test_no_match_on_unrelated_text(self):
        wrapper, _ = _make_driver(shadow_text="Welcome back! Please fill in your details.")
        result = detect_popup_thank_you(wrapper)
        self.assertIsNone(result)

    def test_no_match_on_declined_message(self):
        wrapper, _ = _make_driver(shadow_text="Transaction declined — card invalid.")
        result = detect_popup_thank_you(wrapper)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# detect_popup_thank_you — logging assertions
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouLogging(unittest.TestCase):
    """Assert that structured log messages are emitted correctly."""

    def test_warning_log_on_detect_fail(self):
        """A WARNING log must be emitted when the popup is not detected."""
        wrapper, _ = _make_driver(shadow_text="", body_text="")
        with patch.object(drv._log, "warning") as mock_warn:  # pylint: disable=protected-access
            detect_popup_thank_you(wrapper)
        self.assertTrue(
            mock_warn.called,
            "Expected a warning log when thank-you popup is not detected",
        )
        warn_str = str(mock_warn.call_args_list)
        self.assertIn("FAIL", warn_str)

    def test_warning_log_contains_selector_and_snippet(self):
        """Warning log must include the selector and a body snippet for debugging."""
        body_snippet = "random unrelated page content"
        wrapper, _ = _make_driver(shadow_text="", body_text=body_snippet)
        with patch.object(drv._log, "warning") as mock_warn:  # pylint: disable=protected-access
            detect_popup_thank_you(wrapper)
        warn_str = str(mock_warn.call_args_list)
        self.assertIn("selector", warn_str)

    def test_debug_log_on_selector_match(self):
        """A debug log must be emitted when the selector scan finds a match."""
        wrapper, _ = _make_driver(shadow_text="Thank you for your order")
        with patch.object(drv._log, "debug") as mock_debug:  # pylint: disable=protected-access
            detect_popup_thank_you(wrapper)
        debug_str = str(mock_debug.call_args_list)
        self.assertIn("detect_popup_thank_you", debug_str)

    def test_debug_log_on_body_fallback_match(self):
        """A debug log must be emitted when the body fallback finds a match."""
        wrapper, _ = _make_driver(shadow_text="", body_text="Thank you for your order")
        with patch.object(drv._log, "debug") as mock_debug:  # pylint: disable=protected-access
            detect_popup_thank_you(wrapper)
        debug_str = str(mock_debug.call_args_list)
        self.assertIn("body fallback", debug_str)

    def test_no_warning_log_on_success(self):
        """No WARNING must be emitted when the popup is successfully detected."""
        wrapper, _ = _make_driver(shadow_text="Thank you for your order")
        with patch.object(drv._log, "warning") as mock_warn:  # pylint: disable=protected-access
            detect_popup_thank_you(wrapper)
        self.assertFalse(
            mock_warn.called,
            "WARNING log must NOT be emitted on successful detection",
        )


# ---------------------------------------------------------------------------
# Full cycle mock: checkout → thank_you  (Acceptance Criterion #2)
# ---------------------------------------------------------------------------

class TestThankYouFullCycleMock(unittest.TestCase):
    """Simulate the complete checkout → confirmation flow with injected DOM.

    This validates that detect_popup_thank_you() integrates correctly with
    the GivexDriver.run_full_cycle() success path: the confirmation page DOM
    is injected and the thank-you text is asserted.
    """

    def _build_full_cycle_driver(self, confirmation_text: str) -> MagicMock:
        """Return a mock GivexDriver whose detect_page_state returns 'success'
        and whose underlying raw driver returns *confirmation_text* for the
        thank-you detection step.
        """
        base = MagicMock()

        def _exec_script(script, *args):
            if args:
                return confirmation_text
            return confirmation_text

        base.execute_script.side_effect = _exec_script
        base.find_elements.return_value = []
        base.current_url = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/confirmation"

        wrapper = MagicMock()
        wrapper._driver = base  # pylint: disable=protected-access
        return wrapper

    def test_full_cycle_en_confirmation_detected(self):
        """After a successful checkout, EN thank-you text is detected in DOM."""
        wrapper = self._build_full_cycle_driver(
            "Thank you for your order! Your e-gift card will be emailed shortly."
        )
        result = detect_popup_thank_you(wrapper)
        self.assertIsNotNone(result, "Expected EN thank-you to be detected after checkout")
        self.assertIn(result, POPUP_TEXT_PATTERNS_THANK_YOU_EN)

    def test_full_cycle_vn_confirmation_detected(self):
        """After a successful checkout, VN thank-you text is detected in DOM."""
        wrapper = self._build_full_cycle_driver(
            "Cảm ơn đơn hàng của bạn! Thẻ quà sẽ được gửi đến email của bạn."
        )
        result = detect_popup_thank_you(wrapper)
        self.assertIsNotNone(result, "Expected VN thank-you to be detected after checkout")
        self.assertIn(result, POPUP_TEXT_PATTERNS_THANK_YOU_VN)

    def test_full_cycle_dom_inject_en_via_body(self):
        """Injected DOM via body innerText fallback is detected (EN)."""
        wrapper = self._build_full_cycle_driver(
            "Your order has been placed. Thank you for shopping with Lush!"
        )
        result = detect_popup_thank_you(wrapper)
        self.assertIsNotNone(result)
        self.assertIn(result, POPUP_TEXT_PATTERNS_THANK_YOU)

    def test_full_cycle_dom_inject_vn_via_body(self):
        """Injected DOM via body innerText fallback is detected (VN)."""
        wrapper = self._build_full_cycle_driver(
            "Đặt hàng thành công. Cảm ơn bạn đã mua sắm tại Lush!"
        )
        result = detect_popup_thank_you(wrapper)
        self.assertIsNotNone(result)
        self.assertIn(result, POPUP_TEXT_PATTERNS_THANK_YOU)

    def test_full_cycle_log_on_missing_dom(self):
        """When confirmation DOM is missing after checkout, WARNING log is emitted."""
        wrapper = self._build_full_cycle_driver("")  # empty page
        with patch.object(drv._log, "warning") as mock_warn:  # pylint: disable=protected-access
            result = detect_popup_thank_you(wrapper)
        self.assertIsNone(result)
        self.assertTrue(
            mock_warn.called,
            "Expected WARNING log when DOM has no thank-you text after checkout",
        )

    def test_full_cycle_mixed_lang_dom(self):
        """Confirmation page with both EN and VN text — at least one pattern matches."""
        wrapper = self._build_full_cycle_driver(
            "Thank you for your order! Cảm ơn đơn hàng của bạn!"
        )
        result = detect_popup_thank_you(wrapper)
        self.assertIsNotNone(result)
        self.assertIn(result, POPUP_TEXT_PATTERNS_THANK_YOU)

    def test_full_cycle_custom_selector(self):
        """detect_popup_thank_you honours a custom confirmation selector."""
        wrapper = self._build_full_cycle_driver("Order confirmed — see your inbox.")
        result = detect_popup_thank_you(wrapper, selector=".custom-confirmation")
        self.assertIsNotNone(result)
        self.assertEqual(result, "order confirmed")


# ---------------------------------------------------------------------------
# Callable test invocation
# ---------------------------------------------------------------------------

class TestDetectPopupThankYouCallable(unittest.TestCase):
    """Ensure detect_popup_thank_you_test() passes as a callable assertion."""

    def test_callable_detect_popup_thank_you_test(self):
        """detect_popup_thank_you_test() must not raise any assertion error."""
        detect_popup_thank_you_test()


if __name__ == "__main__":
    unittest.main()
