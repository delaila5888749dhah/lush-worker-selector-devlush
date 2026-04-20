import inspect
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    SEL_POPUP_CLOSE,
    SEL_POPUP_SOMETHING_WRONG,
    POPUP_TEXT_PATTERNS_EN,
    POPUP_TEXT_PATTERNS_VN,
    POPUP_TEXT_PATTERNS_DEFAULT,
    check_popup_text_match,
    handle_something_wrong_popup,
)


class TestPopupHandler(unittest.TestCase):
    def test_clicks_close_when_popup_present(self):
        base_driver = MagicMock()
        wrapper = MagicMock()
        wrapper._driver = base_driver
        wrapper.bounding_box_click = MagicMock()

        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertTrue(result)
        wrapper.bounding_box_click.assert_called_once_with(SEL_POPUP_CLOSE)

    def test_returns_false_when_no_popup(self):
        from selenium.common.exceptions import TimeoutException

        base_driver = MagicMock()
        wrapper = MagicMock()
        wrapper._driver = base_driver

        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.side_effect = TimeoutException()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertFalse(result)

    def test_NEVER_calls_removeNode(self):
        source = inspect.getsource(handle_something_wrong_popup)
        self.assertNotIn("removeNode", source)
        self.assertNotIn("removeChild", source)
        self.assertNotIn(".remove(", source)


class TestPopupTextMatchPatterns(unittest.TestCase):
    """Validate that all required EN/VN pattern constants are present."""

    def test_en_patterns_non_empty(self):
        self.assertTrue(len(POPUP_TEXT_PATTERNS_EN) > 0)

    def test_vn_patterns_non_empty(self):
        self.assertTrue(len(POPUP_TEXT_PATTERNS_VN) > 0)

    def test_default_includes_en_and_vn(self):
        for pat in POPUP_TEXT_PATTERNS_EN:
            self.assertIn(pat, POPUP_TEXT_PATTERNS_DEFAULT)
        for pat in POPUP_TEXT_PATTERNS_VN:
            self.assertIn(pat, POPUP_TEXT_PATTERNS_DEFAULT)

    def test_patterns_are_lowercase(self):
        for pat in POPUP_TEXT_PATTERNS_DEFAULT:
            self.assertEqual(pat, pat.lower(), f"Pattern not lowercase: {pat!r}")

    def test_en_contains_something_went_wrong(self):
        self.assertIn("something went wrong", POPUP_TEXT_PATTERNS_EN)

    def test_vn_contains_co_loi_xay_ra(self):
        self.assertIn("có lỗi xảy ra", POPUP_TEXT_PATTERNS_VN)


