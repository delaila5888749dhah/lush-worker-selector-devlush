import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, SEL_BILLING_ZIP, SEL_GUEST_EMAIL


def _make_driver():
    driver = MagicMock()
    driver.find_elements.return_value = [MagicMock()]
    return driver


class TestTypingTelemetry(unittest.TestCase):
    def test_duration_ms_emitted(self):
        selenium = _make_driver()
        givex = GivexDriver(selenium)
        typed = {
            "typed_chars": 5,
            "typos_injected": 0,
            "corrections_made": 0,
            "mode": "cdp_key",
        }
        with patch.object(givex, "_human_scroll_to"), \
             patch.object(givex, "_wait_scroll_stable"), \
             patch.object(givex, "bounding_box_click"), \
             patch.object(givex, "_engine_aware_sleep"), \
             patch.object(givex, "_field_value_length", return_value=5), \
             patch("modules.cdp.driver._type_value", return_value=typed), \
             patch("time.monotonic_ns", side_effect=[1_000_000_000, 1_187_600_000]), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            res = givex._realistic_type_field(SEL_BILLING_ZIP, "12345")

        self.assertEqual(res, typed)
        complete = [
            line for line in logs.output
            if "_realistic_type_field_complete" in line
        ]
        self.assertEqual(len(complete), 1)
        self.assertIn("field=SEL_BILLING_ZIP", complete[0])
        self.assertIn("expected_len=5", complete[0])
        self.assertIn("actual_len=5", complete[0])
        self.assertIn("duration_ms=187.6", complete[0])
        self.assertIn("typed_chars=5", complete[0])
        self.assertIn("mode=cdp_key", complete[0])
        self.assertIn("engine_delay_permitted=True", complete[0])

    def test_no_raw_value_logged(self):
        selenium = _make_driver()
        givex = GivexDriver(selenium)
        raw_value = "person@example.com"
        typed = {
            "typed_chars": len(raw_value),
            "typos_injected": 0,
            "corrections_made": 0,
            "mode": "cdp_key",
        }
        with patch.object(givex, "_human_scroll_to"), \
             patch.object(givex, "_wait_scroll_stable"), \
             patch.object(givex, "bounding_box_click"), \
             patch.object(givex, "_engine_aware_sleep"), \
             patch.object(givex, "_field_value_length", return_value=len(raw_value)), \
             patch("modules.cdp.driver._type_value", return_value=typed), \
             patch("time.monotonic_ns", side_effect=[2_000_000_000, 2_025_000_000]), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            givex._realistic_type_field(SEL_GUEST_EMAIL, raw_value)

        output = "\n".join(logs.output)
        self.assertNotIn(raw_value, output)
        self.assertIn("_realistic_type_field_complete", output)
        self.assertIn("field=SEL_GUEST_EMAIL", output)
        self.assertIn(f"expected_len={len(raw_value)}", output)

    def test_duration_ms_emitted_for_fallback(self):
        selenium = _make_driver()
        givex = GivexDriver(selenium)
        with patch.object(givex, "_human_scroll_to"), \
             patch.object(givex, "_wait_scroll_stable"), \
             patch.object(givex, "bounding_box_click"), \
             patch.object(givex, "_engine_aware_sleep"), \
             patch.object(givex, "_field_value_length", return_value=5), \
             patch.object(givex, "_send_keys_fallback"), \
             patch("modules.cdp.driver._type_value", None), \
             patch("time.monotonic_ns", side_effect=[3_000_000_000, 3_010_000_000]), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            res = givex._realistic_type_field(SEL_BILLING_ZIP, "12345")

        self.assertEqual(res, {})
        complete = [
            line for line in logs.output
            if "_realistic_type_field_complete" in line
        ]
        self.assertEqual(len(complete), 1)
        self.assertIn("duration_ms=10.0", complete[0])
        self.assertIn("typed_chars=-1", complete[0])
        self.assertIn("mode=unknown", complete[0])

    def test_completion_log_handles_mocked_return_values(self):
        selenium = _make_driver()
        givex = GivexDriver(selenium)
        with patch.object(givex, "_human_scroll_to"), \
             patch.object(givex, "_wait_scroll_stable"), \
             patch.object(givex, "bounding_box_click"), \
             patch.object(givex, "_engine_aware_sleep"), \
             patch.object(givex, "_verify_field_value_length", return_value=MagicMock()), \
             patch("modules.cdp.driver._type_value", return_value=MagicMock()), \
             patch("time.monotonic_ns", side_effect=[4_000_000_000, 4_001_000_000]), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            givex._realistic_type_field(SEL_BILLING_ZIP, "12345")

        complete = [
            line for line in logs.output
            if "_realistic_type_field_complete" in line
        ]
        self.assertEqual(len(complete), 1)
        self.assertIn("actual_len=-1", complete[0])
        self.assertIn("typed_chars=-1", complete[0])
        self.assertIn("mode=unknown", complete[0])

    def test_delay_permitted_recomputed_after_focus_sleeps(self):
        selenium = _make_driver()
        givex = GivexDriver(selenium)
        givex._persona = MagicMock()
        givex._persona.get_typo_probability.return_value = 0.25
        givex._engine = MagicMock()
        givex._engine.is_delay_permitted.return_value = True
        typed = {
            "typed_chars": 5,
            "typos_injected": 0,
            "corrections_made": 0,
            "mode": "cdp_key",
        }
        kwargs_seen = []

        def disable_delay_permission(*_args, **_kwargs):
            givex._engine.is_delay_permitted.return_value = False

        def capture_type_value(_driver, _element, _value, _rng, **kwargs):
            kwargs_seen.append(kwargs)
            return typed

        self.assertTrue(givex._engine.is_delay_permitted())
        with patch.object(givex, "_human_scroll_to"), \
             patch.object(givex, "_wait_scroll_stable"), \
             patch.object(givex, "bounding_box_click"), \
             patch.object(givex, "_engine_aware_sleep", side_effect=disable_delay_permission), \
             patch.object(givex, "_field_value_length", return_value=5), \
             patch("modules.cdp.driver._type_value", side_effect=capture_type_value), \
             patch("time.monotonic_ns", side_effect=[5_000_000_000, 5_001_000_000]), \
             self.assertLogs("modules.cdp.driver", level="INFO") as logs:
            givex._realistic_type_field(SEL_BILLING_ZIP, "12345")

        self.assertEqual(kwargs_seen[0]["typo_rate"], 0.0)
        self.assertIn("engine_delay_permitted=False", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
