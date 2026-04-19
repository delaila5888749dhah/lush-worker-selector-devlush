"""Tests for standalone preflight_geo_check (C3) — country assertion + recovery."""
import json
import unittest
from unittest.mock import MagicMock

from modules.cdp.driver import GeoCheckFailedError, preflight_geo_check
from modules.common.exceptions import SessionFlaggedError


def _make_driver(body_text):
    drv = MagicMock()
    drv.get = MagicMock()
    elem = MagicMock()
    elem.text = body_text
    drv.find_element.return_value = elem
    return drv


class TestPreflightGeoCheck(unittest.TestCase):
    def test_geo_check_us_passes(self):
        drv = _make_driver(json.dumps({"country": "US", "ip": "1.2.3.4"}))
        result = preflight_geo_check(drv)
        self.assertEqual(result["country"], "US")
        self.assertEqual(result["ip"], "1.2.3.4")
        drv.get.assert_called_once()

    def test_geo_check_non_us_raises_GeoCheckFailedError(self):
        drv = _make_driver(json.dumps({"country": "CA"}))
        with self.assertRaises(GeoCheckFailedError) as ctx:
            preflight_geo_check(drv)
        self.assertIn("CA", str(ctx.exception))

    def test_no_such_window_raises_SessionFlaggedError(self):
        from selenium.common.exceptions import NoSuchWindowException

        drv = MagicMock()
        drv.get.side_effect = NoSuchWindowException("window gone")
        with self.assertRaises(SessionFlaggedError):
            preflight_geo_check(drv)

    def test_malformed_json_raises_GeoCheckFailedError(self):
        drv = _make_driver("this is <html>, not JSON")
        with self.assertRaises(GeoCheckFailedError):
            preflight_geo_check(drv)

    def test_non_dict_json_raises_GeoCheckFailedError(self):
        drv = _make_driver(json.dumps(["US"]))
        with self.assertRaises(GeoCheckFailedError):
            preflight_geo_check(drv)

    def test_custom_expected_country(self):
        drv = _make_driver(json.dumps({"country": "GB"}))
        result = preflight_geo_check(drv, expected_country="GB")
        self.assertEqual(result["country"], "GB")


if __name__ == "__main__":
    unittest.main()
