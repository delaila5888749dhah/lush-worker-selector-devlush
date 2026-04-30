import threading
import time
import unittest
from unittest.mock import patch

from integration import runtime

MIN_WAIT_FACTOR = 0.75
MAX_WAIT_FACTOR = 7.5


class RuntimeWaitTests(unittest.TestCase):
    def test_wait_returns_true_when_loop_thread_is_none(self):
        with patch.object(runtime, "_loop_thread", None):
            self.assertTrue(runtime.wait(timeout=0.1))

    def test_wait_blocks_until_loop_thread_exits(self):
        done = threading.Event()
        release_delay = 0.2
        expected_min_wait = release_delay * MIN_WAIT_FACTOR
        expected_max_wait = release_delay * MAX_WAIT_FACTOR

        def _fake_loop():
            done.wait(timeout=2.0)

        thread = threading.Thread(target=_fake_loop, daemon=True)
        thread.start()
        try:
            with patch.object(runtime, "_loop_thread", thread):
                threading.Timer(release_delay, done.set).start()
                start = time.monotonic()
                ok = runtime.wait(timeout=2.0)
                elapsed = time.monotonic() - start
        finally:
            done.set()
            thread.join(timeout=2.0)

        self.assertTrue(ok)
        self.assertGreaterEqual(elapsed, expected_min_wait)
        self.assertLess(elapsed, expected_max_wait)

    def test_wait_returns_false_on_timeout(self):
        blocker = threading.Event()

        def _hung_loop():
            blocker.wait(timeout=10.0)

        thread = threading.Thread(target=_hung_loop, daemon=True)
        thread.start()
        try:
            with patch.object(runtime, "_loop_thread", thread):
                self.assertFalse(runtime.wait(timeout=0.1))
        finally:
            blocker.set()
            thread.join(timeout=2.0)
