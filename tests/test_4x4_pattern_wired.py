import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, SEL_CARD_NUMBER


class Test4x4PatternWired(unittest.TestCase):
    def test_card_number_fill_invokes_4x4_delays(self):
        driver = MagicMock()
        element = MagicMock()
        driver.find_elements.return_value = [element]
        gd = GivexDriver(driver)
        bio = MagicMock()
        bio.generate_4x4_pattern.return_value = [0.1] * 16
        bio.generate_burst_pattern.return_value = [0.2]
        setattr(gd, "_bio", bio)

        with patch.object(gd, "_human_scroll_to"), \
             patch.object(gd, "_wait_scroll_stable"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_verify_field_value_length"), \
             patch.object(gd, "_engine_aware_sleep"), \
             patch("modules.cdp.driver._type_value") as mock_type:
            gd._realistic_type_field(
                SEL_CARD_NUMBER,
                "4111111111111111",
                use_burst=True,
                field_kind="card_number",
            )

        bio.generate_4x4_pattern.assert_called_once()
        bio.generate_burst_pattern.assert_not_called()
        self.assertEqual(mock_type.call_args.kwargs["delays"], [0.1] * 16)


if __name__ == "__main__":
    unittest.main()
