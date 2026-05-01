import re as _re
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
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(True, True)), \
             patch.object(gd, "_wait_for_url_or_capture"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch("modules.cdp.driver.time.sleep"):
            gd.add_to_cart_and_checkout()
        scripts = [c.args[0] for c in gd._driver.execute_script.call_args_list]
        self.assertTrue(any("closest('button,a,[role=\"button\"],.btn,#cws_btn_gcBuyAdd')" in s for s in scripts))
        self.assertTrue(any("#cws_btn_gcBuyAdd" in s for s in scripts))

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
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
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
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
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
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
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
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(False, False)), \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(SelectorTimeoutError) as ctx:
                gd.add_to_cart_and_checkout()
        self.assertGreaterEqual(ctx.exception.timeout, 0)
        self.assertEqual(ctx.exception.reason, "review checkout absent")
        self.assertIsInstance(ctx.exception, SessionFlaggedError)

    def test_atc_present_disabled_timeout_is_distinct(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(False, True)), \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver.time.sleep"):
            with self.assertRaisesRegex(SelectorTimeoutError, "present but disabled") as ctx:
                gd.add_to_cart_and_checkout()
        self.assertGreaterEqual(ctx.exception.timeout, 0)
        self.assertEqual(ctx.exception.reason, "present but disabled")
        self.assertIsInstance(ctx.exception, SessionFlaggedError)

    def test_atc_cart_state_timeout_is_distinct_session_flagged(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "_click_closest_control_for"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_review_checkout_diagnostics", return_value={}), \
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(False, {})), \
             patch.object(gd, "_wait_for_review_checkout_enabled") as review_wait, \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver.time.sleep"):
            with self.assertRaisesRegex(SelectorTimeoutError, "cart total not materialized") as ctx:
                gd.add_to_cart_and_checkout()
        self.assertGreaterEqual(ctx.exception.timeout, 0)
        self.assertEqual(ctx.exception.reason, "cart total not materialized")
        self.assertIsInstance(ctx.exception, SessionFlaggedError)
        review_wait.assert_not_called()

    def test_click_closest_control_for_uses_control_rect_not_span_rect(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = {
            "span": {"x": 100, "y": 100, "w": 35, "h": 19},
            "control": {"x": 10, "y": 20, "w": 313, "h": 37},
        }
        with patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_ghost_move_to") as move, \
             patch.object(gd, "cdp_click_absolute") as click:
            gd._click_closest_control_for(drv.SEL_ADD_TO_CART)
        move.assert_called_once_with(drv.SEL_ADD_TO_CART)
        click.assert_called_once()
        x, y = click.call_args.args
        self.assertGreaterEqual(x, 10)
        self.assertLessEqual(x, 323)
        self.assertGreaterEqual(y, 20)
        self.assertLessEqual(y, 57)
        script = gd._driver.execute_script.call_args.args[0]
        self.assertIn("#cws_btn_gcBuyAdd", script)

    def test_click_closest_control_missing_anchor_raises_selector_timeout(self):
        gd = GivexDriver(_make_driver(), strict=True)
        gd._driver.execute_script.return_value = None
        with self.assertRaises(SelectorTimeoutError) as ctx:
            gd._click_closest_control_for(drv.SEL_ADD_TO_CART)
        self.assertEqual(ctx.exception.selector, drv.SEL_ADD_TO_CART)

    def test_wait_for_cart_state_accepts_total_delta(self):
        gd = GivexDriver(_make_driver(), strict=False)
        baseline = {"total_like_present": False, "cart_like_visible_count": 1}
        post = {"total_like_present": True, "cart_like_visible_count": 1}
        with patch.object(gd, "_cart_state_snapshot", return_value=post), \
             patch.object(gd, "_review_checkout_diagnostics") as full_diag, \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            materialized, snapshot = gd._wait_for_cart_state_after_atc(baseline, timeout=1)
        self.assertTrue(materialized)
        self.assertIs(snapshot, post)
        full_diag.assert_not_called()
        self.assertIn("signal=total_like_present", "\n".join(logs.output))

    def test_wait_for_cart_state_accepts_explicit_line_item_delta(self):
        gd = GivexDriver(_make_driver(), strict=False)
        baseline = {
            "total_like_present": False,
            "explicit_cart_line_item_count": 0,
            "explicit_cart_line_item_visible_count": 0,
            "cart_like_visible_count": 1,
        }
        post = {
            "total_like_present": False,
            "explicit_cart_line_item_count": 1,
            "explicit_cart_line_item_visible_count": 1,
            "cart_like_visible_count": 1,
        }
        with patch.object(gd, "_cart_state_snapshot", return_value=post):
            materialized, snapshot = gd._wait_for_cart_state_after_atc(baseline, timeout=1)
        self.assertTrue(materialized)
        self.assertIs(snapshot, post)

    def test_wait_for_cart_state_accepts_review_enabled_without_total(self):
        gd = GivexDriver(_make_driver(), strict=False)
        baseline = {
            "total_like_present": False,
            "cart_like_visible_count": 1,
            "review_checkout": {"present": True, "enabled": False},
        }
        post = {
            "total_like_present": False,
            "cart_like_visible_count": 1,
            "review_checkout": {"present": True, "enabled": True},
        }
        with patch.object(gd, "_cart_state_snapshot", return_value=post), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            materialized, snapshot = gd._wait_for_cart_state_after_atc(baseline, timeout=1)
        self.assertTrue(materialized)
        self.assertIs(snapshot, post)
        self.assertIn("signal=review_checkout_enabled_without_total", "\n".join(logs.output))

    def test_wait_for_cart_state_rejects_review_enabled_when_baseline_already_enabled(self):
        gd = GivexDriver(_make_driver(), strict=False)
        baseline = {
            "total_like_present": False,
            "cart_like_visible_count": 1,
            "review_checkout": {"present": True, "enabled": True},
        }
        post = dict(baseline)
        with patch.object(gd, "_cart_state_snapshot", return_value=post), \
             patch.object(gd, "_review_checkout_diagnostics") as full_diag, \
             patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.monotonic", side_effect=[0.0, 0.0, 2.0]):
            materialized, snapshot = gd._wait_for_cart_state_after_atc(baseline, timeout=1)
        self.assertFalse(materialized)
        self.assertIs(snapshot, post)
        full_diag.assert_not_called()

    def test_wait_for_cart_state_does_not_accept_cart_icon_delta_alone(self):
        gd = GivexDriver(_make_driver(), strict=False)
        baseline = {"total_like_present": False, "cart_like_visible_count": 1}
        post = {"total_like_present": False, "cart_like_visible_count": 2}
        with patch.object(gd, "_cart_state_snapshot", return_value=post), \
             patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.monotonic", side_effect=[0.0, 0.0, 2.0]):
            materialized, snapshot = gd._wait_for_cart_state_after_atc(baseline, timeout=1)
        self.assertFalse(materialized)
        self.assertIs(snapshot, post)

    def test_atc_review_timeout_reports_actual_elapsed_including_cart_poll(self):
        gd = GivexDriver(_make_driver(), strict=False)
        with patch.object(gd, "_click_closest_control_for"), \
             patch.object(gd, "_get_rng", return_value=LowBoundRng()), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_review_checkout_diagnostics", return_value={}), \
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(False, True)), \
             patch.object(gd, "_capture_failure_screenshot"), \
             patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.monotonic", side_effect=[0, 10, 10, 13, 20, 20, 25, 49]), \
             self.assertLogs("modules.cdp.driver", level="ERROR") as logs:
            with self.assertRaises(SelectorTimeoutError) as ctx:
                gd.add_to_cart_and_checkout()
        self.assertEqual(ctx.exception.timeout, 39)
        joined = "\n".join(logs.output)
        self.assertIn("blueprint_wait=", joined)
        self.assertIn("cart_poll_elapsed=7.00s", joined)
        self.assertIn("review_poll=5.00s", joined)
        self.assertIn("total_elapsed=39.00s", joined)

    def test_cart_log_snapshot_excludes_raw_values(self):
        snapshot = {
            "total_like_present": False,
            "cart_like_visible_count": 1,
            "recipient_email": "recipient@example.com",
            "form_validation": {"raw": "recipient@example.com"},
            "review_checkout": {
                "present": True,
                "enabled": False,
                "text_len": 15,
                "innerText": "recipient@example.com",
            },
        }
        logged = GivexDriver._cart_log_snapshot(snapshot)
        self.assertEqual(logged["cart_like_visible_count"], 1)
        self.assertNotIn("recipient_email", logged)
        self.assertNotIn("form_validation", logged)
        self.assertNotIn("review_checkout", logged)
        self.assertNotIn("recipient@example.com", repr(logged))


