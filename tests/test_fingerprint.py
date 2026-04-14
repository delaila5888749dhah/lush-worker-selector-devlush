"""Unit tests for modules.cdp.fingerprint."""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from modules.cdp.fingerprint import (
    BitBrowserClient,
    BitBrowserSession,
    get_bitbrowser_client,
)
from modules.cdp.main import get_browser_profile, register_browser_profile


class _BitBrowserMockHandler(BaseHTTPRequestHandler):
    _state_lock = threading.Lock()
    _counter = 0
    _calls = []

    @classmethod
    def reset_state(cls):
        with cls._state_lock:
            cls._counter = 0
            cls._calls = []

    @classmethod
    def snapshot_calls(cls):
        with cls._state_lock:
            return list(cls._calls)

    def _write_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # pylint: disable=invalid-name
        if self.path == "/api/v1/browser/list":
            self._write_json(200, {"data": []})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self):  # pylint: disable=invalid-name
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        with self._state_lock:
            self._calls.append((self.path, payload))
        if self.path == "/api/v1/browser/create":
            with self._state_lock:
                self.__class__._counter += 1
                profile_id = f"profile-{self.__class__._counter}"
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

    def log_message(self, _fmt, *args):  # pylint: disable=unused-argument
        return


class BitBrowserClientTests(unittest.TestCase):
    def setUp(self):
        _BitBrowserMockHandler.reset_state()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _BitBrowserMockHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        self._endpoint = f"http://{host}:{port}"
        self.client = BitBrowserClient(self._endpoint, api_key="k")

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def test_round_trip_create_launch_close_delete(self):
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
        profile_a = self.client.create_profile()
        profile_b = self.client.create_profile()
        self.assertNotEqual(profile_a, profile_b)
        register_browser_profile("worker-1", profile_a)
        register_browser_profile("worker-2", profile_b)
        self.assertEqual(get_browser_profile("worker-1"), profile_a)
        self.assertEqual(get_browser_profile("worker-2"), profile_b)


class BitBrowserFactoryTests(unittest.TestCase):
    def test_get_bitbrowser_client_returns_none_without_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_bitbrowser_client())


class BitBrowserSessionTests(unittest.TestCase):
    def test_exit_cleanup_errors_do_not_propagate(self):
        fake_client = MagicMock()
        fake_client.create_profile.return_value = "profile-x"
        fake_client.launch_profile.return_value = {"webdriver": "ws://127.0.0.1:9222/x"}
        fake_client.close_profile.side_effect = RuntimeError("close failed")
        fake_client.delete_profile.side_effect = RuntimeError("delete failed")

        with BitBrowserSession(fake_client) as (profile_id, webdriver_url):
            self.assertEqual(profile_id, "profile-x")
            self.assertEqual(webdriver_url, "ws://127.0.0.1:9222/x")
