"""Tests for BitBrowserClient._post() retry with exponential backoff.

Covers INV-BITBROWSER-RETRY-01:
* Transient failures (URLError, OSError, 5xx HTTPError) are retried.
* 4xx HTTPError fails fast (no retry).
* Retry count is bounded by BITBROWSER_RETRY_ATTEMPTS.
* Backoff wait doubles each attempt, capped at BITBROWSER_RETRY_WAIT_MAX_S.
"""
# pylint: disable=protected-access
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from modules.cdp.fingerprint import BitBrowserClient


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _ok(payload=None):
    body = json.dumps(payload or {"data": {"id": "p1"}}).encode("utf-8")
    return _FakeResp(body)


class RetryTransientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BITBROWSER_RETRY_ATTEMPTS": "3",
            "BITBROWSER_RETRY_WAIT_INITIAL_S": "0",
            "BITBROWSER_RETRY_WAIT_MAX_S": "0",
        })
        self.env.start()
        self.client = BitBrowserClient("http://127.0.0.1:54345", "k")

    def tearDown(self):
        self.env.stop()

    def test_retries_on_urlerror_then_succeeds(self):
        calls = {"n": 0}

        def side_effect(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 2:
                raise urllib.error.URLError("boom")
            return _ok()

        with patch("urllib.request.urlopen", side_effect=side_effect):
            data = self.client._post("/x", {})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(data, {"id": "p1"})

    def test_retries_on_oserror_then_succeeds(self):
        calls = {"n": 0}

        def side_effect(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("transient")
            return _ok()

        with patch("urllib.request.urlopen", side_effect=side_effect):
            data = self.client._post("/x", {})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(data, {"id": "p1"})

    def test_retries_on_5xx_then_succeeds(self):
        calls = {"n": 0}
        http500 = urllib.error.HTTPError(
            url="http://x", code=503, msg="unavailable", hdrs=None, fp=io.BytesIO(b"")
        )

        def side_effect(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 2:
                raise http500
            return _ok()

        with patch("urllib.request.urlopen", side_effect=side_effect):
            data = self.client._post("/x", {})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(data, {"id": "p1"})

    def test_does_not_retry_on_4xx(self):
        calls = {"n": 0}
        http404 = urllib.error.HTTPError(
            url="http://x", code=404, msg="nope", hdrs=None, fp=io.BytesIO(b"")
        )

        def side_effect(*_a, **_k):
            calls["n"] += 1
            raise http404

        with patch("urllib.request.urlopen", side_effect=side_effect):
            with self.assertRaises(urllib.error.HTTPError):
                self.client._post("/x", {})
        self.assertEqual(calls["n"], 1)

    def test_raises_after_exhausting_attempts(self):
        calls = {"n": 0}

        def side_effect(*_a, **_k):
            calls["n"] += 1
            raise urllib.error.URLError("always")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            with self.assertRaises(urllib.error.URLError):
                self.client._post("/x", {})
        self.assertEqual(calls["n"], 3)


class RetryBackoffTests(unittest.TestCase):
    def test_exponential_backoff_capped(self):
        env = patch.dict(os.environ, {
            "BITBROWSER_RETRY_ATTEMPTS": "4",
            "BITBROWSER_RETRY_WAIT_INITIAL_S": "0.1",
            "BITBROWSER_RETRY_WAIT_MAX_S": "0.25",
        })
        env.start()
        self.addCleanup(env.stop)
        client = BitBrowserClient("http://127.0.0.1:54345", "k")
        waits = []

        def fake_sleep(s):
            waits.append(s)

        def side_effect(*_a, **_k):
            raise urllib.error.URLError("boom")

        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch("modules.cdp.fingerprint.time.sleep", side_effect=fake_sleep):
            with self.assertRaises(urllib.error.URLError):
                client._post("/x", {})
        # 4 attempts → 3 sleeps: 0.1, 0.2, 0.25 (capped)
        self.assertEqual(len(waits), 3)
        self.assertAlmostEqual(waits[0], 0.1, places=6)
        self.assertAlmostEqual(waits[1], 0.2, places=6)
        self.assertAlmostEqual(waits[2], 0.25, places=6)


class RetryAttemptsOverrideTests(unittest.TestCase):
    def test_single_attempt_when_env_is_1(self):
        env = patch.dict(os.environ, {
            "BITBROWSER_RETRY_ATTEMPTS": "1",
            "BITBROWSER_RETRY_WAIT_INITIAL_S": "0",
            "BITBROWSER_RETRY_WAIT_MAX_S": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        client = BitBrowserClient("http://127.0.0.1:54345", "k")
        calls = {"n": 0}

        def side_effect(*_a, **_k):
            calls["n"] += 1
            raise urllib.error.URLError("boom")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            with self.assertRaises(urllib.error.URLError):
                client._post("/x", {})
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
