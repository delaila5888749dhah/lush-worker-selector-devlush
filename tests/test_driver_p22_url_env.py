"""P2-2 tests: URL_PAYMENT and URL_EGIFT env override (#121).

Verifies that setting GIVEX_PAYMENT_URL / GIVEX_EGIFT_URL environment
variables causes driver.py to use the overridden URLs rather than the
hardcoded production defaults.
"""
import importlib
import os
import unittest

_STAGING_PAYMENT = "https://staging.givex.com/payment.html"
_STAGING_EGIFT = "https://staging.givex.com/e-gifts/"


class UrlEnvOverrideTest(unittest.TestCase):
    """Verify URL_PAYMENT and URL_EGIFT read from environment."""

    def _reload_driver(self, env_patch: dict):
        """Reload modules.cdp.driver with patched env vars and return the module."""
        import modules.cdp.driver as drv  # noqa: PLC0415
        # Remove keys with None value from environment; set others to their value
        env = os.environ.copy()
        for k, v in env_patch.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            return importlib.reload(drv)

    def test_payment_url_default(self):
        """Without env override, URL_PAYMENT is the production URL."""
        drv = self._reload_driver({"GIVEX_PAYMENT_URL": None, "GIVEX_EGIFT_URL": None})
        self.assertIn("wwws-usa2.givex.com", drv.URL_PAYMENT)
        self.assertIn("payment.html", drv.URL_PAYMENT)

    def test_egift_url_default(self):
        """Without env override, URL_EGIFT is the production URL."""
        drv = self._reload_driver({"GIVEX_PAYMENT_URL": None, "GIVEX_EGIFT_URL": None})
        self.assertIn("wwws-usa2.givex.com", drv.URL_EGIFT)
        self.assertIn("e-gifts", drv.URL_EGIFT)

    def test_payment_url_override(self):
        """Setting GIVEX_PAYMENT_URL overrides URL_PAYMENT."""
        drv = self._reload_driver({"GIVEX_PAYMENT_URL": _STAGING_PAYMENT})
        self.assertEqual(drv.URL_PAYMENT, _STAGING_PAYMENT)

    def test_egift_url_override(self):
        """Setting GIVEX_EGIFT_URL overrides URL_EGIFT."""
        drv = self._reload_driver({"GIVEX_EGIFT_URL": _STAGING_EGIFT})
        self.assertEqual(drv.URL_EGIFT, _STAGING_EGIFT)


# Make mock available in the module scope for _reload_driver
import unittest.mock  # noqa: E402 (must be after class definition to avoid circular ref issue)

if __name__ == "__main__":
    unittest.main()
