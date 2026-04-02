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

    def _set_profiles(self, profiles, cursor=0):
        with billing._lock:
            billing._profiles = profiles
            billing._cursor = cursor
            billing._initialized = True

    def test_select_profile_empty_pool_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertRaises(CycleExhaustedError):
                    billing.select_profile("12345")

    def test_select_profile_zip_match_does_not_advance_cursor(self):
        profile_a = BillingProfile(
            first_name="Ana",
            last_name="Bell",
            address="123 Main St",
            city="New York",
            state="NY",
            zip_code="10001",
            phone="2125550100",
            email="ana.bell@example.com",
        )
        profile_b = BillingProfile(
            first_name="Zed",
            last_name="Cole",
            address="55 Pine St",
            city="Beverly Hills",
            state="CA",
            zip_code="90210",
            phone="3105550100",
            email="zed.cole@example.com",
        )
        self._set_profiles([profile_a, profile_b], cursor=1)

        result = billing.select_profile("10001")

        self.assertEqual(result.zip_code, "10001")
        self.assertEqual(billing._cursor, 1)

    def test_select_profile_no_match_advances_cursor(self):
        profile_a = BillingProfile(
            first_name="Ana",
            last_name="Bell",
            address="123 Main St",
            city="New York",
            state="NY",
            zip_code="10001",
            phone="2125550100",
            email="ana.bell@example.com",
        )
        profile_b = BillingProfile(
            first_name="Zed",
            last_name="Cole",
            address="55 Pine St",
            city="Beverly Hills",
            state="CA",
            zip_code="90210",
            phone="3105550100",
            email="zed.cole@example.com",
        )
        self._set_profiles([profile_a, profile_b], cursor=0)

        result = billing.select_profile("99999")

        self.assertEqual(result.zip_code, "10001")
        self.assertEqual(billing._cursor, 1)

    def test_select_profile_generates_missing_contact_fields(self):
        profile = BillingProfile(
            first_name="John",
            last_name="Doe",
            address="1 Oak St",
            city="Austin",
            state="TX",
            zip_code="73301",
            phone=None,
            email=None,
        )
        self._set_profiles([profile], cursor=0)

        result = billing.select_profile(None)

        self.assertIsNotNone(result.phone)
        self.assertIsNotNone(result.email)
        self.assertEqual(len(result.phone), 10)
        self.assertTrue(result.phone.isdigit())
        self.assertIn(result.phone[0], "23456789")
        self.assertTrue(result.email.startswith("john.doe@"))

        result_again = billing.select_profile(None)
        self.assertEqual(result_again.phone, result.phone)
        self.assertEqual(result_again.email, result.email)


if __name__ == "__main__":
    unittest.main()
