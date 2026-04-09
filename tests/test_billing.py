import collections
import os
import tempfile
import unittest
from unittest.mock import patch

from modules.billing import main as billing
from modules.common.exceptions import CycleExhaustedError
from modules.common.types import BillingProfile


class BillingTests(unittest.TestCase):
    def setUp(self):
        billing._reset_state()

    def tearDown(self):
        billing._reset_state()

    def _set_profiles(self, profiles):
        with billing._lock:
            billing._profiles = collections.deque(profiles)

    def test_select_profile_valid_input_returns_profile(self):
        profile = BillingProfile(
            first_name="Ana",
            last_name="Bell",
            address="123 Main St",
            city="New York",
            state="NY",
            zip_code="10001",
            phone="2125550100",
            email="ana.bell@example.com",
        )
        self._set_profiles([profile])

        result = billing.select_profile("10001")

        self.assertIsInstance(result, BillingProfile)
        self.assertEqual(result, profile)

    def test_select_profile_empty_pool_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertRaises(CycleExhaustedError):
                    billing.select_profile("12345")

    def test_select_profile_invalid_input_raises(self):
        with self.assertRaises(ValueError):
            billing.select_profile({"zip": "10001"})

    def test_rotation_order_without_zip_match(self):
        """Non-matching zip rotates profiles: first goes to back, second comes front."""
        p1 = BillingProfile(
            first_name="A", last_name="One", address="1 St",
            city="X", state="NY", zip_code="00001",
            phone="2125550001", email="a@example.com",
        )
        p2 = BillingProfile(
            first_name="B", last_name="Two", address="2 St",
            city="Y", state="CA", zip_code="00002",
            phone="2125550002", email="b@example.com",
        )
        p3 = BillingProfile(
            first_name="C", last_name="Three", address="3 St",
            city="Z", state="TX", zip_code="00003",
            phone="2125550003", email="c@example.com",
        )
        self._set_profiles([p1, p2, p3])

        # No zip match → should return p1 (front) and rotate it to back
        result1 = billing.select_profile("99999")
        self.assertEqual(result1.first_name, "A")

        # Next call → p2 is now at front
        result2 = billing.select_profile("99999")
        self.assertEqual(result2.first_name, "B")

        # Next call → p3 is now at front
        result3 = billing.select_profile("99999")
        self.assertEqual(result3.first_name, "C")

        # Full cycle — p1 is back at front
        result4 = billing.select_profile("99999")
        self.assertEqual(result4.first_name, "A")

    def test_rotation_fills_missing_phone_and_email(self):
        """Profile with missing phone/email is enriched during rotation."""
        p = BillingProfile(
            first_name="D", last_name="Four", address="4 St",
            city="W", state="FL", zip_code="00004",
            phone=None, email=None,
        )
        self._set_profiles([p])

        result = billing.select_profile("99999")
        self.assertIsNotNone(result.phone)
        self.assertIsNotNone(result.email)
        self.assertEqual(result.first_name, "D")
        # Enriched profile should be appended back into the pool
        with billing._lock:
            self.assertEqual(len(billing._profiles), 1)
            back = billing._profiles[0]
        self.assertIsNotNone(back.phone)
        self.assertIsNotNone(back.email)


if __name__ == "__main__":
    unittest.main()