_CANONICAL_AUDIT = {
    "current_url_path": "/cws4.0/lushusa/e-gifts/",
    "body_html_len": 182340,
    "cws_id_count": 47,
    "cws_class_count": 12,
    "add_to_cart_present": True,
    "add_to_cart_state": {"present": True, "enabled": True, "disabled": False, "visible": True},
    "review_checkout_present": False,
    "review_checkout_state": {"present": False, "enabled": False, "disabled": None},
    "cart_container_count": 3,
    "cart_container_visible_count": 1,
    "alt_line_item_patterns": {"cws_underscored": 0, "table_rows_in_cart": 0, "list_items_in_cart": 0},
    "alt_total_patterns": {"grand": 0, "sub": 0, "order": 0, "cws": 0},
    "sample_cws_ids": ["cws_btn_gcBuyAdd", "cws_btn_gcBuyCheckout", "cws_div_cart"],
    "alert_count": 0,
    "alert_visible_count": 0,
}

_FORBIDDEN_PII_PATTERN = _re.compile(
    r"innerText|outerHTML|innerHTML|placeholder|cookie|storage|email|sender|recipient|card|password",
    _re.I,
)


class CartDomAuditTests(unittest.TestCase):
    def test_cart_dom_audit_returns_pii_safe_fields(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script = MagicMock(return_value=_CANONICAL_AUDIT)
        audit = gd._cart_dom_audit()
        self.assertIsInstance(audit, dict)
        required_keys = [
            "current_url_path",
            "body_html_len",
            "cws_id_count",
            "cws_class_count",
            "add_to_cart_present",
            "add_to_cart_state",
            "review_checkout_present",
            "review_checkout_state",
            "cart_container_count",
            "cart_container_visible_count",
            "alt_line_item_patterns",
            "alt_total_patterns",
            "sample_cws_ids",
            "alert_count",
            "alert_visible_count",
        ]
        for k in required_keys:
            self.assertIn(k, audit, f"Missing required key: {k}")
        for k in ("cws_id_count", "cws_class_count", "body_html_len",
                  "cart_container_count", "cart_container_visible_count",
                  "alert_count", "alert_visible_count"):
            self.assertIsInstance(audit[k], int, f"{k} should be int")
        for k in ("add_to_cart_present", "review_checkout_present"):
            self.assertIsInstance(audit[k], bool, f"{k} should be bool")
        for state_key in ("add_to_cart_state", "review_checkout_state"):
            s = audit[state_key]
            self.assertIsInstance(s, dict)
            self.assertIsInstance(s["present"], bool)
            self.assertIsInstance(s["enabled"], bool)
        self.assertIsInstance(audit["sample_cws_ids"], list)
        self.assertLessEqual(len(audit["sample_cws_ids"]), 30)
        for sid in audit["sample_cws_ids"]:
            self.assertIsInstance(sid, str)
        self.assertNotIn("?", audit["current_url_path"])
        self.assertNotIn("#", audit["current_url_path"])
        forbidden = _FORBIDDEN_PII_PATTERN
        for k in audit:
            self.assertIsNone(forbidden.search(k), f"Forbidden key found: {k}")
        self.assertNotRegex(repr(audit), r"@[^@\s]+\.")

    def test_cart_dom_audit_logged_on_timeout(self):
        gd = GivexDriver(_make_driver(), strict=False)
        non_materializing = {
            "total_like_present": False,
            "explicit_cart_line_item_count": 0,
            "explicit_cart_line_item_visible_count": 0,
            "cart_like_visible_count": 1,
            "error_like_visible_count": 0,
        }
        with patch.object(gd, "_cart_state_snapshot", return_value=non_materializing), \
             patch.object(gd, "_cart_dom_audit", return_value=_CANONICAL_AUDIT) as mock_audit, \
             patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.monotonic", side_effect=[0.0, 0.0, 2.0]), \
             self.assertLogs("modules.cdp.driver", level="ERROR") as logs:
            result = gd._wait_for_cart_state_after_atc(
                baseline={"total_like_present": False, "cart_like_visible_count": 1},
                timeout=1,
            )
        self.assertEqual(result[0], False)
        mock_audit.assert_called_once()
        joined = "\n".join(logs.output)
        self.assertIn("dom_audit=", joined)
        self.assertIn("cws_id_count", joined)

    def test_cart_dom_audit_not_called_on_success(self):
        gd = GivexDriver(_make_driver(), strict=False)
        materializing = {
            "total_like_present": True,
            "explicit_cart_line_item_count": 0,
            "explicit_cart_line_item_visible_count": 0,
            "cart_like_visible_count": 1,
        }
        mock_audit = MagicMock()
        with patch.object(gd, "_cart_state_snapshot", return_value=materializing), \
             patch.object(gd, "_cart_dom_audit", mock_audit):
            ok, snapshot = gd._wait_for_cart_state_after_atc(
                baseline={"total_like_present": False, "cart_like_visible_count": 1},
                timeout=1,
            )
        self.assertTrue(ok)
        self.assertIs(snapshot, materializing)
        mock_audit.assert_not_called()

    def test_cart_dom_audit_does_not_include_form_values_or_inner_text(self):
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script = MagicMock(return_value=_CANONICAL_AUDIT)
        audit = gd._cart_dom_audit()
        for k in audit:
            self.assertIsNone(_FORBIDDEN_PII_PATTERN.search(k), f"Forbidden key found in audit result: {k}")

    def test_cart_state_snapshot_keys_present_reflects_actual_timeout_snapshot(self):
        gd = GivexDriver(_make_driver(), strict=False)
        partial_snapshot = {"total_like_present": False, "cart_like_visible_count": 1}
        non_materializing = dict(partial_snapshot)
        audit_without_keys = dict(_CANONICAL_AUDIT)
        audit_without_keys.pop("cart_state_snapshot_keys_present", None)
        with patch.object(gd, "_cart_state_snapshot", return_value=non_materializing), \
             patch.object(gd, "_cart_dom_audit", return_value=audit_without_keys), \
             patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.monotonic", side_effect=[0.0, 0.0, 2.0]), \
             self.assertLogs("modules.cdp.driver", level="ERROR") as logs:
            result = gd._wait_for_cart_state_after_atc(
                baseline={"total_like_present": False, "cart_like_visible_count": 1},
                timeout=1,
            )
        self.assertEqual(result[0], False)
        joined = "\n".join(logs.output)
        self.assertIn("dom_audit=", joined)
        self.assertIn("cart_state_snapshot_keys_present", joined)
        self.assertIn("'cart_like_visible_count'", joined)
        self.assertIn("'total_like_present'", joined)
        # Must be sorted alphabetically and contain only the 2 actual keys
        self.assertIn("['cart_like_visible_count', 'total_like_present']", joined)

    def test_cart_state_snapshot_keys_present_empty_when_snapshot_missing(self):
        gd = GivexDriver(_make_driver(), strict=False)
        empty_snapshot: dict = {}
        audit_without_keys = dict(_CANONICAL_AUDIT)
        audit_without_keys.pop("cart_state_snapshot_keys_present", None)
        with patch.object(gd, "_cart_state_snapshot", return_value=empty_snapshot), \
             patch.object(gd, "_cart_dom_audit", return_value=audit_without_keys), \
             patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.monotonic", side_effect=[0.0, 0.0, 2.0]), \
             self.assertLogs("modules.cdp.driver", level="ERROR") as logs:
            result = gd._wait_for_cart_state_after_atc(
                baseline={},
                timeout=1,
            )
        self.assertEqual(result[0], False)
        joined = "\n".join(logs.output)
        self.assertIn("dom_audit=", joined)
        self.assertIn("'cart_state_snapshot_keys_present': []", joined)

    def test_cart_dom_audit_sample_cws_ids_filters_only_card_password_ssn(self):
        import inspect
        import re

        _SAFE_CWS_ID_RE = re.compile(r"^cws_", re.I)
        _UNSAFE_CWS_ID_RE = re.compile(r"cc(num|cvv|exp|name)|password|ssn", re.I)

        def _python_safe_cws_id(id_):
            return bool(_SAFE_CWS_ID_RE.match(id_ or "")) and not _UNSAFE_CWS_ID_RE.search(id_ or "")

        # IDs that must be KEPT
        keep = [
            "cws_btn_gcBuyAdd",
            "cws_btn_gcBuyCheckout",
            "cws_div_cart",
            "cws_lbl_subtotalAmount",
            "cws_txt_orderTotal",
            "cws_div_cartItemName_0",
            "cws_txt_recipEmail",
            "cws_txt_gcBuyFrom",
            "cws_txt_billingAddr1",
        ]
        # IDs that must be EXCLUDED
        exclude = [
            "cws_txt_ccNum",
            "cws_txt_ccCvv",
            "cws_txt_ccExpMon",
            "cws_txt_ccName",
            "cws_password_field",
            "cws_ssn_input",
        ]
        for id_ in keep:
            self.assertTrue(_python_safe_cws_id(id_), f"Expected KEEP but got EXCLUDE: {id_}")
        for id_ in exclude:
            self.assertFalse(_python_safe_cws_id(id_), f"Expected EXCLUDE but got KEEP: {id_}")

        # Verify the JS source contains safeCwsId and the correct regex pattern
        source = inspect.getsource(GivexDriver._cart_dom_audit)
        self.assertIn("safeCwsId", source)
        self.assertIn("cc(num|cvv|exp|name)|password|ssn", source)


class SelectCardDesignTests(unittest.TestCase):
    """Tests for GivexDriver._select_card_design_if_required()."""

    _CANDIDATES_6 = [
        {"id": f"cws_lbl_41576{i}", "x": 10.0 + i * 90, "y": 100.0, "w": 80.0, "h": 40.0}
        for i in range(6)
    ]
    _POST_RECT = {"x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0, "in_viewport": True}
    _STATE_BEFORE = {
        "value_present": True,
        "value_text_len": 0,
        "value_value_len": 0,
        "value_attr_len": 5,
        "selected_like_count": 0,
        "visible_option_count": 6,
    }
    _STATE_AFTER = {
        "value_present": True,
        "value_text_len": 8,
        "value_value_len": 0,
        "value_attr_len": 5,
        "selected_like_count": 1,
        "visible_option_count": 6,
    }

    def _make_gd_with_script_side_effect(self, side_effects):
        selenium = _make_driver()
        selenium.execute_script.side_effect = list(side_effects)
        return GivexDriver(selenium, strict=False)

    def test_select_card_design_clicks_visible_candidate(self):
        """Clicking one of 6 candidates should call cdp_click_absolute once, no error."""
        gd = self._make_gd_with_script_side_effect([
            self._CANDIDATES_6,    # detect
            self._STATE_BEFORE,    # state_before snapshot
            None,                  # scroll
            self._POST_RECT,       # post-scroll rect
            self._STATE_AFTER,     # state_after snapshot
        ])
        with patch.object(gd, "cdp_click_absolute") as mock_click, \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._select_card_design_if_required()
        mock_click.assert_called_once()
        x, y = mock_click.call_args.args
        # click center = 10 + 80/2 = 50 (or nearby, depends on rng idx)
        self.assertIsInstance(x, float)
        self.assertIsInstance(y, float)

    def test_select_card_design_no_op_when_no_picker(self):
        """Empty candidates list must produce no click and an INFO log."""
        selenium = _make_driver()
        selenium.execute_script.return_value = []
        gd = GivexDriver(selenium, strict=False)
        with patch.object(gd, "cdp_click_absolute") as mock_click, \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            gd._select_card_design_if_required()
        mock_click.assert_not_called()
        self.assertIn("no_picker_detected", "\n".join(logs.output))

    def test_select_card_design_raises_when_state_unchanged(self):
        """If state_before == state_after on all signals → SessionFlaggedError."""
        state_same = {
            "value_present": True,
            "value_text_len": 5,
            "value_value_len": 0,
            "value_attr_len": 5,
            "selected_like_count": 1,
            "visible_option_count": 1,
        }
        candidates = [{"id": "cws_lbl_415760", "x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0}]
        gd = self._make_gd_with_script_side_effect([
            candidates,                           # detect
            state_same,                           # state_before
            None,                                 # scroll
            {"x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0, "in_viewport": True},  # post-rect
            state_same,                           # state_after (unchanged)
        ])
        with patch.object(gd, "cdp_click_absolute"), \
             patch.object(gd, "_capture_failure_screenshot"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            with self.assertRaisesRegex(SessionFlaggedError, "Card design"):
                gd._select_card_design_if_required()

    def test_select_card_design_log_does_not_contain_raw_text_or_innertext(self):
        """INFO log line must not contain raw IDs, innerText, outerHTML, or PII."""
        gd = self._make_gd_with_script_side_effect([
            self._CANDIDATES_6,
            self._STATE_BEFORE,
            None,
            self._POST_RECT,
            self._STATE_AFTER,
        ])
        with patch.object(gd, "cdp_click_absolute"), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            gd._select_card_design_if_required()
        info_lines = [l for l in logs.output if "INFO" in l and "card_design" in l]
        self.assertTrue(info_lines, "Expected at least one INFO card_design log line")
        combined = "\n".join(info_lines)
        # No raw ID strings at INFO level (IDs contain the full cws_lbl_NNNNNN pattern)
        for cand in self._CANDIDATES_6:
            self.assertNotIn(cand["id"], combined,
                             f"Raw candidate ID {cand['id']!r} must not appear in INFO log")
        # No DOM content keywords
        for forbidden in ("innerText", "outerHTML", "innerHTML", "textContent"):
            self.assertNotIn(forbidden, combined)

    def test_fill_egift_form_calls_card_design_before_completion(self):
        """_select_card_design_if_required must be called BEFORE final validation."""
        gd = GivexDriver(_make_driver(), strict=False)
        order = []

        def spy_card_design():
            order.append("card_design")

        def spy_field_value_length(_sel):
            order.append("final_validation")
            return 10

        with patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally", return_value=True), \
             patch.object(gd, "_field_value_length", side_effect=spy_field_value_length), \
             patch.object(gd, "_field_value", return_value="recipient@example.com"), \
             patch.object(gd, "_select_card_design_if_required", side_effect=spy_card_design), \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            gd.fill_egift_form(_make_task(), _make_billing())
        log_text = "\n".join(logs.output)
        # Both events must have occurred
        self.assertIn("card_design", order, "_select_card_design_if_required was not called")
        self.assertIn("final_validation", order, "final_validation (_field_value_length) was not called")
        # AND card_design must come BEFORE final_validation
        self.assertLess(
            order.index("card_design"),
            order.index("final_validation"),
            f"card_design must be called before final_validation; actual order={order}",
        )
        self.assertIn("fill_egift_form: completed", log_text)

    def test_select_card_design_verification_passes_on_any_signal_change(self):
        """Any one changed signal is sufficient to pass verification (no raise)."""
        candidates = [{"id": "cws_lbl_415760", "x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0}]
        post_rect = {"x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0, "in_viewport": True}
        base = {
            "value_present": True,
            "value_text_len": 0,
            "value_value_len": 0,
            "value_attr_len": 5,
            "selected_like_count": 1,
            "visible_option_count": 1,
        }
        signal_cases = [
            ("value_text_len", 8),
            ("value_value_len", 3),
            ("value_attr_len", 12),
            ("selected_like_count", 2),
        ]
        for signal_key, new_val in signal_cases:
            with self.subTest(signal=signal_key):
                state_after = dict(base)
                state_after[signal_key] = new_val
                gd = self._make_gd_with_script_side_effect([
                    candidates,   # detect
                    dict(base),   # state_before
                    None,         # scroll
                    post_rect,    # post-rect
                    state_after,  # state_after with one signal changed
                ])
                with patch.object(gd, "cdp_click_absolute"), \
                     self.assertLogs("modules.cdp.driver", level="INFO"):
                    # Must not raise
                    gd._select_card_design_if_required()


    def test_select_card_design_raises_when_post_rect_unavailable(self):
        """SessionFlaggedError when post-scroll rect is unavailable (None / empty / zero-size)."""
        candidates = [{"id": "cws_lbl_415760", "x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0}]
        state_before = {
            "value_present": True,
            "value_text_len": 0,
            "value_value_len": 0,
            "value_attr_len": 5,
            "selected_like_count": 0,
            "visible_option_count": 1,
        }
        for label, bad_rect in [
            ("none_rect", None),
            ("empty_dict", {}),
            ("zero_size", {"x": 10.0, "y": 100.0, "w": 0, "h": 0}),
        ]:
            with self.subTest(label):
                gd = self._make_gd_with_script_side_effect([
                    candidates,   # detect
                    state_before, # state_before
                    None,         # scroll
                    bad_rect,     # post-rect (unavailable)
                ])
                with patch.object(gd, "cdp_click_absolute") as mock_click, \
                     patch.object(gd, "_capture_failure_screenshot") as mock_shot:
                    with self.assertRaisesRegex(SessionFlaggedError, "post-scroll rect unavailable"):
                        gd._select_card_design_if_required()
                mock_click.assert_not_called()
                mock_shot.assert_called_once_with("card_design_post_rect_unavailable")

    def test_select_card_design_raises_when_offscreen_after_scroll(self):
        """SessionFlaggedError when element reports in_viewport=False after scroll."""
        candidates = [{"id": "cws_lbl_415760", "x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0}]
        state_before = {
            "value_present": True,
            "value_text_len": 0,
            "value_value_len": 0,
            "value_attr_len": 5,
            "selected_like_count": 0,
            "visible_option_count": 1,
        }
        offscreen_rect = {"x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0, "in_viewport": False}
        gd = self._make_gd_with_script_side_effect([
            candidates,     # detect
            state_before,   # state_before
            None,           # scroll
            offscreen_rect, # post-rect (offscreen)
        ])
        with patch.object(gd, "cdp_click_absolute") as mock_click, \
             patch.object(gd, "_capture_failure_screenshot") as mock_shot:
            with self.assertRaisesRegex(SessionFlaggedError, "offscreen after scroll"):
                gd._select_card_design_if_required()
        mock_click.assert_not_called()
        mock_shot.assert_called_once_with("card_design_offscreen")

    def test_select_card_design_raises_when_center_outside_viewport_partial_intersect(self):
        """SessionFlaggedError for partial intersection where click-center is outside viewport.

        Emulates the edge case: element at x=-50, w=60 intersects the left
        viewport edge (right=10 > 0) but its center is at cx=-20, which is
        outside the viewport.  The updated center-point JS returns
        in_viewport=False for this case, preventing cdp_click_absolute from
        being called with a negative coordinate.
        """
        candidates = [{"id": "cws_lbl_415760", "x": -50.0, "y": 100.0, "w": 60.0, "h": 40.0}]
        state_before = {
            "value_present": True,
            "value_text_len": 0,
            "value_value_len": 0,
            "value_attr_len": 5,
            "selected_like_count": 0,
            "visible_option_count": 1,
        }
        # Partial-intersection rect: rect intersects viewport but center (cx=-20) is outside.
        # The updated center-point JS sets in_viewport=False for this geometry.
        partial_rect = {"x": -50.0, "y": 100.0, "w": 60.0, "h": 40.0, "in_viewport": False}
        gd = self._make_gd_with_script_side_effect([
            candidates,    # detect
            state_before,  # state_before
            None,          # scroll
            partial_rect,  # post-rect (center outside viewport)
        ])
        with patch.object(gd, "cdp_click_absolute") as mock_click, \
             patch.object(gd, "_capture_failure_screenshot") as mock_shot:
            with self.assertRaisesRegex(SessionFlaggedError, "offscreen after scroll"):
                gd._select_card_design_if_required()
        mock_click.assert_not_called()
        mock_shot.assert_called_once_with("card_design_offscreen")


# ── Round-4 new tests ────────────────────────────────────────────────────────


class Round4TimingOrderTests(unittest.TestCase):
    """Fix 1 — _select_card_design_if_required() is called BEFORE _smooth_scroll_to."""

    def test_card_design_called_before_smooth_scroll_to(self):
        """`_select_card_design_if_required` must fire before `_smooth_scroll_to`."""
        gd = GivexDriver(_make_driver(), strict=False)
        order = []

        def _spy_design():
            order.append("card_design")

        def _spy_scroll(_sel):
            order.append("scroll")

        with patch.object(gd, "_select_card_design_if_required", side_effect=_spy_design), \
             patch.object(gd, "_smooth_scroll_to", side_effect=_spy_scroll), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally", return_value=True), \
             patch.object(gd, "_field_value_length", return_value=10), \
             patch.object(gd, "_field_value", return_value="recipient@example.com"), \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd.fill_egift_form(_make_task(), _make_billing())
        self.assertIn("card_design", order)
        self.assertIn("scroll", order)
        self.assertLess(
            order.index("card_design"),
            order.index("scroll"),
            f"card_design must precede smooth_scroll_to; actual order={order}",
        )

    def test_card_design_called_exactly_once(self):
        """`_select_card_design_if_required` must be called exactly once."""
        gd = GivexDriver(_make_driver(), strict=False)
        call_count = []

        def _spy_design():
            call_count.append(1)

        with patch.object(gd, "_select_card_design_if_required", side_effect=_spy_design), \
             patch.object(gd, "_smooth_scroll_to"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch.object(gd, "_realistic_type_field"), \
             patch.object(gd, "_blur_active_field_naturally", return_value=True), \
             patch.object(gd, "_field_value_length", return_value=10), \
             patch.object(gd, "_field_value", return_value="recipient@example.com"), \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd.fill_egift_form(_make_task(), _make_billing())
        self.assertEqual(len(call_count), 1, "_select_card_design_if_required called more than once")


class Round4DetectionTests(unittest.TestCase):
    """Fix 2 — detection JS scoped to containers, using getElementById for radios."""

    def _make_gd(self, side_effects):
        selenium = _make_driver()
        selenium.execute_script.side_effect = list(side_effects)
        return GivexDriver(selenium, strict=False)

    def test_detect_js_scopes_to_form_select_card(self):
        """Detection script must reference #form--select-card container."""
        gd = GivexDriver(_make_driver(), strict=False)
        gd._driver.execute_script.return_value = []
        import inspect
        src = inspect.getsource(gd._select_card_design_if_required)
        self.assertIn("#form--select-card", src)

    def test_detect_js_scopes_to_cards_container(self):
        """Detection script must reference #cardsContainer container."""
        gd = GivexDriver(_make_driver(), strict=False)
        import inspect
        src = inspect.getsource(gd._select_card_design_if_required)
        self.assertIn("#cardsContainer", src)

    def test_detect_js_uses_get_element_by_id_not_css_query(self):
        """Radio lookup must use getElementById (CSS cannot start with digit)."""
        gd = GivexDriver(_make_driver(), strict=False)
        import inspect
        src = inspect.getsource(gd._select_card_design_if_required)
        self.assertIn("getElementById", src)
        # Must NOT use querySelector with bare numeric id
        self.assertNotIn('querySelector("#4', src)

    def test_returns_candidate_with_valid_label_and_radio(self):
        """A candidate with numeric-suffix label + matching radio → accepted."""
        # Simulate JS returning a single valid candidate (as the real DOM would)
        candidates = [
            {"id": "cws_lbl_415760", "radio_id_len": 6, "x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0}
        ]
        state = {"checked_count": 0, "preview_src_len": -1, "preview_name_len": -1,
                 "clicked_radio_checked": False,
                 "value_text_len": 0, "value_value_len": 0, "value_attr_len": 0,
                 "selected_like_count": 0, "visible_option_count": 1}
        post_rect = {"x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0, "in_viewport": True}
        state_after = dict(state)
        state_after["clicked_radio_checked"] = True  # radio is now checked
        gd = self._make_gd([candidates, state, None, post_rect, state_after])
        with patch.object(gd, "cdp_click_absolute") as mock_click, \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._select_card_design_if_required()
        mock_click.assert_called_once()

    def test_no_picker_detected_when_js_returns_none(self):
        """When execute_script returns None (no container), skip gracefully."""
        selenium = _make_driver()
        selenium.execute_script.return_value = None
        gd = GivexDriver(selenium, strict=False)
        with patch.object(gd, "cdp_click_absolute") as mock_click, \
             patch("modules.cdp.driver.time.sleep") as mock_sleep, \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            gd._select_card_design_if_required()
        mock_click.assert_not_called()
        mock_sleep.assert_not_called()
        self.assertEqual(selenium.execute_script.call_count, 1)
        self.assertIn("no_picker_detected", "\n".join(logs.output))

    def test_container_present_empty_polls_until_candidate(self):
        """Empty list means picker exists but candidates are still rendering."""
        candidates = [
            {"id": "cws_lbl_415760", "radio_id_len": 6, "x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0}
        ]
        state = {"checked_count": 0, "preview_src_len": -1, "preview_name_len": -1,
                 "clicked_radio_checked": False,
                 "value_text_len": 0, "value_value_len": 0, "value_attr_len": 0,
                 "selected_like_count": 0, "visible_option_count": 1}
        state_after = dict(state)
        state_after["clicked_radio_checked"] = True
        gd = self._make_gd([
            [], candidates, state, None,
            {"x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0, "in_viewport": True},
            state_after,
        ])
        with patch.object(gd, "cdp_click_absolute") as mock_click, \
             patch("modules.cdp.driver.time.sleep") as mock_sleep, \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._select_card_design_if_required()
        mock_sleep.assert_called()
        self.assertGreaterEqual(gd._driver.execute_script.call_count, 2)
        mock_click.assert_called_once()

    def test_no_picker_detected_does_not_raise(self):
        """no_picker_detected path must return gracefully (no exception)."""
        selenium = _make_driver()
        selenium.execute_script.return_value = None
        gd = GivexDriver(selenium, strict=False)
        with self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            try:
                gd._select_card_design_if_required()
            except Exception as exc:  # pylint: disable=broad-except
                self.fail(f"_select_card_design_if_required raised unexpectedly: {exc}")
        self.assertIn("no_picker_detected", "\n".join(logs.output))

    def test_non_numeric_labels_excluded_from_candidates(self):
        """Labels like cws_lbl_gcMsg must NOT be treated as card design picks."""
        import inspect
        src = inspect.getsource(GivexDriver._select_card_design_if_required)
        self.assertIn(r"cws_lbl_\d{6}", src)
        self.assertIn("radio.type==='radio'", src)


class Round4VerificationTests(unittest.TestCase):
    """Fix 3 — verification signals: clicked_radio_checked, checked_count, preview."""

    _BASE_BEFORE = {
        "clicked_radio_checked": False,
        "checked_count": 0,
        "preview_src_len": -1,
        "preview_name_len": -1,
        "value_text_len": 0,
        "value_value_len": 0,
        "value_attr_len": 0,
        "selected_like_count": 0,
    }

    def _make_gd(self, candidates, state_before, state_after):
        side_effects = [
            candidates,
            state_before,
            None,  # scroll
            {"x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0, "in_viewport": True},
            state_after,
        ]
        selenium = _make_driver()
        selenium.execute_script.side_effect = list(side_effects)
        return GivexDriver(selenium, strict=False)

    def _candidates(self):
        return [{"id": "cws_lbl_415760", "radio_id_len": 6,
                 "x": 10.0, "y": 100.0, "w": 80.0, "h": 40.0}]

    def test_clicked_radio_checked_is_sufficient(self):
        """clicked_radio_checked=True alone → no raise."""
        after = dict(self._BASE_BEFORE)
        after["clicked_radio_checked"] = True
        gd = self._make_gd(self._candidates(), dict(self._BASE_BEFORE), after)
        with patch.object(gd, "cdp_click_absolute"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._select_card_design_if_required()  # must not raise

    def test_checked_count_increase_is_sufficient(self):
        """checked_count increasing from 0 → 1 alone → no raise."""
        after = dict(self._BASE_BEFORE)
        after["checked_count"] = 1
        gd = self._make_gd(self._candidates(), dict(self._BASE_BEFORE), after)
        with patch.object(gd, "cdp_click_absolute"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._select_card_design_if_required()  # must not raise

    def test_preview_src_len_change_is_sufficient(self):
        """#cardPreview src length changing (from -1 to 100) alone → no raise."""
        before = dict(self._BASE_BEFORE)
        before["preview_src_len"] = -1
        after = dict(self._BASE_BEFORE)
        after["preview_src_len"] = 100
        gd = self._make_gd(self._candidates(), before, after)
        with patch.object(gd, "cdp_click_absolute"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._select_card_design_if_required()  # must not raise

    def test_preview_name_len_change_is_sufficient(self):
        """#cardPreviewName text length changing (from -1 to 20) alone → no raise."""
        before = dict(self._BASE_BEFORE)
        before["preview_name_len"] = -1
        after = dict(self._BASE_BEFORE)
        after["preview_name_len"] = 20
        gd = self._make_gd(self._candidates(), before, after)
        with patch.object(gd, "cdp_click_absolute"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._select_card_design_if_required()  # must not raise

    def test_no_new_signal_raises_session_flagged_error(self):
        """No primary OR legacy signal changed → SessionFlaggedError + screenshot."""
        before = dict(self._BASE_BEFORE)
        after = dict(self._BASE_BEFORE)  # all same — nothing changed
        gd = self._make_gd(self._candidates(), before, after)
        with patch.object(gd, "cdp_click_absolute"), \
             patch.object(gd, "_capture_failure_screenshot") as mock_shot, \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            with self.assertRaisesRegex(SessionFlaggedError, "Card design selection required"):
                gd._select_card_design_if_required()
        mock_shot.assert_called_once_with("card_design_not_verified")

    def test_verification_info_log_includes_counts_not_raw_values(self):
        """INFO log must include count/length fields and no raw PII."""
        after = dict(self._BASE_BEFORE)
        after["clicked_radio_checked"] = True
        after["checked_count"] = 1
        gd = self._make_gd(self._candidates(), dict(self._BASE_BEFORE), after)
        with patch.object(gd, "cdp_click_absolute"), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            gd._select_card_design_if_required()
        combined = "\n".join(l for l in logs.output if "card_design" in l and "INFO" in l)
        self.assertTrue(combined, "Expected INFO card_design log line")
        self.assertIn("checked_count_before", combined)
        self.assertIn("checked_count_after", combined)
        self.assertIn("clicked_radio_checked", combined)
        # No raw IDs in INFO logs
        self.assertNotIn("cws_lbl_415760", combined)


class Round4AtcHittabilityTests(unittest.TestCase):
    """Fix 4 — ATC viewport/hit-test before click."""

    def _make_gd(self, scroll_ret=None, hittest_ret=None):
        selenium = _make_driver()
        side = [scroll_ret, hittest_ret]
        selenium.execute_script.side_effect = side
        return GivexDriver(selenium, strict=False)

    def test_center_outside_viewport_raises_session_flagged_error(self):
        """ATC center outside viewport → SessionFlaggedError + screenshot."""
        gd = self._make_gd(
            scroll_ret=None,
            hittest_ret={"in_viewport": False, "hittest_pass": False, "w": 100, "h": 40},
        )
        with patch.object(gd, "_capture_failure_screenshot") as mock_shot, \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            with self.assertRaisesRegex(SessionFlaggedError, "Add-to-Cart not hittable"):
                gd._verify_atc_hittable()
        mock_shot.assert_called_once_with("add_to_cart_not_hittable")

    def test_hittest_unrelated_element_raises_session_flagged_error(self):
        """elementFromPoint returns unrelated element → raises."""
        gd = self._make_gd(
            scroll_ret=None,
            hittest_ret={"in_viewport": True, "hittest_pass": False, "w": 100, "h": 40},
        )
        with patch.object(gd, "_capture_failure_screenshot") as mock_shot, \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            with self.assertRaisesRegex(SessionFlaggedError, "Add-to-Cart not hittable"):
                gd._verify_atc_hittable()
        mock_shot.assert_called_once_with("add_to_cart_not_hittable")

    def test_hittest_returns_control_passes(self):
        """elementFromPoint returns the control itself → no raise."""
        gd = self._make_gd(
            scroll_ret=None,
            hittest_ret={"in_viewport": True, "hittest_pass": True, "w": 100, "h": 40},
        )
        with patch.object(gd, "_capture_failure_screenshot") as mock_shot, \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._verify_atc_hittable()  # must not raise
        mock_shot.assert_not_called()

    def test_hittest_returns_descendant_passes(self):
        """elementFromPoint returns span child (control.contains(hit)) → pass."""
        # hittest_pass=True covers el.contains(hit) case in JS
        gd = self._make_gd(
            scroll_ret=None,
            hittest_ret={"in_viewport": True, "hittest_pass": True, "w": 313, "h": 37},
        )
        with patch.object(gd, "_capture_failure_screenshot") as mock_shot, \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._verify_atc_hittable()  # must not raise
        mock_shot.assert_not_called()

    def test_hittest_returns_ancestor_passes(self):
        """elementFromPoint returns wrapper div (hit.contains(el)) → pass."""
        gd = self._make_gd(
            scroll_ret=None,
            hittest_ret={"in_viewport": True, "hittest_pass": True, "w": 400, "h": 50},
        )
        with patch.object(gd, "_capture_failure_screenshot") as mock_shot, \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="INFO"):
            gd._verify_atc_hittable()  # must not raise
        mock_shot.assert_not_called()

    def test_element_absent_proceeds_without_raise(self):
        """JS returns None (element not found) → inconclusive, no raise."""
        gd = self._make_gd(scroll_ret=None, hittest_ret=None)
        with patch.object(gd, "_capture_failure_screenshot") as mock_shot, \
             patch("modules.cdp.driver.time.sleep"), \
             self.assertLogs("modules.cdp.driver", level="WARNING"):
            gd._verify_atc_hittable()  # must not raise
        mock_shot.assert_not_called()

    def test_atc_verify_called_before_click_in_add_to_cart(self):
        """_verify_atc_hittable must be called before _click_closest_control_for."""
        gd = GivexDriver(_make_driver(), strict=False)
        order = []
        with patch.object(gd, "_verify_atc_hittable", side_effect=lambda: order.append("verify")), \
             patch.object(gd, "_click_closest_control_for", side_effect=lambda *a: order.append("click")), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_review_checkout_diagnostics", return_value={}), \
             patch.object(gd, "_wait_for_cart_state_after_atc", return_value=(True, {})), \
             patch.object(gd, "_wait_for_review_checkout_enabled", return_value=(True, True)), \
             patch.object(gd, "_wait_for_url_or_capture"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch("modules.cdp.driver.time.sleep"):
            gd.add_to_cart_and_checkout()
        self.assertIn("verify", order)
        self.assertIn("click", order)
        self.assertLess(
            order.index("verify"),
            order.index("click"),
            f"_verify_atc_hittable must precede _click_closest_control_for; order={order}",
        )

    def test_atc_not_hittable_prevents_click(self):
        """When _verify_atc_hittable raises, _click_closest_control_for NOT called."""
        gd = GivexDriver(_make_driver(), strict=False)
        click_called = []
        with patch.object(gd, "_verify_atc_hittable",
                          side_effect=SessionFlaggedError("Add-to-Cart not hittable after scroll")), \
             patch.object(gd, "_click_closest_control_for",
                          side_effect=lambda *a: click_called.append(1)), \
             patch.object(gd, "_wait_for_interactable", return_value=True), \
             patch.object(gd, "_review_checkout_diagnostics", return_value={}), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch("modules.cdp.driver.time.sleep"):
            with self.assertRaisesRegex(SessionFlaggedError, "Add-to-Cart not hittable"):
                gd.add_to_cart_and_checkout()
        self.assertEqual(click_called, [], "_click_closest_control_for must NOT be called")

    def test_verify_atc_js_contains_expected_selectors(self):
        """_verify_atc_hittable JS must reference #cws_btn_gcBuyAdd and elementFromPoint."""
        import inspect
        src = inspect.getsource(GivexDriver._verify_atc_hittable)
        self.assertIn("#cws_btn_gcBuyAdd", src)
        self.assertIn("elementFromPoint", src)
        self.assertIn("scrollIntoView", src)


if __name__ == "__main__":
    unittest.main()
