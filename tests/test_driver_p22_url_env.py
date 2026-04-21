"""P2-2 tests: URL_PAYMENT and URL_EGIFT env override (#121).

Verifies that setting GIVEX_PAYMENT_URL / GIVEX_EGIFT_URL environment
variables causes driver.py to use the overridden URLs rather than the
hardcoded production defaults.

Override tests load an isolated module object to avoid ``importlib.reload``
side-effects that would invalidate class references held by other test modules.
"""
import importlib.util
import os
import unittest
from unittest.mock import patch

_STAGING_PAYMENT = "https://staging.givex.com/payment.html"
_STAGING_EGIFT = "https://staging.givex.com/e-gifts/"
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DRIVER_PATH = os.path.join(_PROJECT_DIR, "modules", "cdp", "driver.py")


class UrlEnvOverrideTest(unittest.TestCase):
    """Verify URL_PAYMENT and URL_EGIFT read from environment."""

    def _load_driver(self, env_patch):
        """Load driver.py as an isolated module with patched environment."""
        env = os.environ.copy()
        for key, value in env_patch.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        spec = importlib.util.spec_from_file_location("_test_driver_env_override", _DRIVER_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, env, clear=True):
            spec.loader.exec_module(module)
        return module

    def test_payment_url_default(self):
        """Without env override, URL_PAYMENT is the production URL."""
        drv = self._load_driver({"GIVEX_PAYMENT_URL": None, "GIVEX_EGIFT_URL": None})
        self.assertIn("wwws-usa2.givex.com", drv.URL_PAYMENT)
        self.assertIn("payment.html", drv.URL_PAYMENT)

    def test_egift_url_default(self):
        """Without env override, URL_EGIFT is the production URL."""
        drv = self._load_driver({"GIVEX_PAYMENT_URL": None, "GIVEX_EGIFT_URL": None})
        self.assertIn("wwws-usa2.givex.com", drv.URL_EGIFT)
        self.assertIn("e-gifts", drv.URL_EGIFT)

    def test_payment_url_override(self):
        """Setting GIVEX_PAYMENT_URL overrides URL_PAYMENT."""
        drv = self._load_driver({"GIVEX_PAYMENT_URL": _STAGING_PAYMENT, "GIVEX_EGIFT_URL": None})
        self.assertEqual(drv.URL_PAYMENT, _STAGING_PAYMENT)

    def test_egift_url_override(self):
        """Setting GIVEX_EGIFT_URL overrides URL_EGIFT."""
        drv = self._load_driver({"GIVEX_PAYMENT_URL": None, "GIVEX_EGIFT_URL": _STAGING_EGIFT})
        self.assertEqual(drv.URL_EGIFT, _STAGING_EGIFT)


if __name__ == "__main__":
    unittest.main()
