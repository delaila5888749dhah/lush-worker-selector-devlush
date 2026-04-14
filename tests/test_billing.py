import collections
import logging
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

    def test_max_billing_profiles_cap(self):
        """_read_profiles_from_disk respects _MAX_BILLING_PROFILES limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write 2 files, each with 5 valid lines → 10 total
            for name in ("a.txt", "b.txt"):
                path = os.path.join(tmpdir, name)
                with open(path, "w") as f:
                    for i in range(5):
                        f.write(f"F{i}|L{i}|{i} St|City|ST|{i:05d}|555000000{i}|u{i}@e.com\n")

            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                # Cap at 3 → only 3 profiles should be loaded
                original = billing._MAX_BILLING_PROFILES
                try:
                    billing._MAX_BILLING_PROFILES = 3
                    result = billing._read_profiles_from_disk()
                    self.assertEqual(len(result), 3)
                finally:
                    billing._MAX_BILLING_PROFILES = original


class BillingHardeningTests(unittest.TestCase):
    """Tests for billing loader hardening: encoding faults, env validation, min threshold."""

    def setUp(self):
        billing._reset_state()

    def tearDown(self):
        billing._reset_state()

    def test_non_utf8_file_is_skipped_with_warning(self):
        """Non-UTF8 .txt files are skipped; valid files still load; warning is logged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = os.path.join(tmpdir, "bad.txt")
            with open(bad, "wb") as fh:
                fh.write(b"First\xff|Last|1 St|City|NY|10001|2125550001|a@e.com\n")
            good = os.path.join(tmpdir, "good.txt")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write("Alice|Smith|2 St|City|NY|10002|2125550002|b@e.com\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertLogs("modules.billing.main", level="WARNING") as cm:
                    result = billing._read_profiles_from_disk()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].first_name, "Alice")
            self.assertTrue(any("bad.txt" in m for m in cm.output))

    def test_non_utf8_file_skipped_counter_in_summary_log(self):
        """Load summary log shows skipped=1 when one file has a decode error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = os.path.join(tmpdir, "bad.txt")
            with open(bad, "wb") as fh:
                fh.write(b"\xff\xfe bad data\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertLogs("modules.billing.main", level="INFO") as cm:
                    billing._read_profiles_from_disk()
            summary = next((m for m in cm.output if "scanned=" in m), None)
            self.assertIsNotNone(summary, "Expected a load summary log line")
            self.assertIn("skipped=1", summary)

    def test_empty_billing_pool_dir_raises(self):
        """Explicitly empty BILLING_POOL_DIR raises ValueError."""
        with patch.dict(os.environ, {"BILLING_POOL_DIR": ""}):
            with self.assertRaises(ValueError):
                billing._pool_dir()

    def test_whitespace_billing_pool_dir_raises(self):
        """Whitespace-only BILLING_POOL_DIR raises ValueError."""
        with patch.dict(os.environ, {"BILLING_POOL_DIR": "   "}):
            with self.assertRaises(ValueError):
                billing._pool_dir()

    def test_unset_billing_pool_dir_uses_default(self):
        """Unset BILLING_POOL_DIR falls back to default billing_pool directory."""
        env = {k: v for k, v in os.environ.items() if k != "BILLING_POOL_DIR"}
        with patch.dict(os.environ, env, clear=True):
            result = billing._pool_dir()
        self.assertTrue(str(result).endswith("billing_pool"))

    def test_min_pool_threshold_raises_when_below(self):
        """select_profile raises CycleExhaustedError when pool is below MIN_BILLING_PROFILES."""
        p = BillingProfile(
            first_name="A", last_name="B", address="1 St",
            city="X", state="NY", zip_code="10001",
            phone="2125550001", email="a@e.com",
        )
        with billing._lock:
            billing._profiles = collections.deque([p])
        original = billing._MIN_BILLING_PROFILES
        try:
            billing._MIN_BILLING_PROFILES = 5
            with self.assertRaises(CycleExhaustedError) as ctx:
                billing.select_profile()
            self.assertIn("below minimum threshold", str(ctx.exception))
        finally:
            billing._MIN_BILLING_PROFILES = original

    def test_min_pool_threshold_ok_when_met(self):
        """select_profile succeeds when pool meets MIN_BILLING_PROFILES threshold."""
        profiles = [
            BillingProfile(
                first_name=f"F{i}", last_name="L", address="1 St",
                city="X", state="NY", zip_code="10001",
                phone="2125550001", email="a@e.com",
            )
            for i in range(3)
        ]
        with billing._lock:
            billing._profiles = collections.deque(profiles)
        original = billing._MIN_BILLING_PROFILES
        try:
            billing._MIN_BILLING_PROFILES = 3
            result = billing.select_profile()
            self.assertIsInstance(result, BillingProfile)
        finally:
            billing._MIN_BILLING_PROFILES = original

    def test_load_summary_log_emitted(self):
        """_read_profiles_from_disk always emits a load-summary INFO log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "pool.txt")
            with open(path, "w") as fh:
                fh.write("Alice|Smith|1 St|City|NY|10001|2125550001|a@e.com\n")
                fh.write("bad-line-no-pipes\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertLogs("modules.billing.main", level="INFO") as cm:
                    billing._read_profiles_from_disk()
        summary = next((m for m in cm.output if "scanned=" in m), None)
        self.assertIsNotNone(summary, "Expected load summary log")
        self.assertIn("accepted=1", summary)
        self.assertIn("rejected=1", summary)


if __name__ == "__main__":
    unittest.main()
