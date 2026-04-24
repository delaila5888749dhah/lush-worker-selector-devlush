"""Tests for BITBROWSER_ENDPOINT scheme/host safety warnings.

Plain HTTP is only safe against loopback hosts (127.0.0.1 / localhost / ::1).
On any other host the API key transits in clear-text, so the client must
warn. With ``BITBROWSER_ENDPOINT_STRICT=1`` the condition escalates to
``ValueError`` instead of a warning.
"""
import logging
import os
import unittest
from unittest.mock import patch

from modules.cdp.fingerprint import BitBrowserClient


class LoopbackHttpIsSilentTests(unittest.TestCase):
    def test_http_loopback_ipv4_no_warning(self):
        with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
            logging.getLogger("modules.cdp.fingerprint").warning("sentinel")
            BitBrowserClient("http://127.0.0.1:54345", "k")
        # Only the sentinel — no additional warning emitted by __init__.
        self.assertEqual(len(cm.output), 1)
        self.assertIn("sentinel", cm.output[0])

    def test_http_localhost_no_warning(self):
        with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
            logging.getLogger("modules.cdp.fingerprint").warning("sentinel")
            BitBrowserClient("http://localhost:54345", "k")
        self.assertEqual(len(cm.output), 1)

    def test_https_remote_no_warning(self):
        with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
            logging.getLogger("modules.cdp.fingerprint").warning("sentinel")
            BitBrowserClient("https://remote.example.com", "k")
        self.assertEqual(len(cm.output), 1)


class NonLoopbackHttpWarnsTests(unittest.TestCase):
    def test_http_remote_host_emits_warning(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BITBROWSER_ENDPOINT_STRICT", None)
            with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
                BitBrowserClient("http://10.0.0.5:54345", "k")
        joined = "\n".join(cm.output)
        self.assertIn("10.0.0.5", joined)
        self.assertIn("clear-text", joined)

    def test_http_remote_hostname_emits_warning(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BITBROWSER_ENDPOINT_STRICT", None)
            with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
                BitBrowserClient("http://bitbrowser.example.com:54345", "k")
        joined = "\n".join(cm.output)
        self.assertIn("bitbrowser.example.com", joined)


class StrictModeRaisesTests(unittest.TestCase):
    def test_strict_mode_raises_on_non_loopback_http(self):
        with patch.dict(os.environ, {"BITBROWSER_ENDPOINT_STRICT": "1"}):
            with self.assertRaises(ValueError):
                BitBrowserClient("http://10.0.0.5:54345", "k")

    def test_strict_mode_allows_loopback_http(self):
        with patch.dict(os.environ, {"BITBROWSER_ENDPOINT_STRICT": "1"}):
            BitBrowserClient("http://127.0.0.1:54345", "k")  # no raise

    def test_strict_mode_allows_https(self):
        with patch.dict(os.environ, {"BITBROWSER_ENDPOINT_STRICT": "1"}):
            BitBrowserClient("https://remote.example.com", "k")  # no raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
