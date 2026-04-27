"""Tests for Givex URL host allowlist (issue [P2] A3 audit).

Verifies that ``modules/cdp/driver.py`` validates ``GIVEX_EGIFT_URL`` /
``GIVEX_PAYMENT_URL`` env overrides against ``_ALLOWED_GIVEX_HOSTS`` at
module import time. Foreign hosts must raise ``RuntimeError`` unless
``ALLOW_NON_PROD_GIVEX_HOSTS=1`` is set, in which case a WARNING is
emitted and the URL is accepted.

Each test loads ``driver.py`` as an isolated module (``importlib.util``)
so ``importlib.reload`` side-effects don't leak between tests.
"""
import importlib.util
import logging
import os
import unittest
from unittest.mock import patch

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DRIVER_PATH = os.path.join(_PROJECT_DIR, "modules", "cdp", "driver.py")

_FOREIGN_EGIFT = "https://evil.example.com/e-gifts/"
_FOREIGN_PAYMENT = "https://evil.example.com/payment.html"


def _load_driver(env_patch):
    """Load driver.py as an isolated module with patched environment."""
    env = os.environ.copy()
    for key, value in env_patch.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    spec = importlib.util.spec_from_file_location(
        "_test_driver_url_allowlist", _DRIVER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, env, clear=True):
        spec.loader.exec_module(module)
    return module


class UrlAllowlistTest(unittest.TestCase):
    """Validate ``_validate_url`` host-allowlist enforcement."""

    def test_default_urls_pass(self):
        """No env override → defaults parse to allowlisted host."""
        drv = _load_driver({
            "GIVEX_EGIFT_URL": None,
            "GIVEX_PAYMENT_URL": None,
            "ALLOW_NON_PROD_GIVEX_HOSTS": None,
        })
        self.assertIn("wwws-usa2.givex.com", drv.URL_EGIFT)
        self.assertIn("wwws-usa2.givex.com", drv.URL_PAYMENT)
        self.assertEqual(drv._ALLOWED_GIVEX_HOSTS, ("wwws-usa2.givex.com",))

    def test_prod_host_override_passes(self):
        """Explicit override pointing at the prod host is accepted."""
        prod = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/other.html"
        drv = _load_driver({
            "GIVEX_EGIFT_URL": prod,
            "GIVEX_PAYMENT_URL": None,
            "ALLOW_NON_PROD_GIVEX_HOSTS": None,
        })
        self.assertEqual(drv.URL_EGIFT, prod)

    def test_foreign_egift_host_without_flag_raises(self):
        """Foreign GIVEX_EGIFT_URL without flag → RuntimeError at import."""
        with self.assertRaises(RuntimeError) as ctx:
            _load_driver({
                "GIVEX_EGIFT_URL": _FOREIGN_EGIFT,
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": None,
            })
        msg = str(ctx.exception)
        self.assertIn("GIVEX_EGIFT_URL", msg)
        self.assertIn("evil.example.com", msg)
        self.assertIn("ALLOW_NON_PROD_GIVEX_HOSTS", msg)

    def test_foreign_payment_host_without_flag_raises(self):
        """Foreign GIVEX_PAYMENT_URL without flag → RuntimeError at import."""
        with self.assertRaises(RuntimeError) as ctx:
            _load_driver({
                "GIVEX_EGIFT_URL": None,
                "GIVEX_PAYMENT_URL": _FOREIGN_PAYMENT,
                "ALLOW_NON_PROD_GIVEX_HOSTS": None,
            })
        self.assertIn("GIVEX_PAYMENT_URL", str(ctx.exception))

    def test_foreign_host_with_flag_passes_and_warns(self):
        """Flag set → foreign host accepted, WARNING logged."""
        with self.assertLogs("_test_driver_url_allowlist", level="WARNING") as cap:
            drv = _load_driver({
                "GIVEX_EGIFT_URL": _FOREIGN_EGIFT,
                "GIVEX_PAYMENT_URL": _FOREIGN_PAYMENT,
                "ALLOW_NON_PROD_GIVEX_HOSTS": "1",
            })
        self.assertEqual(drv.URL_EGIFT, _FOREIGN_EGIFT)
        self.assertEqual(drv.URL_PAYMENT, _FOREIGN_PAYMENT)
        joined = "\n".join(cap.output)
        self.assertIn("GIVEX_EGIFT_URL", joined)
        self.assertIn("GIVEX_PAYMENT_URL", joined)
        self.assertIn("evil.example.com", joined)

    def test_flag_other_value_does_not_bypass(self):
        """Only the literal value '1' enables the bypass."""
        with self.assertRaises(RuntimeError):
            _load_driver({
                "GIVEX_EGIFT_URL": _FOREIGN_EGIFT,
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": "true",
            })


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
