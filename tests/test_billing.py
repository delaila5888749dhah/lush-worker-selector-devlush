import collections
import os
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

# Tests legitimately access module internals (_lock, _profiles, _reset_state, etc.)
# pylint: disable=protected-access

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

    def test_select_profile_int_zip_matches_string_profile(self):
        """Integer ZIP input must match profiles stored with string ZIPs."""
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

        result = billing.select_profile(10001)

        self.assertEqual(result, profile)

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


class BillingDeterministicFillTests(unittest.TestCase):
    """Regression tests for deterministic persona-scoped fill generation."""

    def test_fill_missing_is_deterministic_for_same_persona(self):
        """Same persona data yields the same generated phone/email on repeated fills."""
        profile = BillingProfile(
            first_name="Ada",
            last_name="Lovelace",
            address="1 Analytical St",
            city="London",
            state="LN",
            zip_code="12345",
            phone=None,
            email=None,
        )

        result1 = billing._fill_missing(profile)
        result2 = billing._fill_missing(profile)

        self.assertEqual(result1.phone, result2.phone)
        self.assertEqual(result1.email, result2.email)

    def test_fill_missing_diff_personas_produce_diff_outputs(self):
        """Different persona seeds should not collapse to the same generated values."""
        profile1 = BillingProfile(
            first_name="Ada",
            last_name="Lovelace",
            address="1 Analytical St",
            city="London",
            state="LN",
            zip_code="12345",
            phone=None,
            email=None,
        )
        profile2 = BillingProfile(
            first_name="Grace",
            last_name="Hopper",
            address="2 Compiler Ave",
            city="Arlington",
            state="VA",
            zip_code="22201",
            phone=None,
            email=None,
        )

        result1 = billing._fill_missing(profile1)
        result2 = billing._fill_missing(profile2)

        self.assertNotEqual((result1.phone, result1.email), (result2.phone, result2.email))

    def test_fill_missing_uses_address_seed_when_names_blank(self):
        """Address fields become the deterministic seed when first/last names are blank."""
        profile = BillingProfile(
            first_name="",
            last_name="",
            address="123 Main St",
            city="Seattle",
            state="WA",
            zip_code="98101",
            phone=None,
            email=None,
        )

        result1 = billing._fill_missing(profile)
        result2 = billing._fill_missing(profile)

        self.assertEqual(result1.phone, result2.phone)
        self.assertEqual(result1.email, result2.email)

    def test_fill_missing_uses_phone_seed_when_names_blank(self):
        """Phone/email fallback seed stays deterministic when names and address are blank."""
        profile = BillingProfile(
            first_name="",
            last_name="",
            address="",
            city="",
            state="",
            zip_code="",
            phone="2125550101",
            email=None,
        )

        result1 = billing._fill_missing(profile)
        result2 = billing._fill_missing(profile)

        self.assertEqual(result1.phone, "2125550101")
        self.assertEqual(result1.email, result2.email)


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
            with open(bad, "wb") as handle:
                handle.write(b"First\xff|Last|1 St|City|NY|10001|2125550001|a@e.com\n")
            good = os.path.join(tmpdir, "good.txt")
            with open(good, "w", encoding="utf-8") as handle:
                handle.write("Alice|Smith|2 St|City|NY|10002|2125550002|b@e.com\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertLogs("modules.billing.main", level="WARNING") as logs:
                    result = billing._read_profiles_from_disk()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].first_name, "Alice")
            self.assertTrue(any("bad.txt" in m for m in logs.output))

    def test_non_utf8_file_skipped_counter_in_summary_log(self):
        """Load summary log shows skipped=1 when one file has a decode error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = os.path.join(tmpdir, "bad.txt")
            with open(bad, "wb") as handle:
                handle.write(b"\xff\xfe bad data\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertLogs("modules.billing.main", level="INFO") as logs:
                    billing._read_profiles_from_disk()
            summary = next((m for m in logs.output if "scanned=" in m), None)
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

    def test_lookalike_path_prefix_rejected(self):
        """Paths like /tmpx/... must not be treated as if they were under /tmp."""
        with patch.dict(os.environ, {"BILLING_POOL_DIR": "/tmpx/not-allowed"}):
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
            with open(path, "w") as handle:
                handle.write("Alice|Smith|1 St|City|NY|10001|2125550001|a@e.com\n")
                handle.write("bad-line-no-pipes\n")
            with patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                with self.assertLogs("modules.billing.main", level="INFO") as logs:
                    billing._read_profiles_from_disk()
        summary = next((m for m in logs.output if "scanned=" in m), None)
        self.assertIsNotNone(summary, "Expected load summary log")
        self.assertIn("accepted=1", summary)
        self.assertIn("rejected=1", summary)

    def test_normalize_zip_rejects_bool(self):
        """Boolean ZIP input must be rejected instead of coercing to 1/0."""
        with self.assertRaises(ValueError):
            billing._normalize_zip(True)

    def test_find_matching_index_requires_lock(self):
        """Direct callers must hold _lock before scanning the shared pool."""
        with self.assertRaises(RuntimeError):
            billing._find_matching_index("10001")

    def test_find_matching_index_returns_correct_index_with_lock(self):
        """Holding _lock preserves the normal index-selection contract."""
        profiles = [
            BillingProfile(
                first_name="A", last_name="L", address="1 St",
                city="City", state="NY", zip_code="99999",
                phone="2125550001", email="a@e.com",
            ),
            BillingProfile(
                first_name="B", last_name="L", address="2 St",
                city="City", state="NY", zip_code="10001",
                phone="2125550002", email="b@e.com",
            ),
        ]
        with billing._lock:
            billing._profiles = collections.deque(profiles)
            self.assertEqual(billing._find_matching_index("10001"), 1)


class ZipAffinityTests(unittest.TestCase):
    """Tests for zip-affinity rotation in billing selection."""

    def setUp(self):
        billing._reset_state()

    def tearDown(self):
        billing._reset_state()

    @staticmethod
    def _set_profiles(profiles):
        with billing._lock:
            billing._profiles = collections.deque(profiles)

    @staticmethod
    def _make_profile(name, zip_code):
        return BillingProfile(
            first_name=name, last_name="L", address="1 St",
            city="City", state="NY", zip_code=zip_code,
            phone="2125550001", email="u@e.com",
        )

    def test_same_zip_rotates_across_matching_profiles(self):
        """Repeated same-zip selections rotate through all matching profiles."""
        profiles = [self._make_profile(f"P{i}", "10001") for i in range(3)]
        self._set_profiles(profiles)

        names = [billing.select_profile("10001").first_name for _ in range(6)]
        self.assertEqual(names, ["P0", "P1", "P2", "P0", "P1", "P2"])

    def test_concurrent_same_zip_gets_different_profiles(self):
        """Concurrent same-zip requests get distinct profiles when pool is sufficient."""
        num_threads = 4
        profiles = [self._make_profile(f"C{i}", "10001") for i in range(num_threads)]
        self._set_profiles(profiles)

        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads

        def worker(idx):
            """Run one synchronized same-zip selection."""
            barrier.wait()
            results[idx] = billing.select_profile("10001").first_name

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads), "Some threads timed out")

        self.assertEqual(
            len(set(results)),
            num_threads,
            f"Expected {num_threads} distinct profiles, got {results}",
        )

    def test_non_zip_round_robin_unaffected(self):
        """Non-zip round-robin still works correctly after zip-affinity fix."""
        profiles = [self._make_profile(f"R{i}", f"0000{i}") for i in range(3)]
        self._set_profiles(profiles)

        names = [billing.select_profile("99999").first_name for _ in range(6)]
        self.assertEqual(names, ["R0", "R1", "R2", "R0", "R1", "R2"])

    def test_zip_match_mixed_with_non_match(self):
        """Zip-matched and non-matched profiles coexist; zip picks only matches."""
        p_match1 = self._make_profile("M1", "10001")
        p_other = self._make_profile("O1", "20002")
        p_match2 = self._make_profile("M2", "10001")
        self._set_profiles([p_match1, p_other, p_match2])

        first = billing.select_profile("10001")
        second = billing.select_profile("10001")
        self.assertEqual(first.first_name, "M1")
        self.assertEqual(second.first_name, "M2")


class GenerateEmailTests(unittest.TestCase):
    """Unit tests for the _generate_email name-based email generation (issue #158)."""

    _VALID_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com"}

    def test_both_names_present_returns_name_based_email(self):
        """When both first and last name are provided, return first.last@domain."""
        result = billing._generate_email("John", "Doe")
        local, domain = result.split("@")
        self.assertEqual(local, "john.doe")
        self.assertIn(domain, self._VALID_DOMAINS)

    def test_both_names_produces_lowercase_email(self):
        """Names are lowercased in the generated email."""
        result = billing._generate_email("ALICE", "SMITH")
        self.assertTrue(result.startswith("alice.smith@"))

    def test_first_name_missing_falls_back_to_hex(self):
        """When first_name is empty, fall back to user{hex}@{domain}."""
        result = billing._generate_email("", "Doe")
        self.assertRegex(result, r"^user[0-9a-f]{8}@")

    def test_last_name_missing_falls_back_to_hex(self):
        """When last_name is empty, fall back to user{hex}@{domain}."""
        result = billing._generate_email("John", "")
        self.assertRegex(result, r"^user[0-9a-f]{8}@")

    def test_both_names_none_falls_back_to_hex(self):
        """When both names are None, fall back to user{hex}@{domain}."""
        result = billing._generate_email(None, None)
        self.assertRegex(result, r"^user[0-9a-f]{8}@")

    def test_both_names_whitespace_falls_back_to_hex(self):
        """Whitespace-only names are treated as missing; fall back to hex form."""
        result = billing._generate_email("   ", "   ")
        self.assertRegex(result, r"^user[0-9a-f]{8}@")

    def test_special_chars_stripped_from_names(self):
        """Non-alphanumeric characters (except . and -) are removed from names."""
        result = billing._generate_email("O'Brien!", "St@nley#")
        local, _ = result.split("@")
        first_part, last_part = local.split(".")
        self.assertFalse(any(c in first_part for c in "'! "))
        self.assertFalse(any(c in last_part for c in "@# "))

    def test_names_sanitized_to_empty_fall_back_to_hex(self):
        """Names that sanitize to empty components fall back to the hex form."""
        result = billing._generate_email("!!!", "@@@")
        self.assertRegex(result, r"^user[0-9a-f]{8}@")

    def test_names_truncated_to_20_chars(self):
        """Each name component is truncated to 20 characters."""
        long_first = "a" * 30
        long_last = "b" * 30
        result = billing._generate_email(long_first, long_last)
        local, _ = result.split("@")
        first_part, last_part = local.split(".")
        self.assertLessEqual(len(first_part), 20)
        self.assertLessEqual(len(last_part), 20)

    def test_domain_chosen_from_allowed_list(self):
        """Generated email always uses a domain from _EMAIL_DOMAINS."""
        for first, last in [("Jane", "Doe"), ("", ""), (None, None), ("X", "Y")]:
            result = billing._generate_email(first, last)
            domain = result.split("@")[1]
            self.assertIn(domain, self._VALID_DOMAINS)

    def test_deterministic_with_seeded_rng(self):
        """Same RNG seed produces the same email every time."""
        result1 = billing._generate_email("Jane", "Doe", rng=random.Random(42))
        result2 = billing._generate_email("Jane", "Doe", rng=random.Random(42))
        self.assertEqual(result1, result2)


if __name__ == "__main__":
    unittest.main()
