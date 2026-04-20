"""C1 — Stagger-start delay between worker launches (Blueprint §1)."""
import threading
import time
import unittest
from unittest import mock

from integration import runtime


class TestStaggerStart(unittest.TestCase):
    def setUp(self):
        # Reset the module-level stagger state without calling reset()
        # (which tries to stop the loop and touches other subsystems).
        with runtime._stagger_lock:
            runtime._last_worker_launch_ts = 0.0
        runtime._stop_event.clear()

    def tearDown(self):
        with runtime._stagger_lock:
            runtime._last_worker_launch_ts = 0.0
        runtime._stop_event.clear()

    def test_first_launch_no_stagger(self):
        """The very first launch must not block (Blueprint §1)."""
        start = time.monotonic()
        slept = runtime._stagger_sleep_before_launch()
        elapsed = time.monotonic() - start
        self.assertEqual(slept, 0.0)
        self.assertLess(elapsed, 0.5)

    def test_inter_launch_delay_within_12_25(self):
        """After the first launch, subsequent launches sleep 12-25s.

        Uses a fake ``rng`` to keep the test fast while still asserting that
        the stagger path picks values only within ``_STAGGER_RANGE``.
        """
        # Seed the module state so the next call is a "subsequent" launch.
        with runtime._stagger_lock:
            runtime._last_worker_launch_ts = time.monotonic()

        captured = []

        class _FakeRng:
            def uniform(self, lo, hi):
                captured.append((lo, hi))
                return lo  # deterministic lower bound

        with mock.patch.object(runtime._stop_event, "wait",
                               return_value=False) as m_wait:
            runtime._stagger_sleep_before_launch(rng=_FakeRng())
            self.assertEqual(captured, [runtime._STAGGER_RANGE])
            # Sleep is > 0 because elapsed was essentially 0s.
            m_wait.assert_called_once()
            (_, kwargs), = [(m_wait.call_args.args, m_wait.call_args.kwargs)]
            self.assertGreater(kwargs["timeout"], 11.9)
            self.assertLessEqual(kwargs["timeout"], 25.0)

    def test_stagger_interruptible_by_stop_event(self):
        """Sleep is interrupted by ``_stop_event`` for fast shutdown."""
        with runtime._stagger_lock:
            runtime._last_worker_launch_ts = time.monotonic()
        runtime._stop_event.set()
        start = time.monotonic()
        # _stop_event is already set → wait returns immediately.
        runtime._stagger_sleep_before_launch()
        self.assertLess(time.monotonic() - start, 0.5)

    def test_stagger_thread_safe(self):
        """Concurrent calls do not corrupt _last_worker_launch_ts."""
        runtime._stop_event.set()  # make each sleep return immediately
        with runtime._stagger_lock:
            runtime._last_worker_launch_ts = time.monotonic()
        errors = []

        def _worker():
            try:
                runtime._stagger_sleep_before_launch()
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertGreater(runtime._last_worker_launch_ts, 0.0)


if __name__ == "__main__":
    unittest.main()
