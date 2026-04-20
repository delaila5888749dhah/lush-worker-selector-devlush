import random
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver


class TestHesitationRange(unittest.TestCase):
    def test_hesitation_duration_within_3_to_5s(self):
        driver = MagicMock()
        gd = GivexDriver(driver)
        gd.find_elements = MagicMock(return_value=[])
        gd._cursor = None
        rng = random.Random(123)

        delays = []

        def record_sleep(value):
            delays.append(value)

        with patch.object(gd, "_get_rng", return_value=rng), \
             patch("modules.cdp.driver.time.sleep", side_effect=record_sleep):
            for _ in range(100):
                gd._hesitate_before_submit()

        self.assertEqual(len(delays), 100)
        for delay in delays:
            self.assertGreaterEqual(delay, 3.0)
            self.assertLessEqual(delay, 5.0)


if __name__ == "__main__":
    unittest.main()
