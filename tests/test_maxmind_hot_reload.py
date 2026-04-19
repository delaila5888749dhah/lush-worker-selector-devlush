"""Tests for MaxMind hot-reload (D1) — driver.py module-level helpers."""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv


class _FakeReader:
    """Minimal stand-in for geoip2.database.Reader."""

    def __init__(self, path: str):
        self.path = path
        self.closed = False
        self.id = id(self)

    def city(self, _ip):  # pragma: no cover - not exercised here
        raise RuntimeError("not used")

    def close(self):
        self.closed = True


class TestMaxMindHotReload(unittest.TestCase):
    def setUp(self):
        # Shrink swap grace to 0 so old-reader close happens immediately.
        self._orig_grace = drv._MAXMIND_SWAP_GRACE_SECONDS
        drv._MAXMIND_SWAP_GRACE_SECONDS = 0
        self._orig_reader = drv._MAXMIND_READER
        self._orig_mtime = drv._MAXMIND_FILE_MTIME
        drv._MAXMIND_READER = None
        drv._MAXMIND_FILE_MTIME = None
        drv._MAXMIND_RELOAD_STOP.clear()
        # Ensure any residual reload thread from a prior test is cleared.
        drv._MAXMIND_RELOAD_THREAD = None

    def tearDown(self):
        drv._MAXMIND_RELOAD_STOP.set()
        t = drv._MAXMIND_RELOAD_THREAD
        if t is not None:
            t.join(timeout=2)
        drv._MAXMIND_RELOAD_THREAD = None
        drv._MAXMIND_SWAP_GRACE_SECONDS = self._orig_grace
        drv._MAXMIND_READER = self._orig_reader
        drv._MAXMIND_FILE_MTIME = self._orig_mtime

    def test_reload_thread_starts_and_stops(self):
        # Use a very long interval so the thread parks on wait().
        with patch.object(drv, "_MAXMIND_RELOAD_INTERVAL_HOURS", 10_000):
            drv.start_maxmind_auto_reload()
            t = drv._MAXMIND_RELOAD_THREAD
            self.assertIsNotNone(t)
            self.assertTrue(t.is_alive())
            self.assertTrue(t.daemon)
            self.assertEqual(t.name, "maxmind-auto-reload")
            # Idempotent start — second call returns the same (alive) thread.
            drv.start_maxmind_auto_reload()
            self.assertIs(drv._MAXMIND_RELOAD_THREAD, t)

            drv.stop_maxmind_auto_reload()
            self.assertFalse(t.is_alive())
        self.assertIsNone(drv._MAXMIND_RELOAD_THREAD)

    def test_mtime_change_triggers_swap(self):
        fake_module = MagicMock()
        fake_module.Reader.side_effect = _FakeReader
        drv._MAXMIND_READER = _FakeReader("/tmp/old.mmdb")
        drv._MAXMIND_FILE_MTIME = 100.0

        with patch.object(drv, "_get_mmdb_path", return_value="/tmp/x.mmdb"), \
             patch.object(drv.importlib, "import_module", return_value=fake_module), \
             patch.object(drv.os.path, "getmtime", return_value=200.0):
            # Run one iteration of the reload loop manually by exercising
            # the body of the loop.  We short-circuit _MAXMIND_RELOAD_STOP.
            path = drv._get_mmdb_path()
            mtime = drv.os.path.getmtime(path)
            if drv._MAXMIND_FILE_MTIME is not None and mtime > drv._MAXMIND_FILE_MTIME:
                drv._atomic_swap_reader()

        self.assertIsNotNone(drv._MAXMIND_READER)
        self.assertEqual(drv._MAXMIND_READER.path, "/tmp/x.mmdb")
        self.assertEqual(drv._MAXMIND_FILE_MTIME, 200.0)

    def test_swap_is_atomic(self):
        """After _atomic_swap_reader, the module global points to the new reader."""
        fake_module = MagicMock()
        fake_module.Reader.side_effect = _FakeReader
        old = _FakeReader("/tmp/old.mmdb")
        drv._MAXMIND_READER = old

        with patch.object(drv, "_get_mmdb_path", return_value="/tmp/new.mmdb"), \
             patch.object(drv.importlib, "import_module", return_value=fake_module), \
             patch.object(drv.os.path, "getmtime", return_value=500.0):
            drv._atomic_swap_reader()

        self.assertIsNot(drv._MAXMIND_READER, old)
        self.assertEqual(drv._MAXMIND_READER.path, "/tmp/new.mmdb")
        # Old reader is closed on a background grace thread; wait briefly.
        deadline = time.monotonic() + 2
        while not old.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(old.closed)

    def test_concurrent_reads_during_swap_no_crash(self):
        """10 threads call maxmind_lookup_zip while swap happens — no crash."""
        fake_module = MagicMock()
        fake_module.Reader.side_effect = _FakeReader

        class _LookupReader(_FakeReader):
            def city(self, ip):
                # Minimal record with postal.code
                rec = MagicMock()
                rec.postal.code = "90210"
                return rec

        drv._MAXMIND_READER = _LookupReader("/tmp/a.mmdb")
        # Turn Reader factory into a _LookupReader factory for the swap.
        fake_module.Reader.side_effect = lambda p: _LookupReader(p)

        errors = []

        def reader_thread():
            for _ in range(50):
                try:
                    result = drv.maxmind_lookup_zip("8.8.8.8")
                    self.assertEqual(result, "90210")
                except Exception as exc:  # pragma: no cover - failing path
                    errors.append(exc)

        threads = [threading.Thread(target=reader_thread) for _ in range(10)]
        for t in threads:
            t.start()

        with patch.object(drv, "_get_mmdb_path", return_value="/tmp/b.mmdb"), \
             patch.object(drv.importlib, "import_module", return_value=fake_module), \
             patch.object(drv.os.path, "getmtime", return_value=999.0):
            for _ in range(5):
                drv._atomic_swap_reader()
                time.sleep(0.005)

        for t in threads:
            t.join(timeout=5)
        self.assertFalse(errors, f"Unexpected errors in readers: {errors}")


if __name__ == "__main__":
    unittest.main()
