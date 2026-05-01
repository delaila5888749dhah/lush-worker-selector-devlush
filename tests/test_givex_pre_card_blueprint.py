import random
import unittest
from unittest.mock import MagicMock, call, patch

from selenium.common.exceptions import WebDriverException

from modules.cdp import driver as drv
from modules.cdp.driver import (
    GivexDriver,
    SEL_AMOUNT_INPUT,
    SEL_CONFIRM_RECIPIENT_EMAIL,
    SEL_GREETING_MSG,
    SEL_RECIPIENT_EMAIL,
    SEL_RECIPIENT_NAME,
    SEL_REVIEW_CHECKOUT,
    SEL_SENDER_NAME,
    URL_CART,
)
from modules.common.exceptions import SelectorTimeoutError, SessionFlaggedError
from tests.test_givex_driver import _make_billing, _make_driver, _make_task


class LowBoundRng:
    def uniform(self, low, high):
        return low

    def randint(self, low, _high):
        return low


class FakeDelayEngine:
    def __init__(self, permitted=True, actual=None):
        self.permitted = permitted
        self.actual = actual
        self.accumulated = []

    def is_delay_permitted(self):
        return self.permitted

    def accumulate_delay(self, delay):
        self.accumulated.append(delay)
        return self.actual if self.actual is not None else delay


def _make_scroll_test_driver():
    selenium = _make_driver()
    selenium.find_elements.return_value = [MagicMock()]

    def execute_script(script, *_args):
        if "getBoundingClientRect" in script:
            return {"top": 1000, "bottom": 1040, "height": 40}
        if "window.innerHeight" in script:
            return 720
        return None

    selenium.execute_script.side_effect = execute_script
    return selenium


class HumanScrollToTests(unittest.TestCase):
    def test_uses_cdp_wheel_as_primary(self):
        selenium = _make_scroll_test_driver()
        gd = GivexDriver(selenium)
        with patch("modules.cdp.driver.time.sleep"):
            gd._human_scroll_to(SEL_GREETING_MSG, max_steps=1)
        payloads = [c.args[1] for c in selenium.execute_cdp_cmd.call_args_list]
        self.assertTrue(any(p.get("type") == "mouseWheel" for p in payloads))
        scripts = [c.args[0] for c in selenium.execute_script.call_args_list]
        self.assertFalse(any("scrollIntoView" in s for s in scripts))

    def test_falls_back_to_js_when_cdp_wheel_raises(self):
        selenium = _make_scroll_test_driver()
        selenium.execute_cdp_cmd.side_effect = WebDriverException("no wheel")
        gd = GivexDriver(selenium)
        with patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="WARNING") as logs:
            gd._human_scroll_to(SEL_GREETING_MSG, max_steps=1)
        self.assertIn("falling back to JS scrollIntoView", "\n".join(logs.output))
        self.assertIn("stage=wheel_dispatch", "\n".join(logs.output))
        scripts = [c.args[0] for c in selenium.execute_script.call_args_list]
        self.assertTrue(any("scrollIntoView" in s for s in scripts))

    def test_degraded_mode_no_crash(self):
        selenium = _make_scroll_test_driver()
        selenium.execute_cdp_cmd.side_effect = AttributeError("degraded")
        gd = GivexDriver(selenium)
        with patch("modules.cdp.driver.time.sleep"):
            gd._human_scroll_to(SEL_GREETING_MSG, max_steps=1)

    def test_micro_ticks_use_adaptive_count_and_delta_range(self):
        selenium = _make_scroll_test_driver()
        gd = GivexDriver(selenium)
        with patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch("modules.cdp.driver.time.sleep"):
            gd._human_scroll_to(SEL_GREETING_MSG)
        wheel_payloads = [
            c.args[1] for c in selenium.execute_cdp_cmd.call_args_list
            if c.args[1].get("type") == "mouseWheel"
        ]
        self.assertEqual(len(wheel_payloads), 10)
        for payload in wheel_payloads:
            self.assertGreaterEqual(abs(payload["deltaY"]), 70)
            self.assertLessEqual(abs(payload["deltaY"]), 120)

    def test_micro_ticks_are_capped_by_max_steps_multiplier(self):
        selenium = _make_scroll_test_driver()
        gd = GivexDriver(selenium)
        selenium.execute_script.side_effect = lambda script, *_a: (
            {"top": 10000, "bottom": 10040, "height": 40}
            if "getBoundingClientRect" in script else 720 if "window.innerHeight" in script else None
        )
        with patch.object(gd, "_get_rng", return_value=LowBoundRng()), patch("modules.cdp.driver.time.sleep"):
            gd._human_scroll_to(SEL_GREETING_MSG, max_steps=2)
        # max_steps=2 is capped at max_steps * 4 adaptive micro-ticks.
        self.assertEqual(sum(1 for c in selenium.execute_cdp_cmd.call_args_list if c.args[1].get("type") == "mouseWheel"), 8)

    def test_cursor_path_does_not_use_ghostcursor_sleep(self):
        selenium = _make_scroll_test_driver()
        gd = GivexDriver(selenium)
        gd._cursor = MagicMock()
        with patch.object(gd, "_get_rng", return_value=LowBoundRng()), patch("modules.cdp.driver.time.sleep"):
            gd._human_scroll_to(SEL_GREETING_MSG, max_steps=1)
        gd._cursor.scroll_wheel.assert_not_called()
        self.assertTrue(selenium.execute_cdp_cmd.called)


