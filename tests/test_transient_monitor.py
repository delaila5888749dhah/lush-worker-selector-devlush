"""Tests for TransientMonitor — active-poll VBV/3DS iframe detection.

Covers Blueprint §6 Fork 3 contract: poll every ~500 ms, thread-safe cancel,
metric emission, and module-isolation (detector is injected, not imported).
"""

import threading
import time
import unittest

from modules.monitor.main import (
    TransientMonitor,
    get_metrics,
    reset,
)


def _vbv_count() -> int:
    """Read the monitor-internal VBV detection counter via the public API."""
    return get_metrics()["vbv_detections"]


class _MonitorResetMixin:
    def setUp(self):
        reset()

    def tearDown(self):
        reset()


class TestVBVDetectionMetric(_MonitorResetMixin, unittest.TestCase):
    def test_counter_starts_at_zero(self):
        self.assertEqual(_vbv_count(), 0)
        self.assertEqual(get_metrics()["vbv_detections"], 0)

    def test_detection_increments_counter_via_monitor(self):
        # The monitor is the only production path that increments this
        # counter; exercise it via the class rather than a private helper.
        for _ in range(2):
            mon = TransientMonitor(detector=lambda: True, interval=0.01)
            mon.start()
            deadline = time.monotonic() + 1.0
            while mon.is_running() and time.monotonic() < deadline:
                time.sleep(0.01)
            mon.cancel(timeout=0.5)
        self.assertEqual(_vbv_count(), 2)
        self.assertEqual(get_metrics()["vbv_detections"], 2)

    def test_reset_clears_counter(self):
        mon = TransientMonitor(detector=lambda: True, interval=0.01)
        mon.start()
        deadline = time.monotonic() + 1.0
        while mon.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        mon.cancel(timeout=0.5)
        self.assertEqual(_vbv_count(), 1)
        reset()
        self.assertEqual(_vbv_count(), 0)


class TestTransientMonitorConstruction(unittest.TestCase):
    def test_non_callable_detector_rejected(self):
        with self.assertRaises(TypeError):
            TransientMonitor(detector="not-callable")

    def test_non_positive_interval_rejected(self):
        with self.assertRaises(ValueError):
            TransientMonitor(detector=lambda: False, interval=0)
        with self.assertRaises(ValueError):
            TransientMonitor(detector=lambda: False, interval=-0.1)


class TestTransientMonitorDetection(_MonitorResetMixin, unittest.TestCase):
    def test_detects_late_appearing_iframe(self):
        calls = []

        def detector():
            calls.append(time.monotonic())
            # Appear on the 3rd poll
            return len(calls) >= 3

        detected = threading.Event()
        mon = TransientMonitor(
            detector=detector,
            interval=0.02,
            on_detect=detected.set,
        )
        mon.start()
        self.assertTrue(detected.wait(2.0), "detection never fired")
        mon.cancel(timeout=1.0)
        self.assertFalse(mon.is_running())
        self.assertEqual(mon.detections, 1)
        self.assertEqual(_vbv_count(), 1)
        self.assertEqual(get_metrics()["vbv_detections"], 1)

    def test_stops_after_first_detection(self):
        """Single-shot: detector must not be called after a positive hit."""
        hits = [0]

        def detector():
            hits[0] += 1
            return True  # first call detects

        mon = TransientMonitor(detector=detector, interval=0.02)
        mon.start()
        # Give the loop enough time to run many more iterations if it were
        # not single-shot.
        time.sleep(0.2)
        self.assertFalse(mon.is_running())
        self.assertEqual(hits[0], 1)
        self.assertEqual(mon.detections, 1)

    def test_detector_exception_is_swallowed(self):
        calls = [0]

        def detector():
            calls[0] += 1
            if calls[0] < 3:
                raise RuntimeError("transient detector failure")
            return True

        mon = TransientMonitor(detector=detector, interval=0.02)
        mon.start()
        deadline = time.monotonic() + 2.0
        while mon.is_running() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(mon.is_running())
        self.assertEqual(mon.detections, 1)


class TestTransientMonitorCancel(_MonitorResetMixin, unittest.TestCase):
    def test_cancel_is_thread_safe_and_prompt(self):
        def detector():
            return False  # never fires

        mon = TransientMonitor(detector=detector, interval=0.5)
        mon.start()
        self.assertTrue(mon.is_running())
        # Cancel from a different thread.
        t = threading.Thread(target=mon.cancel, kwargs={"timeout": 2.0})
        start = time.monotonic()
        t.start()
        t.join(2.0)
        self.assertFalse(t.is_alive())
        # Even though interval=0.5 s, cancel.wait should return immediately
        # when the event is set — shutdown should be much faster than a full
        # poll interval.
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertFalse(mon.is_running())
        self.assertEqual(_vbv_count(), 0)

    def test_cancel_is_idempotent(self):
        mon = TransientMonitor(detector=lambda: False, interval=0.05)
        mon.start()
        mon.cancel(timeout=1.0)
        # Second cancel must not raise and must not block.
        mon.cancel(timeout=0.1)
        self.assertFalse(mon.is_running())

    def test_cancel_before_start_is_safe(self):
        mon = TransientMonitor(detector=lambda: False)
        # Never started; cancel must be a no-op that does not raise.
        mon.cancel(timeout=0.1)
        self.assertFalse(mon.is_running())

    def test_double_start_is_noop(self):
        call_times = []
        lock = threading.Lock()

        def detector():
            with lock:
                call_times.append(time.monotonic())
            return False

        mon = TransientMonitor(detector=detector, interval=0.05)
        mon.start()
        # Give the first loop a moment to record at least one poll.
        time.sleep(0.12)
        with lock:
            before = len(call_times)
        # A second start while already running must be a no-op: it must not
        # spawn a second poller (which would roughly double the poll rate).
        mon.start()
        time.sleep(0.2)
        mon.cancel(timeout=1.0)
        self.assertFalse(mon.is_running())
        with lock:
            after = len(call_times)
        # If start() had spawned a second thread, we'd see ~2× the polls in
        # the post-start window compared to the pre-start window of similar
        # length.  Allow generous slack for scheduling jitter.
        self.assertGreater(before, 0)
        delta = after - before
        self.assertLess(delta, before * 3)


class TestTransientMonitorPollInterval(_MonitorResetMixin, unittest.TestCase):
    def test_polls_at_configured_interval(self):
        """Sanity-check that interval gates the poll cadence."""
        timestamps = []

        def detector():
            timestamps.append(time.monotonic())
            return False

        mon = TransientMonitor(detector=detector, interval=0.1)
        mon.start()
        time.sleep(0.55)
        mon.cancel(timeout=1.0)
        # Expect roughly 5-7 polls in 0.55s with 0.1s interval.  Lower bound
        # guards against "never polled"; upper bound guards against busy loop.
        self.assertGreaterEqual(len(timestamps), 3)
        self.assertLess(len(timestamps), 20)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
