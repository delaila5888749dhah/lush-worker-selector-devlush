"""P2-2 tests: URL_PAYMENT and URL_EGIFT env override (#121).

Verifies that setting GIVEX_PAYMENT_URL / GIVEX_EGIFT_URL environment
variables causes driver.py to use the overridden URLs rather than the
hardcoded production defaults.

Override tests run in a subprocess to avoid ``importlib.reload`` side-effects
that would invalidate class references held by other test modules.
"""
import os
import subprocess
import sys
import unittest

import modules.cdp.driver as drv

_STAGING_PAYMENT = "https://staging.givex.com/payment.html"
_STAGING_EGIFT = "https://staging.givex.com/e-gifts/"
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class UrlEnvOverrideTest(unittest.TestCase):
    """Verify URL_PAYMENT and URL_EGIFT read from environment."""

    def test_payment_url_default(self):
        """Without env override, URL_PAYMENT is the production URL."""
        self.assertIn("wwws-usa2.givex.com", drv.URL_PAYMENT)
        self.assertIn("payment.html", drv.URL_PAYMENT)

    def test_egift_url_default(self):
        """Without env override, URL_EGIFT is the production URL."""
        self.assertIn("wwws-usa2.givex.com", drv.URL_EGIFT)
        self.assertIn("e-gifts", drv.URL_EGIFT)

    def _check_env_override(self, env_key, expected_value, module_attr):
        """Run a subprocess to verify env override without module reload side-effects."""
        code = (
            f"import os; os.environ['{env_key}'] = '{expected_value}'; "
            f"from modules.cdp.driver import {module_attr}; "
            f"print({module_attr})"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=_PROJECT_DIR,
        )
        self.assertEqual(result.returncode, 0, f"Subprocess failed: {result.stderr}")
        self.assertEqual(result.stdout.strip(), expected_value)

    def test_payment_url_override(self):
        """Setting GIVEX_PAYMENT_URL overrides URL_PAYMENT."""
        self._check_env_override("GIVEX_PAYMENT_URL", _STAGING_PAYMENT, "URL_PAYMENT")

    def test_egift_url_override(self):
        """Setting GIVEX_EGIFT_URL overrides URL_EGIFT."""
        self._check_env_override("GIVEX_EGIFT_URL", _STAGING_EGIFT, "URL_EGIFT")


if __name__ == "__main__":
    unittest.main()
