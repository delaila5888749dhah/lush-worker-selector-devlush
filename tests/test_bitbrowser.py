"""Unit tests for modules.cdp.bitbrowser BitBrowser API client."""

import unittest
from unittest.mock import MagicMock, patch

import modules.cdp.bitbrowser as bitbrowser
from modules.cdp.bitbrowser import BITBROWSER_API_BASE


class TestLaunchProfile(unittest.TestCase):
    def _make_response(self, status_code=200, json_data=None):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_data or {
            "ws": {"selenium": "ws://127.0.0.1:9222/devtools/browser/abc"},
            "http": "http://127.0.0.1:9222",
        }
        if status_code >= 400:
            from requests.exceptions import HTTPError
            response.raise_for_status.side_effect = HTTPError(
                f"HTTP {status_code}", response=response
            )
        else:
            response.raise_for_status.return_value = None
        return response

    def test_launch_profile_returns_debugger_address(self):
        """launch_profile must POST to the correct endpoint and return response dict."""
        mock_response = self._make_response()
        with patch("modules.cdp.bitbrowser.requests.post", return_value=mock_response) as mock_post:
            result = bitbrowser.launch_profile("profile-123")

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("/browser/open", call_args[0][0])
        self.assertEqual(call_args[1]["json"], {"id": "profile-123"})
        self.assertIn("ws", result)
        self.assertIn("http", result)

    def test_launch_profile_posts_to_correct_url(self):
        """launch_profile must POST to BITBROWSER_API_BASE/browser/open."""
        mock_response = self._make_response()
        with patch("modules.cdp.bitbrowser.requests.post", return_value=mock_response) as mock_post:
            bitbrowser.launch_profile("my-profile")

        posted_url = mock_post.call_args[0][0]
        self.assertEqual(posted_url, f"{BITBROWSER_API_BASE}/browser/open")

    def test_launch_profile_raises_on_failure(self):
        """launch_profile must raise RuntimeError when the API returns 500."""
        mock_response = self._make_response(status_code=500)
        with patch("modules.cdp.bitbrowser.requests.post", return_value=mock_response):
            with self.assertRaises(RuntimeError) as ctx:
                bitbrowser.launch_profile("bad-profile")
        self.assertIn("bad-profile", str(ctx.exception))

    def test_launch_profile_raises_on_network_error(self):
        """launch_profile must raise RuntimeError on connection error."""
        import requests as _requests
        with patch(
            "modules.cdp.bitbrowser.requests.post",
            side_effect=_requests.ConnectionError("refused"),
        ):
            with self.assertRaises(RuntimeError):
                bitbrowser.launch_profile("unreachable")


class TestCloseProfile(unittest.TestCase):
    def _make_ok_response(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        return response

    def test_close_profile_calls_correct_endpoint(self):
        """close_profile must POST to BITBROWSER_API_BASE/browser/close."""
        mock_response = self._make_ok_response()
        with patch("modules.cdp.bitbrowser.requests.post", return_value=mock_response) as mock_post:
            bitbrowser.close_profile("profile-456")

        posted_url = mock_post.call_args[0][0]
        self.assertEqual(posted_url, f"{BITBROWSER_API_BASE}/browser/close")

    def test_close_profile_sends_profile_id(self):
        """close_profile must include the profile_id in the POST payload."""
        mock_response = self._make_ok_response()
        with patch("modules.cdp.bitbrowser.requests.post", return_value=mock_response) as mock_post:
            bitbrowser.close_profile("profile-789")

        self.assertEqual(mock_post.call_args[1]["json"], {"id": "profile-789"})

    def test_close_profile_raises_on_failure(self):
        """close_profile must raise RuntimeError on HTTP error."""
        from requests.exceptions import HTTPError
        response = MagicMock()
        response.raise_for_status.side_effect = HTTPError("404")
        with patch("modules.cdp.bitbrowser.requests.post", return_value=response):
            with self.assertRaises(RuntimeError):
                bitbrowser.close_profile("gone-profile")


class TestGetDebuggerAddress(unittest.TestCase):
    def _mock_launch(self, http_value):
        def _launch(profile_id, timeout=30):
            return {"ws": {"selenium": "ws://..."}, "http": http_value}
        return _launch

    def test_get_debugger_address_strips_scheme(self):
        """get_debugger_address must strip the http:// scheme from the address."""
        with patch.object(bitbrowser, "launch_profile", self._mock_launch("http://127.0.0.1:9222")):
            addr = bitbrowser.get_debugger_address("p-1")
        self.assertEqual(addr, "127.0.0.1:9222")

    def test_get_debugger_address_returns_plain_address(self):
        """get_debugger_address works when http field is already scheme-less."""
        with patch.object(bitbrowser, "launch_profile", self._mock_launch("127.0.0.1:9999")):
            addr = bitbrowser.get_debugger_address("p-2")
        self.assertEqual(addr, "127.0.0.1:9999")

    def test_get_debugger_address_raises_when_http_missing(self):
        """get_debugger_address raises RuntimeError when 'http' key is absent."""
        def _launch(profile_id, timeout=30):
            return {"ws": {"selenium": "ws://..."}}

        with patch.object(bitbrowser, "launch_profile", _launch):
            with self.assertRaises(RuntimeError) as ctx:
                bitbrowser.get_debugger_address("p-3")
        self.assertIn("http", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
