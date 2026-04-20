"""PR-4 T-G2 — Token-bucket rate limiter tests."""
import threading
import time
import unittest

from modules.notification.telegram_notifier import TokenBucket


class TokenBucketCapTests(unittest.TestCase):
    def test_token_bucket_caps_at_5_per_sec(self):
        """Flooding the bucket converges to ~rate_per_sec throughput."""
        rate = 5.0
        bucket = TokenBucket(rate_per_sec=rate, capacity=int(rate * 2))
        n = 50
        start = time.monotonic()
        granted = 0
        for _ in range(n):
            if bucket.acquire(timeout=15.0):
                granted += 1
        elapsed = time.monotonic() - start
        self.assertEqual(granted, n)
        # After the capacity burst is exhausted the rest drain at ~rate/s.
        # Expected lower bound: (n - capacity) / rate seconds.
        expected_min = (n - bucket.capacity) / rate
        self.assertGreaterEqual(
            elapsed, expected_min * 0.85,
            f"Too fast: elapsed={elapsed:.2f}s expected>={expected_min:.2f}s",
        )

    def test_token_bucket_thread_safe(self):
        """Concurrent acquires never dispense more than (capacity + rate*T) tokens."""
        rate = 10.0
        bucket = TokenBucket(rate_per_sec=rate, capacity=int(rate * 2))
        granted = []
        lock = threading.Lock()
        stop = threading.Event()

        def worker():
            while not stop.is_set():
                if bucket.acquire(timeout=0.5):
                    with lock:
                        granted.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        start = time.monotonic()
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        elapsed = time.monotonic() - start
        # Upper bound: capacity + rate * elapsed * 1.5 (slack for scheduler jitter).
        upper = bucket.capacity + rate * elapsed * 1.5
        self.assertLessEqual(
            len(granted), int(upper),
            f"Thread-safe breach: granted={len(granted)} upper={upper:.1f}",
        )


if __name__ == "__main__":
    unittest.main()
