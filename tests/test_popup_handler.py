import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.common.exceptions import SelectorTimeoutError

from modules.cdp import driver as drv
from modules.cdp.driver import (
    SEL_POPUP_CLOSE,
    SEL_POPUP_SOMETHING_WRONG,
    XPATH_POPUP_SWW,
    POPUP_TEXT_PATTERNS_EN,
    POPUP_TEXT_PATTERNS_VN,
    POPUP_TEXT_PATTERNS_DEFAULT,
    PopupCloseOutcome,
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
        wrapper._driver = base  # pylint: disable=protected-access
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
        wrapper, _ = self._make_driver(
            script_return="Có lỗi xảy ra, vui lòng thử lại."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIn(result, POPUP_TEXT_PATTERNS_VN)

    def test_no_match_when_popup_text_irrelevant(self):
        wrapper, _ = self._make_driver(
            script_return="Welcome back! Your order is confirmed."
        )
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNone(result)

    def test_returns_none_when_no_popup_text(self):
        wrapper, _ = self._make_driver(script_return="")
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNone(result)

    def test_execute_script_raises_returns_none(self):
        wrapper, base = self._make_driver()
        base.execute_script.side_effect = Exception("JS error")
        result = check_popup_text_match(wrapper, shadow_root=True)
        self.assertIsNone(result)

    # ── shadow_root=False path (find_elements) ───────────────────────────────

    def test_match_en_pattern_no_shadow_root(self):
        wrapper, _ = self._make_driver(
            find_elements_texts=["Payment failed. Please try again."]
        )
        with patch.object(drv, "By", drv.By):
            result = check_popup_text_match(wrapper, shadow_root=False)
        self.assertIn(result, ("payment failed", "please try again"))

    def test_no_match_no_shadow_root_empty_elements(self):
        wrapper, _ = self._make_driver(find_elements_texts=[])
        result = check_popup_text_match(wrapper, shadow_root=False)
        self.assertIsNone(result)

    # ── custom patterns ──────────────────────────────────────────────────────

    def test_custom_patterns_override_default(self):
        wrapper, _ = self._make_driver(
            script_return="CUSTOM_ERROR_XYZ on page"
        )
        result = check_popup_text_match(
            wrapper,
            patterns=("custom_error_xyz",),
            shadow_root=True,
        )
        self.assertEqual(result, "custom_error_xyz")

    def test_custom_patterns_no_match(self):
        wrapper, _ = self._make_driver(
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
        with patch.object(drv._log, "debug") as mock_debug:  # pylint: disable=protected-access
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
        with patch.object(drv._log, "debug") as mock_debug:  # pylint: disable=protected-access
            check_popup_text_match(wrapper, shadow_root=True)
        call_args_list = [str(c) for c in mock_debug.call_args_list]
        self.assertTrue(
            any("NO MATCH" in s for s in call_args_list),
            "Expected a debug log containing 'NO MATCH'",
        )

    def test_debug_log_on_empty_text(self):
        wrapper, _ = self._make_driver(script_return="")
        with patch.object(drv._log, "debug") as mock_debug:  # pylint: disable=protected-access
            check_popup_text_match(wrapper, shadow_root=True)
        call_args_list = [str(c) for c in mock_debug.call_args_list]
        self.assertTrue(
            any("no popup text" in s for s in call_args_list),
            "Expected a debug log about no popup text found",
        )


class TestPopupXPathLocator(unittest.TestCase):
    """P1-1: verify XPath text-match locator + POPUP_USE_XPATH rollback flag."""

    # Exact spec from issue P1-1 acceptance criteria.
    EXPECTED_XPATH = (
        "//*[self::div or self::section or self::dialog]"
        "[contains(translate(normalize-space(.),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'something went wrong')]"
    )

    def setUp(self):
        """Preserve and clear the popup locator flag for each test."""
        self._saved_env = os.environ.get("POPUP_USE_XPATH")
        if "POPUP_USE_XPATH" in os.environ:
            del os.environ["POPUP_USE_XPATH"]

    def tearDown(self):
        """Restore the popup locator flag after each test."""
        if self._saved_env is None:
            os.environ.pop("POPUP_USE_XPATH", None)
        else:
            os.environ["POPUP_USE_XPATH"] = self._saved_env

    def test_xpath_constant_matches_issue_spec(self):
        """The XPath literal must remain byte-for-byte aligned with the AC."""
        self.assertEqual(XPATH_POPUP_SWW, self.EXPECTED_XPATH)

    @staticmethod
    def _make_popup_wrapper(base_driver=None):
        """Build a minimal wrapper that matches handle_something_wrong_popup()."""
        return SimpleNamespace(
            _driver=base_driver or MagicMock(),
            bounding_box_click=MagicMock(),
        )

    @staticmethod
    def _run_handler_capturing_locator():
        """Execute the popup handler and return the locator it uses."""
        wrapper = TestPopupXPathLocator._make_popup_wrapper()
        captured = {}

        def fake_presence(locator):
            """Capture the locator passed to Selenium expected-conditions."""
            captured["locator"] = locator
            return lambda d: MagicMock()

        with patch.object(drv.EC, "presence_of_element_located",
                          side_effect=fake_presence), \
             patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            handle_something_wrong_popup(wrapper, timeout=0.1)
        return captured.get("locator")

    def test_uses_xpath_locator_by_default(self):
        """Default configuration should route popup detection through XPath."""
        locator = self._run_handler_capturing_locator()
        self.assertEqual(locator, (drv.By.XPATH, XPATH_POPUP_SWW))

    def test_uses_xpath_when_env_enabled_explicitly(self):
        """Explicitly enabling the flag should still use the XPath locator."""
        os.environ["POPUP_USE_XPATH"] = "1"
        locator = self._run_handler_capturing_locator()
        self.assertEqual(locator, (drv.By.XPATH, XPATH_POPUP_SWW))

    def test_falls_back_to_css_when_env_disabled(self):
        """Disabling the flag should restore the legacy CSS selector path."""
        os.environ["POPUP_USE_XPATH"] = "0"
        locator = self._run_handler_capturing_locator()
        self.assertEqual(locator, (drv.By.CSS_SELECTOR, SEL_POPUP_SOMETHING_WRONG))

    def test_cookie_banner_without_text_does_not_match(self):
        """AC: DOM có cookie banner `.modal` không text → KHÔNG match.

        Simulates XPath evaluation: a cookie banner modal without the target
        phrase produces no XPath hit, so WebDriverWait raises TimeoutException
        and the handler returns False (no click, no false-positive flow loss).
        """
        wrapper = self._make_popup_wrapper()

        captured = {}

        def fake_presence(locator):
            """Capture the XPath locator while simulating a no-match result."""
            captured["locator"] = locator
            return lambda d: None

        with patch.object(drv.EC, "presence_of_element_located",
                          side_effect=fake_presence), \
             patch.object(drv, "WebDriverWait") as mock_wait:
            # XPath locator finds no element containing "something went wrong"
            # → WebDriverWait.until raises TimeoutException.
            mock_wait.return_value.until.side_effect = drv.TimeoutException()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertFalse(result)
        self.assertEqual(captured["locator"], (drv.By.XPATH, XPATH_POPUP_SWW))
        wrapper.bounding_box_click.assert_not_called()

    def test_modal_with_target_text_does_match(self):
        """AC: DOM có modal với text 'Something went wrong, please try again' → match."""
        wrapper = self._make_popup_wrapper()

        captured = {}

        def fake_presence(locator):
            """Capture the XPath locator while simulating a positive match."""
            captured["locator"] = locator
            # Simulate XPath hit — predicate returns a matched element.
            return lambda d: MagicMock(text="Something went wrong, please try again")

        with patch.object(drv.EC, "presence_of_element_located",
                          side_effect=fake_presence), \
             patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock(
                text="Something went wrong, please try again")
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertTrue(result)
        self.assertEqual(captured["locator"], (drv.By.XPATH, XPATH_POPUP_SWW))
        wrapper.bounding_box_click.assert_called_once_with(SEL_POPUP_CLOSE)


class TestPopupClearAfterClose(unittest.TestCase):
    """P1-2 — after close popup → clear card fields + return signal."""

    def setUp(self):
        self._saved = os.environ.get("POPUP_CLEAR_AFTER_CLOSE")
        os.environ.pop("POPUP_CLEAR_AFTER_CLOSE", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("POPUP_CLEAR_AFTER_CLOSE", None)
        else:
            os.environ["POPUP_CLEAR_AFTER_CLOSE"] = self._saved

    def _make_wrapper(self):
        wrapper = SimpleNamespace(
            _driver=MagicMock(),
            bounding_box_click=MagicMock(),
            clear_card_fields_cdp=MagicMock(),
        )
        return wrapper

    def test_close_success_calls_clear_and_returns_needs_refill(self):
        wrapper = self._make_wrapper()
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSED_NEEDS_REFILL)
        self.assertTrue(bool(result))  # enum is truthy for bool-compat
        wrapper.bounding_box_click.assert_called_once_with(SEL_POPUP_CLOSE)
        wrapper.clear_card_fields_cdp.assert_called_once_with()

    def test_no_popup_returns_not_present_and_skips_clear(self):
        from selenium.common.exceptions import TimeoutException

        wrapper = self._make_wrapper()
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.side_effect = TimeoutException()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.NOT_PRESENT)
        self.assertFalse(bool(result))
        wrapper.clear_card_fields_cdp.assert_not_called()

    def test_close_click_failure_returns_close_failed_and_skips_clear(self):
        wrapper = self._make_wrapper()
        wrapper.bounding_box_click.side_effect = RuntimeError("boom")
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSE_FAILED)
        self.assertFalse(bool(result))
        wrapper.clear_card_fields_cdp.assert_not_called()

    def test_clear_failure_is_swallowed_and_still_signals_refill(self):
        wrapper = self._make_wrapper()
        wrapper.clear_card_fields_cdp.side_effect = RuntimeError("cdp down")
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSED_NEEDS_REFILL)
        wrapper.clear_card_fields_cdp.assert_called_once_with()

    def test_env_disable_skips_clear_but_still_signals_refill(self):
        os.environ["POPUP_CLEAR_AFTER_CLOSE"] = "0"
        wrapper = self._make_wrapper()
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSED_NEEDS_REFILL)
        wrapper.clear_card_fields_cdp.assert_not_called()

    def test_driver_without_clear_method_does_not_raise(self):
        wrapper = SimpleNamespace(
            _driver=MagicMock(),
            bounding_box_click=MagicMock(),
        )
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSED_NEEDS_REFILL)


