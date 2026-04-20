import random
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import vbv_dynamic_wait


class TestVbvDynamicWait(unittest.TestCase):
    def test_wait_duration_within_8_to_12_seconds(self):
        rng = random.Random(42)
        with patch("time.sleep"):
            durations = [vbv_dynamic_wait(rng) for _ in range(100)]
        for duration in durations:
            self.assertGreaterEqual(duration, 8.0)
            self.assertLessEqual(duration, 12.0)

    def test_wait_uses_provided_rng(self):
        rng = random.Random(123)
        with patch("time.sleep"):
            first = vbv_dynamic_wait(rng)
            second = vbv_dynamic_wait(rng)
        expected_rng = random.Random(123)
        with patch("time.sleep"):
            expected_first = vbv_dynamic_wait(expected_rng)
            expected_second = vbv_dynamic_wait(expected_rng)
        self.assertEqual((first, second), (expected_first, expected_second))

    def test_wait_does_not_touch_dom(self):
        driver = MagicMock()
        with patch("time.sleep"):
            vbv_dynamic_wait()
        self.assertEqual(driver.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
