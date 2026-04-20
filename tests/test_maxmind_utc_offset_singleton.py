"""M11 — _lookup_maxmind_utc_offset uses the module-level singleton reader."""
import os
import unittest
from unittest import mock

from modules.cdp import driver as cdp_driver


class _FakeLocation:
    time_zone = "UTC"


class _FakeRecord:
    location = _FakeLocation()


class _FakeReader:
    def __init__(self):
        self.calls = 0

    def city(self, ip):  # pylint: disable=unused-argument
        self.calls += 1
        return _FakeRecord()


class TestMaxMindUtcOffsetSingleton(unittest.TestCase):
    def setUp(self):
        self._prev_reader = cdp_driver._MAXMIND_READER

    def tearDown(self):
        cdp_driver._MAXMIND_READER = self._prev_reader

    def test_utc_offset_uses_singleton_reader(self):
        """When the singleton is populated, the function uses it and does NOT
        open the database file again."""
        fake = _FakeReader()
        cdp_driver._MAXMIND_READER = fake
        # Patch geoip2.database.Reader so that any accidental file-open is
        # detected by the test.
        import importlib
        geoip2_db = importlib.import_module("geoip2.database") if _has_geoip2() else None
        if geoip2_db is not None:
            with mock.patch.object(geoip2_db, "Reader") as mocked_reader_ctor:
                cdp_driver._lookup_maxmind_utc_offset("203.0.113.1")
                mocked_reader_ctor.assert_not_called()
        else:
            cdp_driver._lookup_maxmind_utc_offset("203.0.113.1")
        self.assertEqual(fake.calls, 1)

    def test_os_path_exists_not_consulted_when_singleton_present(self):
        """With the singleton populated the fallback path must not run."""
        cdp_driver._MAXMIND_READER = _FakeReader()
        with mock.patch.object(os.path, "exists") as m_exists:
            cdp_driver._lookup_maxmind_utc_offset("203.0.113.5")
            m_exists.assert_not_called()


def _has_geoip2() -> bool:
    try:
        import importlib
        importlib.import_module("geoip2.database")
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    unittest.main()
