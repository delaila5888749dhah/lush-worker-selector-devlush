import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, SEL_CARD_NUMBER


class Test4x4PatternWired(unittest.TestCase):
    def test_card_number_fill_invokes_4x4_delays(self):
        driver = MagicMock()
        element = MagicMock()
        driver.find_elements.return_value = [element]
        gd = GivexDriver(driver)
        gd._bio = MagicMock()
        gd._bio.generate_4x4_pattern.return_value = [0.1] * 19
        gd._bio.generate_burst_pattern.return_value = [0.2]

        with patch("modules.cdp.driver._type_value") as mock_type:
            gd._realistic_type_field(
                SEL_CARD_NUMBER,
                "4111111111111111",
                use_burst=True,
                field_kind="card_number",
            )

        gd._bio.generate_4x4_pattern.assert_called_once()
        gd._bio.generate_burst_pattern.assert_not_called()
        self.assertEqual(mock_type.call_args.kwargs["delays"], [0.1] * 19)


if __name__ == "__main__":
    unittest.main()