class EngineAwareSleepTests(unittest.TestCase):
    def test_respects_delay_not_permitted(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._engine = FakeDelayEngine(permitted=False)
        with patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch("modules.cdp.driver.time.sleep") as sleep:
            actual = gd._engine_aware_sleep(1.0, 2.0, "unit")
        self.assertEqual(actual, 0.0)
        sleep.assert_not_called()
        self.assertEqual(gd._engine.accumulated, [])

    def test_accumulates_and_scales_to_headroom(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._engine = FakeDelayEngine(permitted=True, actual=0.25)
        with patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch("modules.cdp.driver.time.sleep") as sleep:
            actual = gd._engine_aware_sleep(1.0, 2.0, "unit")
        self.assertEqual(actual, 0.25)
        self.assertEqual(gd._engine.accumulated, [1.0])
        sleep.assert_called_once_with(0.25)


class ScrollStableTests(unittest.TestCase):
    def test_wait_scroll_stable_returns_false_on_timeout(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = [1, 2, 3]
        with patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="WARNING") as logs:
            self.assertFalse(gd._wait_scroll_stable(timeout=0.0, stable_ms=350))
        self.assertIn("timeout", "\n".join(logs.output))


class DiagnosticsTests(unittest.TestCase):
    def test_form_validation_diagnostics_returns_structural_lengths(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = {
            "forms": [{
                "checkValidity": True,
                "elements_length": 1,
                "elements": [{
                    "selector_name": "SEL_RECIPIENT_EMAIL",
                    "tag": "input",
                    "type": "email",
                    "id_len": 3,
                    "name_len": 5,
                    "value_len": 18,
                    "validity": {"valid": True},
                    "validationMessage_len": 0,
                }],
            }]
        }
        data = gd._form_validation_diagnostics()
        self.assertTrue(data["forms"][0]["checkValidity"])
        self.assertEqual(data["forms"][0]["elements_length"], 1)
        self.assertEqual(data["forms"][0]["elements"][0]["value_len"], 18)
        self.assertNotIn("secret@example.com", repr(data))


class NaturalBlurTests(unittest.TestCase):
    def test_blur_active_field_uses_tab_when_focus_changes(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.side_effect = [
            {"tag": "input", "id_len": 3},
            {"tag": "body", "id_len": 0},
        ]
        with patch("modules.cdp.keyboard.dispatch_key", return_value=True) as dispatch, \
             patch.object(gd, "_engine_aware_sleep", return_value=0):
            self.assertTrue(gd._blur_active_field_naturally())
        dispatch.assert_called_once_with(gd._driver, "Tab")
        gd._driver.execute_cdp_cmd.assert_not_called()

    def test_blur_active_field_falls_back_to_safe_body_click(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.side_effect = [
            {"tag": "input", "id_len": 3},
            {"tag": "input", "id_len": 3},
            {"x": 24, "y": 24},
            {"tag": "body", "id_len": 0},
        ]
        with patch("modules.cdp.keyboard.dispatch_key", return_value=True), \
             patch.object(gd, "_engine_aware_sleep", return_value=0):
            self.assertTrue(gd._blur_active_field_naturally())
        self.assertTrue(gd._driver.execute_cdp_cmd.called)


class FocusBeforeTypeTests(unittest.TestCase):
    def test_realistic_type_field_calls_bounding_box_click_before_type(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        gd = GivexDriver(selenium, strict=False)
        order = []
        with patch.object(gd, "_human_scroll_to", side_effect=lambda _s: order.append("scroll")), \
             patch.object(gd, "_wait_scroll_stable", side_effect=lambda: order.append("stable")), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "bounding_box_click", side_effect=lambda _s: order.append("click")), \
             patch.object(gd, "_field_value_length", return_value=20), \
             patch("modules.cdp.driver._type_value", side_effect=lambda *_a, **_k: order.append("type")), \
             patch("modules.cdp.driver.time.sleep"):
            gd._realistic_type_field(SEL_RECIPIENT_EMAIL, "secret@example.com")
        self.assertEqual(order, ["scroll", "stable", "click", "type"])

    def test_realistic_type_field_calls_get_rng(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        gd = GivexDriver(selenium, strict=False)
        with patch.object(gd, "_human_scroll_to"), \
             patch.object(gd, "_wait_scroll_stable", return_value=True), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_field_value_length", return_value=2), \
             patch.object(gd, "_get_rng", return_value=random.Random(7)) as mock_rng, \
             patch("modules.cdp.driver._type_value"), \
             patch("modules.cdp.driver.time.sleep"):
            gd._realistic_type_field(SEL_RECIPIENT_NAME, "Jo")
        self.assertGreaterEqual(mock_rng.call_count, 1)


class FieldLengthVerificationTests(unittest.TestCase):
    def _run_type(self, selector, value, actual_len):
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        gd = GivexDriver(selenium, strict=False)
        with patch.object(gd, "_human_scroll_to"), \
             patch.object(gd, "_wait_scroll_stable", return_value=True), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_field_value_length", return_value=actual_len), \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver._type_value"), \
             patch("modules.cdp.driver.time.sleep"):
            gd._realistic_type_field(selector, value)

    def test_raises_session_flagged_when_value_empty(self):
        with self.assertRaisesRegex(SessionFlaggedError, "SEL_RECIPIENT_EMAIL"):
            self._run_type(SEL_RECIPIENT_EMAIL, "secret@example.com", 0)

    def test_unreadable_value_raises_distinct_failure(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch.object(gd, "_field_value_length", return_value=-1), \
             patch.object(gd, "_capture_failure_screenshot") as shot:
            with self.assertRaisesRegex(SessionFlaggedError, "unreadable") as ctx:
                gd._verify_field_value_length(SEL_RECIPIENT_EMAIL, 18, "SEL_RECIPIENT_EMAIL")
        shot.assert_called_once_with("type_field_unreadable_SEL_RECIPIENT_EMAIL")
        self.assertNotIn("empty", str(ctx.exception))
        self.assertNotIn("short", str(ctx.exception))

    def test_amount_field_allows_auto_format_extension(self):
        self._run_type(SEL_AMOUNT_INPUT, "25", 5)

    def test_amount_field_raises_when_truly_empty(self):
        with self.assertRaises(SessionFlaggedError):
            self._run_type(SEL_AMOUNT_INPUT, "25", 0)

    def test_other_fields_allow_70_percent_threshold(self):
        self._run_type(SEL_RECIPIENT_NAME, "abcdefghij", 8)

    def test_logs_only_lengths_never_values(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        gd = GivexDriver(selenium, strict=False)
        with patch.object(gd, "_human_scroll_to"), \
             patch.object(gd, "_wait_scroll_stable", return_value=True), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_field_value_length", return_value=18), \
             patch("modules.cdp.driver._type_value"), \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            gd._realistic_type_field(SEL_RECIPIENT_EMAIL, "secret@example.com")
        text = "\n".join(logs.output)
        self.assertIn("expected_len=18 actual_len=18", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("@example.com", text)


class FillEgiftFormFinalValidationTests(unittest.TestCase):
    fields = [
        SEL_GREETING_MSG,
        SEL_AMOUNT_INPUT,
        SEL_RECIPIENT_NAME,
        SEL_RECIPIENT_EMAIL,
        SEL_CONFIRM_RECIPIENT_EMAIL,
        SEL_SENDER_NAME,
    ]

    def test_final_pass_reads_all_six_fields(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally"), \
             patch.object(gd, "_field_value_length", return_value=10) as mock_len, \
             patch.object(gd, "_field_value", return_value="recipient@example.com"), \
             patch.object(drv, "_random_greeting", return_value="Hi"):
            gd.fill_egift_form(_make_task(), _make_billing())
        mock_len.assert_has_calls([call(sel) for sel in self.fields])

    def test_final_pass_raises_when_field_cleared_post_blur(self):
        gd = GivexDriver(_make_driver(), strict=False)
        lens = [10, 2, 8, 20, 20, 0]
        with patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally"), \
             patch.object(gd, "_field_value_length", side_effect=lens), \
             patch.object(gd, "_capture_failure_screenshot") as shot, \
             patch.object(gd, "_field_value", return_value="recipient@example.com"), \
             patch.object(drv, "_random_greeting", return_value="Hi"):
            with self.assertRaises(SessionFlaggedError):
                gd.fill_egift_form(_make_task(), _make_billing())
        shot.assert_called_once_with("final_check_empty_SEL_SENDER_NAME")

    def test_final_pass_raises_when_field_unreadable(self):
        gd = GivexDriver(_make_driver(), strict=False)
        lens = [10, 2, 8, -1, 20, 8]
        with patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally"), \
             patch.object(gd, "_field_value_length", side_effect=lens), \
             patch.object(gd, "_capture_failure_screenshot") as shot, \
             patch.object(drv, "_random_greeting", return_value="Hi"):
            with self.assertRaisesRegex(SessionFlaggedError, "unreadable"):
                gd.fill_egift_form(_make_task(), _make_billing())
        shot.assert_called_once_with("final_check_unreadable_SEL_RECIPIENT_EMAIL")

    def test_final_pass_validates_email_values_match(self):
        gd = GivexDriver(_make_driver(), strict=False)
        lens = [10, 2, 8, 7, 7, 8]
        with patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally"), \
             patch.object(gd, "_field_value_length", side_effect=lens), \
             patch.object(gd, "_field_value", side_effect=["a@b.com", "c@d.com"]), \
             patch.object(gd, "_capture_failure_screenshot") as shot, \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            with self.assertRaises(SessionFlaggedError):
                gd.fill_egift_form(_make_task(), _make_billing())
        shot.assert_called_once_with("final_check_email_mismatch")
        text = "\n".join(logs.output)
        self.assertIn("mismatch detected", text)
        self.assertNotIn("a@b.com", text)
        self.assertNotIn("c@d.com", text)

    def test_final_pass_raises_when_email_value_unreadable(self):
        gd = GivexDriver(_make_driver(), strict=False)
        lens = [10, 2, 8, 7, 7, 8]
        with patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally"), \
             patch.object(gd, "_field_value_length", side_effect=lens), \
             patch.object(gd, "_field_value", side_effect=[None, "a@b.com"]), \
             patch.object(gd, "_capture_failure_screenshot") as shot, \
             patch.object(drv, "_random_greeting", return_value="Hi"):
            with self.assertRaisesRegex(SessionFlaggedError, "unreadable"):
                gd.fill_egift_form(_make_task(), _make_billing())
        shot.assert_called_once_with("final_check_email_unreadable")

    def test_no_pii_in_final_pass_logs(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally"), \
             patch.object(gd, "_field_value_length", return_value=20), \
             patch.object(gd, "_field_value", return_value="recipient@example.com"), \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            gd.fill_egift_form(_make_task(), _make_billing())
        self.assertNotIn("@example.com", "\n".join(logs.output))

    def test_blur_failure_logs_warning_but_continues(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "_smooth_scroll_to"), patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), patch.object(gd, "_blur_active_field_naturally", return_value=False), \
             patch.object(gd, "_field_value_length", return_value=20), patch.object(gd, "_field_value", return_value="recipient@example.com"), \
             patch.object(drv, "_random_greeting", return_value="Hi"), self.assertLogs("modules.cdp.driver", level="WARNING") as logs:
            gd.fill_egift_form(_make_task(), _make_billing())
        self.assertIn("blur active field failed", "\n".join(logs.output))


class WaitForInteractableTests(unittest.TestCase):
    def _make_interactable_element(self):
        elem = MagicMock()
        elem.is_displayed.return_value = True
        elem.is_enabled.return_value = True
        return elem

    def test_rejects_present_but_display_none(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = False
        self.assertFalse(gd._is_interactable(self._make_interactable_element()))

    def test_rejects_present_but_pointer_events_none(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = False
        self.assertFalse(gd._is_interactable(self._make_interactable_element()))

    def test_rejects_aria_disabled(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = False
        self.assertFalse(gd._is_interactable(self._make_interactable_element()))

    def test_rejects_zero_size_rect(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = False
        self.assertFalse(gd._is_interactable(self._make_interactable_element()))

    def test_accepts_normal_button(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = True
        self.assertTrue(gd._is_interactable(self._make_interactable_element()))

    def test_wait_for_interactable_accepts_later_matching_element(self):
        hidden = self._make_interactable_element()
        visible = self._make_interactable_element()
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "find_elements", return_value=[hidden, visible]), \
             patch.object(gd, "_is_interactable", side_effect=[False, True]):
            self.assertTrue(gd._wait_for_interactable(SEL_REVIEW_CHECKOUT, timeout=1))

    def test_atc_ready_check_uses_closest_control_parent(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = True
        with patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(True, True)), \
             patch.object(gd, "_wait_for_url_or_capture"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch("modules.cdp.driver.time.sleep"):
            gd.add_to_cart_and_checkout()
        scripts = [c.args[0] for c in gd._driver.execute_script.call_args_list]
        self.assertTrue(any("closest('button,a,[role=\"button\"],.btn')" in s for s in scripts))

    def test_review_checkout_ready_requires_visible_nonzero_button(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.side_effect = [
            {"present": True, "enabled": False},
            {"present": True, "enabled": True},
        ]
        with patch("modules.cdp.driver.time.sleep"):
            self.assertEqual(gd._wait_for_review_checkout_enabled(timeout=1), (True, True))
        script = gd._driver.execute_script.call_args_list[0].args[0]
        self.assertIn("style.display!=='none'", script)
        self.assertIn("style.visibility!=='hidden'", script)
        self.assertIn("rect.width>0", script)
        self.assertIn("rect.height>0", script)

class AtcBlueprintWaitTests(unittest.TestCase):
    def test_atc_sleeps_at_least_3s_before_review_checkout(self):
        gd = GivexDriver(_make_driver(current_url=URL_CART), strict=False)
        sleeps = []
        with patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(True, True)), \
             patch.object(gd, "_wait_for_url_or_capture"), \
             patch("modules.cdp.driver.time.sleep", side_effect=sleeps.append):
            gd.add_to_cart_and_checkout()
        self.assertTrue(any(s >= 3.0 for s in sleeps))

    def test_atc_uses_interactable_not_just_element(self):
        gd = GivexDriver(_make_driver(current_url=URL_CART), strict=False)
        with patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True) as wait_inter, \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(True, True)), \
             patch.object(gd, "_wait_for_element") as wait_element, \
             patch.object(gd, "_wait_for_url_or_capture"), \
             patch("modules.cdp.driver.time.sleep"):
            gd.add_to_cart_and_checkout()
        wait_inter.assert_called_once_with(drv.SEL_ADD_TO_CART, timeout=8)
        wait_element.assert_not_called()

    def test_atc_logs_review_checkout_failure_without_pii(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(False, False)), \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="ERROR") as logs:
            with self.assertRaises(SelectorTimeoutError):
                gd.add_to_cart_and_checkout()
        self.assertIn("Review-Checkout not found", "\n".join(logs.output))
        self.assertNotIn("@example.com", "\n".join(logs.output))

    def test_atc_absent_timeout_includes_blueprint_wait_and_poll(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(False, False)), \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(SelectorTimeoutError) as ctx:
                gd.add_to_cart_and_checkout()
        self.assertEqual(ctx.exception.timeout, 21)
        self.assertIsNone(ctx.exception.reason)
        self.assertIsInstance(ctx.exception, SessionFlaggedError)

    def test_atc_present_disabled_timeout_is_distinct(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(False, True)), \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver.time.sleep"):
            with self.assertRaisesRegex(SelectorTimeoutError, "present but disabled") as ctx:
                gd.add_to_cart_and_checkout()
        self.assertEqual(ctx.exception.timeout, 21)
        self.assertEqual(ctx.exception.reason, "present but disabled")
        self.assertIsInstance(ctx.exception, SessionFlaggedError)


if __name__ == "__main__":
    unittest.main()
