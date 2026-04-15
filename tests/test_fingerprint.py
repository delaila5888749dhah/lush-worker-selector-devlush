"""Unit tests for modules.cdp.fingerprint.

Covers BitBrowser API client round-trips, factory behaviour,
session lifecycle cleanup, and runtime profile accessor delegation.
"""
# pylint: disable=duplicate-code

import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer  # pylint: disable=no-name-in-module

from modules.cdp.fingerprint import (
    BitBrowserClient,
    BitBrowserSession,
    get_bitbrowser_client,
)
import modules.cdp.main as cdp
from modules.cdp.main import get_browser_profile, register_browser_profile
from integration import runtime


class _BitBrowserMockHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler that emulates BitBrowser API endpoints."""

    _state_lock = threading.Lock()
    _counter = 0
    _calls = []  # pylint: disable=dangerous-default-value

    @classmethod
    def reset_state(cls):
        """Reset shared counter and recorded calls."""
        with cls._state_lock:
            cls._counter = 0
            cls._calls = []

    @classmethod
    def snapshot_calls(cls):
        """Return a copy of recorded API calls."""
        with cls._state_lock:
            return list(cls._calls)

    def _write_json(self, status_code, payload):
        """Serialise *payload* as JSON and send with *status_code*."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # pylint: disable=invalid-name
        """Handle GET requests (browser list endpoint)."""
        if self.path == "/api/v1/browser/list":
            self._write_json(200, {"data": []})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self):  # pylint: disable=invalid-name
        """Handle POST requests (create, open, close, delete)."""
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        with self._state_lock:
            self._calls.append((self.path, payload))
        if self.path == "/api/v1/browser/create":
            with self._state_lock:
                _BitBrowserMockHandler._counter += 1  # pylint: disable=protected-access
                profile_id = f"profile-{_BitBrowserMockHandler._counter}"  # pylint: disable=protected-access
            self._write_json(200, {"data": {"id": profile_id}})
            return
        if self.path == "/api/v1/browser/open":
            profile_id = payload.get("id")
            self._write_json(
                200,
                {
                    "data": {
                        "http": "http://127.0.0.1:8080",
                        "webdriver": f"ws://127.0.0.1:9222/{profile_id}",
                    }
                },
            )
            return
        if self.path in ("/api/v1/browser/close", "/api/v1/browser/delete"):
            self._write_json(200, {"ok": True})
            return
        self._write_json(404, {"error": "not found"})

    def log_message(self, format, *args):  # noqa: A002  # pylint: disable=redefined-builtin
        """Suppress server log output during tests."""
        return


class BitBrowserClientTests(unittest.TestCase):
    """Tests for BitBrowserClient HTTP round-trips against mock server."""

    def setUp(self):
        """Start mock HTTP server on a random port."""
        _BitBrowserMockHandler.reset_state()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _BitBrowserMockHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        self._endpoint = f"http://{host}:{port}"
        self.client = BitBrowserClient(self._endpoint, api_key="k")

    def tearDown(self):
        """Shut down mock HTTP server."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def test_round_trip_create_launch_close_delete(self):
        """Full lifecycle: create → launch → close → delete."""
        profile_id = self.client.create_profile()
        launch_data = self.client.launch_profile(profile_id)
        self.client.close_profile(profile_id)
        self.client.delete_profile(profile_id)

        self.assertEqual(profile_id, "profile-1")
        self.assertIn("http", launch_data)
        self.assertIn("webdriver", launch_data)
        calls = _BitBrowserMockHandler.snapshot_calls()
        self.assertEqual(
            [path for path, _ in calls],
            [
                "/api/v1/browser/create",
                "/api/v1/browser/open",
                "/api/v1/browser/close",
                "/api/v1/browser/delete",
            ],
        )

    def test_two_workers_get_two_different_profile_ids(self):
        """Two create_profile calls yield distinct IDs."""
        profile_a = self.client.create_profile()
        profile_b = self.client.create_profile()
        self.assertNotEqual(profile_a, profile_b)
        register_browser_profile("worker-1", profile_a)
        register_browser_profile("worker-2", profile_b)
        self.assertEqual(get_browser_profile("worker-1"), profile_a)
        self.assertEqual(get_browser_profile("worker-2"), profile_b)


class BitBrowserFactoryTests(unittest.TestCase):
    """Tests for get_bitbrowser_client factory."""

    def test_get_bitbrowser_client_returns_none_without_api_key(self):
        """Factory returns None when BITBROWSER_API_KEY is unset."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_bitbrowser_client())


class BitBrowserSessionTests(unittest.TestCase):
    """Tests for BitBrowserSession context manager."""

    def test_exit_cleanup_errors_do_not_propagate(self):
        """__exit__ swallows close/delete errors instead of propagating."""
        fake_client = MagicMock()
        fake_client.create_profile.return_value = "profile-x"
        fake_client.launch_profile.return_value = {"webdriver": "ws://127.0.0.1:9222/x"}
        fake_client.close_profile.side_effect = urllib.error.URLError("close failed")
        fake_client.delete_profile.side_effect = urllib.error.URLError("delete failed")

        with BitBrowserSession(fake_client) as (profile_id, webdriver_url):
            self.assertEqual(profile_id, "profile-x")
            self.assertEqual(webdriver_url, "ws://127.0.0.1:9222/x")


def _reset_bitbrowser_registry():
    """Clear the bitbrowser registry via the public API helper."""
    with cdp._registry_lock:  # pylint: disable=protected-access
        cdp._bitbrowser_registry.clear()  # pylint: disable=protected-access


class RuntimeBrowserProfileAccessorTests(unittest.TestCase):
    """Tests for runtime.get_worker_browser_profile delegation."""

    def setUp(self):
        """Clear bitbrowser registry before each test."""
        _reset_bitbrowser_registry()

    def tearDown(self):
        """Clear bitbrowser registry after each test."""
        _reset_bitbrowser_registry()

    def test_runtime_returns_registered_profile(self):
        """Registered profile is returned via runtime accessor."""
        register_browser_profile("worker-a", "profile-a")
        self.assertEqual(runtime.get_worker_browser_profile("worker-a"), "profile-a")

    def test_runtime_returns_none_for_unregistered_worker(self):
        """Unregistered worker returns None via runtime accessor."""
        self.assertIsNone(runtime.get_worker_browser_profile("worker-missing"))