class TestPopupXPathCloseFallback(unittest.TestCase):
    """P1-6 — CSS-miss fallback to XPath text-match for <button>/<a> close."""

    def test_xpath_close_locator_covers_required_texts_and_tags(self):
        """XPath must cover both <button>/<a> and the Close/OK/X/Đóng tokens."""
        xpath = drv.XPATH_POPUP_CLOSE
        # Must cover both <button> and <a> tags.
        self.assertIn("self::button", xpath)
        self.assertIn("self::a", xpath)
        # Must cover the required text tokens (case-insensitive ASCII via
        # translate() + literal Vietnamese "Đóng").
        self.assertIn("'close'", xpath)
        self.assertIn("'ok'", xpath)
        self.assertIn("'x'", xpath)
        self.assertIn("Đóng", xpath)

    def test_css_miss_triggers_xpath_fallback_and_returns_needs_refill(self):
        """CSS-miss (SelectorTimeoutError) must trigger XPath click + clear."""
        fake_el = MagicMock()
        base_driver = MagicMock()
        base_driver.find_elements.return_value = [fake_el]
        wrapper = SimpleNamespace(
            _driver=base_driver,
            bounding_box_click=MagicMock(
                side_effect=SelectorTimeoutError(SEL_POPUP_CLOSE, 0)
            ),
            clear_card_fields_cdp=MagicMock(),
        )
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSED_NEEDS_REFILL)
        base_driver.find_elements.assert_called_once_with(
            "xpath", drv.XPATH_POPUP_CLOSE
        )
        fake_el.click.assert_called_once_with()
        wrapper.clear_card_fields_cdp.assert_called_once_with()

    def test_css_miss_with_no_xpath_match_returns_close_failed(self):
        """CSS-miss with empty XPath result must return CLOSE_FAILED and skip clear."""
        base_driver = MagicMock()
        base_driver.find_elements.return_value = []
        wrapper = SimpleNamespace(
            _driver=base_driver,
            bounding_box_click=MagicMock(
                side_effect=SelectorTimeoutError(SEL_POPUP_CLOSE, 0)
            ),
            clear_card_fields_cdp=MagicMock(),
        )
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSE_FAILED)
        wrapper.clear_card_fields_cdp.assert_not_called()

    def test_xpath_fallback_tries_next_element_when_first_click_raises(self):
        """If the first XPath match's click() raises, fallback must try the next."""
        bad_el = MagicMock()
        bad_el.click.side_effect = RuntimeError("detached")
        good_el = MagicMock()
        base_driver = MagicMock()
        base_driver.find_elements.return_value = [bad_el, good_el]
        wrapper = SimpleNamespace(
            _driver=base_driver,
            bounding_box_click=MagicMock(
                side_effect=SelectorTimeoutError(SEL_POPUP_CLOSE, 0)
            ),
            clear_card_fields_cdp=MagicMock(),
        )
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSED_NEEDS_REFILL)
        bad_el.click.assert_called_once_with()
        good_el.click.assert_called_once_with()

    def test_css_success_does_not_invoke_xpath_fallback(self):
        """When CSS click succeeds, the XPath fallback must not be invoked."""
        base_driver = MagicMock()
        wrapper = SimpleNamespace(
            _driver=base_driver,
            bounding_box_click=MagicMock(),
            clear_card_fields_cdp=MagicMock(),
        )
        with patch.object(drv, "WebDriverWait") as mock_wait:
            mock_wait.return_value.until.return_value = MagicMock()
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertIs(result, PopupCloseOutcome.CLOSED_NEEDS_REFILL)
        base_driver.find_elements.assert_not_called()


if __name__ == "__main__":
    unittest.main()