class TestCheckPopupTextMatch(unittest.TestCase):
    """Unit tests for check_popup_text_match() — text-match, multi-lang, shadow DOM."""

    def _make_driver(self, script_return: str = "", find_elements_texts=None):
        """Helper: build a minimal mock driver."""
        base = MagicMock()
        base.execute_script.return_value = script_return
        if find_elements_texts is not None:
            mocks = []
            for t in find_elements_texts:
                el = MagicMock()
                el.text = t
                mocks.append(el)
            base.find_elements.return_value = mocks
        else:
            base.find_elements.return_value = []
        wrapper = MagicMock()
        wrapper._driver = base
        return wrapper, base

    # ── shadow_root=True path ────────────────────────────────────────────────

    def test_match_en_pattern_via_shadow_root(self):
        wrapper, base = self._make_driver(
            script_return="Something went wrong — please contact support."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertEqual(result, "something went wrong")
        base.execute_script.assert_called_once()

    def test_match_vn_pattern_via_shadow_root(self):
        wrapper, base = self._make_driver(
            script_return="Có lỗi xảy ra, vui lòng thử lại."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIn(result, POPUP_TEXT_PATTERNS_VN)

    def test_no_match_when_popup_text_irrelevant(self):
        wrapper, base = self._make_driver(
            script_return="Welcome back! Your order is confirmed."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNone(result)

    def test_returns_none_when_no_popup_text(self):
        wrapper, base = self._make_driver(script_return="")
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNone(result)

    def test_execute_script_raises_returns_none(self):
        wrapper, base = self._make_driver()
        base.execute_script.side_effect = Exception("JS error")
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNone(result)

    # ── shadow_root=False path (find_elements) ───────────────────────────────

    def test_match_en_pattern_no_shadow_root(self):
        wrapper, base = self._make_driver(
            find_elements_texts=["Payment failed. Please try again."]
        )
        with patch.object(drv, "By", drv.By):
            result = check_popup_text_match(wrapper, shadow_root=False)
        self.assertIn(result, ("payment failed", "please try again"))

    def test_no_match_no_shadow_root_empty_elements(self):
        wrapper, base = self._make_driver(find_elements_texts=[])
        result = check_popup_text_match(wrapper, shadow_root=False)
        self.assertIsNone(result)

    # ── custom patterns ──────────────────────────────────────────────────────

    def test_custom_patterns_override_default(self):
        wrapper, base = self._make_driver(
            script_return="CUSTOM_ERROR_XYZ on page"
        )
        result = check_popup_text_match(
            wrapper,
            patterns=("custom_error_xyz",),
            shadow_root=True,
        )
        self.assertEqual(result, "custom_error_xyz")

    def test_custom_patterns_no_match(self):
        wrapper, base = self._make_driver(
            script_return="something went wrong"
        )
        result = check_popup_text_match(
            wrapper,
            patterns=("custom_error_xyz",),
            shadow_root=True,
        )
        self.assertIsNone(result)

    # ── multi-language edge-cases ─────────────────────────────────────────────

    def test_match_vn_thanh_toan_that_bai(self):
        wrapper, _ = self._make_driver(
            script_return="Thanh toán thất bại. Vui lòng kiểm tra lại thông tin."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIn(result, POPUP_TEXT_PATTERNS_VN)

    def test_match_vn_giao_dich_bi_tu_choi(self):
        wrapper, _ = self._make_driver(
            script_return="Giao dịch bị từ chối bởi ngân hàng."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertEqual(result, "giao dịch bị từ chối")

    def test_match_en_transaction_declined(self):
        wrapper, _ = self._make_driver(
            script_return="Transaction declined — card number invalid."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertEqual(result, "transaction declined")

    def test_match_en_session_expired(self):
        wrapper, _ = self._make_driver(
            script_return="Session expired — please log in again."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertEqual(result, "session expired")

    def test_mixed_lang_popup_matches_first_found(self):
        """Popup containing both EN and VN text should match one of the known patterns."""
        wrapper, _ = self._make_driver(
            script_return=(
                "Something went wrong. Có lỗi xảy ra, vui lòng thử lại."
            )
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNotNone(result)
        self.assertIn(result, POPUP_TEXT_PATTERNS_DEFAULT)

    # ── shadow DOM traversal ─────────────────────────────────────────────────

    def test_shadow_root_js_called_with_correct_selector(self):
        wrapper, base = self._make_driver(script_return="")
        check_popup_text_match(
            wrapper, selector=".custom-popup", shadow_root=True
        )
        call_args = base.execute_script.call_args
        self.assertEqual(call_args[0][1], ".custom-popup")

    def test_shadow_root_text_match_works_with_raw_driver(self):
        """Passing a raw (unwrapped) driver must also work."""
        raw = MagicMock(spec=["execute_script"])
        raw.execute_script.return_value = "An error occurred."
        result = check_popup_text_match(raw, shadow_root=True)
        self.assertEqual(result, "an error occurred")

    def test_shadow_root_returns_none_on_js_none(self):
        wrapper, base = self._make_driver()
        base.execute_script.return_value = None
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNone(result)

    # ── debug logging ─────────────────────────────────────────────────────────

    def test_debug_log_on_match(self):
        wrapper, _ = self._make_driver(
            script_return="Something went wrong"
        )
        with patch.object(drv._log, "debug") as mock_debug:
            check_popup_text_match(wrapper, shadow_root=True)
        call_args_list = [str(c) for c in mock_debug.call_args_list]
        self.assertTrue(
            any("MATCH" in s for s in call_args_list),
            "Expected a debug log containing 'MATCH'",
        )

    def test_debug_log_on_no_match(self):
        wrapper, _ = self._make_driver(
            script_return="Everything is fine, your order is confirmed."
        )
        with patch.object(drv._log, "debug") as mock_debug:
            check_popup_text_match(wrapper, shadow_root=True)
        call_args_list = [str(c) for c in mock_debug.call_args_list]
        self.assertTrue(
            any("NO MATCH" in s for s in call_args_list),
            "Expected a debug log containing 'NO MATCH'",
        )

    def test_debug_log_on_empty_text(self):
        wrapper, _ = self._make_driver(script_return="")
        with patch.object(drv._log, "debug") as mock_debug:
            check_popup_text_match(wrapper, shadow_root=True)
        call_args_list = [str(c) for c in mock_debug.call_args_list]
        self.assertTrue(
            any("no popup text" in s for s in call_args_list),
            "Expected a debug log about no popup text found",
        )


if __name__ == "__main__":
    unittest.main()
