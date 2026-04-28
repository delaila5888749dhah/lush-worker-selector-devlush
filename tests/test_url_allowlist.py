"""Tests for Givex URL host allowlist (issue [P2] A3 audit).

Verifies that ``modules/cdp/driver.py`` validates ``GIVEX_EGIFT_URL`` /
``GIVEX_PAYMENT_URL`` env overrides against ``_ALLOWED_GIVEX_HOSTS`` at
module import time. Foreign hosts must raise ``RuntimeError`` unless
``ALLOW_NON_PROD_GIVEX_HOSTS`` is truthy (``1``/``true``/``yes`` —
case-insensitive), in which case a WARNING is emitted and the URL is
accepted. The scheme must always be ``https`` regardless of the flag.

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

    def test_uppercase_host_passes(self):
        """``urlparse`` lowercases the host — uppercase override is accepted."""
        upper = "https://WWWS-USA2.GIVEX.COM/cws4.0/lushusa/e-gifts/"
        drv = _load_driver({
            "GIVEX_EGIFT_URL": upper,
            "GIVEX_PAYMENT_URL": None,
            "ALLOW_NON_PROD_GIVEX_HOSTS": None,
        })
        self.assertEqual(drv.URL_EGIFT, upper)

    def test_prod_host_with_port_passes(self):
        """Explicit port on the allowlisted host is accepted (host match only)."""
        ported = "https://wwws-usa2.givex.com:8443/cws4.0/lushusa/e-gifts/"
        drv = _load_driver({
            "GIVEX_EGIFT_URL": ported,
            "GIVEX_PAYMENT_URL": None,
            "ALLOW_NON_PROD_GIVEX_HOSTS": None,
        })
        self.assertEqual(drv.URL_EGIFT, ported)

    def test_userinfo_does_not_disguise_foreign_host(self):
        """``user@host`` URLs use ``host`` for matching — not the userinfo.

        ``https://attacker@evil.example.com`` must reject because the host
        is ``evil.example.com``, even though the userinfo contains the
        allowlisted name as a component.
        """
        sneaky = "https://wwws-usa2.givex.com@evil.example.com/payment.html"
        with self.assertRaises(RuntimeError) as ctx:
            _load_driver({
                "GIVEX_EGIFT_URL": None,
                "GIVEX_PAYMENT_URL": sneaky,
                "ALLOW_NON_PROD_GIVEX_HOSTS": None,
            })
        msg = str(ctx.exception)
        self.assertIn("evil.example.com", msg)

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

    def test_idn_homograph_host_rejected(self):
        """IDN/punycode lookalike (Cyrillic 'е' in 'givex') must reject.

        Exact comparison against ASCII allowlist means any Unicode
        homograph (or its punycode encoding) does not equal the
        allowlisted host.
        """
        # Cyrillic 'е' (U+0435) where ASCII 'e' should be.
        idn = "https://wwws-usa2.giv\u0435x.com/cws4.0/lushusa/e-gifts/"
        with self.assertRaises(RuntimeError):
            _load_driver({
                "GIVEX_EGIFT_URL": idn,
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": None,
            })

    def test_foreign_host_with_flag_passes_and_warns(self):
        """Flag set → foreign host accepted, WARNING logged per validated URL."""
        with self.assertLogs("_test_driver_url_allowlist", level="WARNING") as cap:
            drv = _load_driver({
                "GIVEX_EGIFT_URL": _FOREIGN_EGIFT,
                "GIVEX_PAYMENT_URL": _FOREIGN_PAYMENT,
                "ALLOW_NON_PROD_GIVEX_HOSTS": "1",
            })
        self.assertEqual(drv.URL_EGIFT, _FOREIGN_EGIFT)
        self.assertEqual(drv.URL_PAYMENT, _FOREIGN_PAYMENT)
        # Filter to bypass-warning records so the assertion does not trip
        # if an unrelated WARNING is added later in driver.py import.
        bypass_records = [
            r for r in cap.records if "INSECURE/DEGRADED" in r.getMessage()
        ]
        self.assertEqual(
            len(bypass_records), 2,
            f"expected one bypass WARNING per validated URL, got: {cap.output}",
        )
        joined = "\n".join(r.getMessage() for r in bypass_records)
        self.assertIn("GIVEX_EGIFT_URL", joined)
        self.assertIn("GIVEX_PAYMENT_URL", joined)
        self.assertIn("evil.example.com", joined)

    def test_truthy_flag_values_enable_bypass(self):
        """Repo convention: ``1``, ``true``, ``yes`` (case-insensitive) all bypass."""
        for raw in ("1", "true", "True", "TRUE", "yes", "Yes", " 1 ", "  true\t"):
            with self.subTest(value=raw):
                drv = _load_driver({
                    "GIVEX_EGIFT_URL": _FOREIGN_EGIFT,
                    "GIVEX_PAYMENT_URL": None,
                    "ALLOW_NON_PROD_GIVEX_HOSTS": raw,
                })
                self.assertEqual(drv.URL_EGIFT, _FOREIGN_EGIFT)

    def test_falsy_flag_values_do_not_bypass(self):
        """Empty/0/false/no/garbage values must keep strict enforcement."""
        for raw in ("", "0", "false", "False", "no", "No", "01", "off", "garbage"):
            with self.subTest(value=raw):
                with self.assertRaises(RuntimeError):
                    _load_driver({
                        "GIVEX_EGIFT_URL": _FOREIGN_EGIFT,
                        "GIVEX_PAYMENT_URL": None,
                        "ALLOW_NON_PROD_GIVEX_HOSTS": raw,
                    })


class UrlSchemeEnforcementTest(unittest.TestCase):
    """Scheme must be ``https`` — no http downgrade, no exotic schemes."""

    def test_http_on_allowlisted_host_rejected(self):
        """``http://`` on the prod host is still rejected (downgrade attack)."""
        downgrade = "http://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/"
        with self.assertRaises(RuntimeError) as ctx:
            _load_driver({
                "GIVEX_EGIFT_URL": downgrade,
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": None,
            })
        self.assertIn("scheme", str(ctx.exception))

    def test_http_not_bypassable_even_with_flag(self):
        """``ALLOW_NON_PROD_GIVEX_HOSTS`` does NOT permit http downgrade."""
        downgrade = "http://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/"
        with self.assertRaises(RuntimeError):
            _load_driver({
                "GIVEX_EGIFT_URL": downgrade,
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": "1",
            })

    def test_javascript_scheme_rejected(self):
        """``javascript:`` URLs are rejected."""
        with self.assertRaises(RuntimeError):
            _load_driver({
                "GIVEX_EGIFT_URL": "javascript:alert(1)",
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": "1",
            })

    def test_path_only_url_rejected(self):
        """A scheme-less / hostname-less path is rejected."""
        with self.assertRaises(RuntimeError):
            _load_driver({
                "GIVEX_EGIFT_URL": "/just/path",
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": "1",
            })

    def test_empty_string_rejected(self):
        """An empty override string is rejected (no scheme, no host)."""
        with self.assertRaises(RuntimeError):
            _load_driver({
                "GIVEX_EGIFT_URL": "",
                "GIVEX_PAYMENT_URL": None,
                "ALLOW_NON_PROD_GIVEX_HOSTS": "1",
            })


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
