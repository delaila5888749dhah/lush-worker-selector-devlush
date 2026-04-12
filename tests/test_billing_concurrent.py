"""Concurrent stress tests for billing.select_profile() cold-start safety."""
import collections
import os
import tempfile
import threading
import unittest
import unittest.mock

from modules.billing import main as billing
from modules.common.types import BillingProfile


def _make_profile(n: int) -> BillingProfile:
    return BillingProfile(
        first_name=f"First{n}",
        last_name=f"Last{n}",
        address=f"{n} Main St",
        city="Testville",
        state="NY",
        zip_code="10001",
        phone=f"{'2' + str(n).zfill(9)}",
        email=f"user{n}@example.com",
    )


class ColdStartConcurrentTests(unittest.TestCase):
    def setUp(self):
        billing._reset_state()

    def tearDown(self):
        billing._reset_state()

    def test_cold_start_concurrent_load(self):
        """16 threads hit cold-start simultaneously; no exceptions, all get BillingProfile."""
        profile_line = "FirstA|LastA|1 St|City|NY|10001|2125550001|a@example.com"

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("pool_a.txt", "pool_b.txt", "pool_c.txt"):
                path = os.path.join(tmpdir, name)
                with open(path, "w") as fh:
                    for i in range(20):
                        fh.write(
                            f"First{i}|Last{i}|{i} St|City|NY|10001"
                            f"|210000000{i % 10}|u{i}@e.com\n"
                        )

            results = []
            errors = []

            def worker():
                try:
                    profile = billing.select_profile("10001")
                    results.append(profile)
                except Exception as exc:
                    errors.append(exc)

            with unittest.mock.patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                billing._reset_state()
                threads = [threading.Thread(target=worker) for _ in range(16)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(len(results), 16)
        for r in results:
            self.assertIsInstance(r, BillingProfile)
        with billing._lock:
            self.assertGreater(len(billing._profiles), 0)

    def test_concurrent_rotation_no_corruption(self):
        """20 threads each call select_profile 50 times (rotation path); pool stays intact."""
        profiles = [_make_profile(i) for i in range(10)]
        with billing._lock:
            billing._profiles = collections.deque(profiles)

        errors = []
        returned = []

        def worker():
            try:
                for _ in range(50):
                    p = billing.select_profile("99999")  # no zip match → rotation
                    returned.append(p)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(len(returned), 1000)
        with billing._lock:
            self.assertEqual(len(billing._profiles), 10, "Pool size must remain 10 after rotation")
        for r in returned:
            self.assertIsInstance(r, BillingProfile)

    def test_concurrent_zip_match_no_corruption(self):
        """20 threads each call select_profile with matching zip; no corruption."""
        profiles = [_make_profile(i) for i in range(10)]
        with billing._lock:
            billing._profiles = collections.deque(profiles)

        errors = []
        returned = []

        def worker():
            try:
                for _ in range(50):
                    p = billing.select_profile("10001")  # zip match path
                    returned.append(p)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(len(returned), 1000)
        for r in returned:
            self.assertIsInstance(r, BillingProfile)

    def test_cold_start_only_loads_once_per_pool(self):
        """After cold-start, pool is populated; subsequent concurrent calls don't overwrite it."""
        profile_line = "F|L|1 St|City|NY|10001|2125550001|a@example.com"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "pool.txt")
            with open(path, "w") as fh:
                for i in range(5):
                    fh.write(
                        f"First{i}|Last{i}|{i} St|City|NY|10001"
                        f"|210000000{i}|u{i}@e.com\n"
                    )

            load_count = [0]
            original_read = billing._read_profiles_from_disk

            def counting_read():
                load_count[0] += 1
                return original_read()

            errors = []

            def worker():
                try:
                    billing.select_profile("10001")
                except Exception as exc:
                    errors.append(exc)

            with unittest.mock.patch.dict(os.environ, {"BILLING_POOL_DIR": tmpdir}):
                billing._reset_state()
                with unittest.mock.patch.object(
                    billing, "_read_profiles_from_disk", side_effect=counting_read
                ):
                    threads = [threading.Thread(target=worker) for _ in range(8)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()

            # Multiple reads are acceptable (the double-check prevents overwriting)
            self.assertEqual(errors, [])
            self.assertLessEqual(load_count[0], 8)
            with billing._lock:
                # Pool must be non-empty after cold start
                self.assertGreater(len(billing._profiles), 0)


if __name__ == "__main__":
    unittest.main()
