"""Phase 2 — BitBrowserSession pool-mode integration tests (Blueprint §2.1).

Covers INV-POOL-INT, POOL-RANDOMIZE, POOL-NO-DELETE, POOL-EVICT.

Tests:
  - test_session_uses_pool_client_when_pool_mode_1
  - test_pool_mode_call_sequence
  - test_runtime_404_on_browser_open_evicts_profile
  - test_runtime_404_on_update_partial_evicts_profile
  - test_worker_count_exceeds_pool_size_timeout_e2e
  - test_legacy_session_still_creates_and_deletes
  - test_pool_mode_release_on_exception_does_not_delete
"""
import os
import threading
import time
import unittest
import unittest.mock as um
import urllib.error
from unittest.mock import patch

from modules.cdp.fingerprint import (
    BitBrowserClient,
    BitBrowserPoolClient,
    BitBrowserSession,
    get_bitbrowser_client,
)


def _make_pool_client(ids=None, **kwargs):
    ids = ids or ["p1", "p2", "p3"]
    return BitBrowserPoolClient(
        endpoint="http://127.0.0.1:54345",
        api_key="k",
        profile_ids=ids,
        **kwargs,
    )


class _RecordingPost:
    """Recording mock for BitBrowserPoolClient._post."""

    def __init__(self, responses=None):
        self.calls = []  # list of (path, payload)
        # Default responses keyed by path
        self._responses = responses or {}

    def __call__(self, path, payload, timeout=10):  # pragma: no cover - trivial
        self.calls.append((path, payload))
        resp = self._responses.get(path)
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            return resp(path, payload)
        if resp is not None:
            return resp
        if path == "/browser/open":
            return {"webdriver": "http://127.0.0.1:9999"}
        return {}

    def paths(self):
        return [p for p, _ in self.calls]


class TestSessionPoolAware(unittest.TestCase):
    """INV-POOL-INT — BitBrowserSession must use the pool client protocol."""

    def test_session_uses_pool_client_when_pool_mode_1(self):
        """create_profile/delete_profile NEVER called in pool mode; each pool
        primitive (acquire/randomize/launch/close/release) called exactly once."""
        with patch.dict(
            os.environ,
            {
                "BITBROWSER_API_KEY": "k",
                "BITBROWSER_POOL_MODE": "1",
                "BITBROWSER_PROFILE_IDS": "p1,p2,p3",
            },
            clear=False,
        ):
            client = get_bitbrowser_client()
        self.assertIsInstance(client, BitBrowserPoolClient)
        rec = _RecordingPost()
        with patch.object(BitBrowserPoolClient, "_post", new=rec), \
             patch.object(
                 BitBrowserClient, "create_profile",
                 side_effect=AssertionError("create_profile must NOT be called")
             ), \
             patch.object(
                 BitBrowserClient, "delete_profile",
                 side_effect=AssertionError("delete_profile must NOT be called")
             ), \
             patch.object(
                 BitBrowserPoolClient, "acquire_profile",
                 wraps=client.acquire_profile,
             ) as m_acq, \
             patch.object(
                 BitBrowserPoolClient, "randomize_fingerprint",
                 wraps=client.randomize_fingerprint,
             ) as m_rnd, \
             patch.object(
                 BitBrowserPoolClient, "launch_profile",
                 wraps=client.launch_profile,
             ) as m_launch, \
             patch.object(
                 BitBrowserPoolClient, "_close_browser",
                 wraps=client._close_browser,
             ) as m_close, \
             patch.object(
                 BitBrowserPoolClient, "release_profile",
                 wraps=client.release_profile,
             ) as m_rel:
            with BitBrowserSession(client) as (profile_id, wsurl):
                self.assertIn(profile_id, ["p1", "p2", "p3"])
                self.assertEqual(wsurl, "http://127.0.0.1:9999")
        # Each pool primitive must be called exactly once.
        self.assertEqual(m_acq.call_count, 1)
        self.assertEqual(m_rnd.call_count, 1)
        self.assertEqual(m_launch.call_count, 1)
        self.assertEqual(m_close.call_count, 1)
        self.assertEqual(m_rel.call_count, 1)
        # /browser/create and /browser/delete must never appear in the path list
        self.assertNotIn("/browser/create", rec.paths())
        self.assertNotIn("/browser/delete", rec.paths())
        self.assertNotIn("/api/v1/browser/create", rec.paths())
        self.assertNotIn("/api/v1/browser/delete", rec.paths())

    def test_pool_mode_call_sequence(self):
        """Exact order: acquire → /browser/update/partial → /browser/open → /browser/close."""
        client = _make_pool_client(ids=["only"])
        rec = _RecordingPost()
        with patch.object(BitBrowserPoolClient, "_post", new=rec):
            with BitBrowserSession(client) as (pid, _ws):
                self.assertEqual(pid, "only")
                # Inside the with-block: acquire has happened, update+open posted.
                self.assertEqual(
                    rec.paths(),
                    ["/browser/update/partial", "/browser/open"],
                )
            # After exit, close is posted last.
            self.assertEqual(
                rec.paths(),
                [
                    "/browser/update/partial",
                    "/browser/open",
                    "/browser/close",
                ],
            )
        # Profile returned to the pool (not busy).
        self.assertNotIn("only", client._busy)
        # Never called create or delete.
        for p in rec.paths():
            self.assertNotIn("create", p)
            self.assertNotIn("delete", p)

    def test_pool_mode_release_on_exception_does_not_delete(self):
        """POOL-NO-DELETE — exceptions still release but NEVER delete."""
        client = _make_pool_client(ids=["x"])
        rec = _RecordingPost()
        with patch.object(BitBrowserPoolClient, "_post", new=rec):
            with self.assertRaises(RuntimeError):
                with BitBrowserSession(client):
                    raise RuntimeError("boom")
        self.assertIn("/browser/close", rec.paths())
        for p in rec.paths():
            self.assertNotIn("delete", p)
        self.assertNotIn("x", client._busy)


class TestPoolEvict404(unittest.TestCase):
    """POOL-EVICT — 404 responses must evict from the pool."""

    def _http_404(self):
        return urllib.error.HTTPError(
            url="http://x", code=404, msg="Not Found",
            hdrs=None, fp=None,
        )

    def test_runtime_404_on_update_partial_evicts_profile(self):
        client = _make_pool_client(ids=["p1", "p2", "p3"])
        rec = _RecordingPost(responses={
            "/browser/update/partial": self._http_404(),
        })
        with patch.object(BitBrowserPoolClient, "_post", new=rec):
            with self.assertRaises(RuntimeError):
                with BitBrowserSession(client):
                    pass  # pragma: no cover
        self.assertEqual(len(client._pool), 2)
        self.assertNotIn("p1", client._pool)

    def test_runtime_404_on_browser_open_evicts_profile(self):
        """404 from /browser/open must also evict the profile; cursor
        re-anchors so the next acquire does not IndexError."""
        client = _make_pool_client(ids=["p1", "p2", "p3"])
        http_404 = self._http_404()
        rec = _RecordingPost(responses={"/browser/open": http_404})
        with patch.object(BitBrowserPoolClient, "_post", new=rec):
            with self.assertRaises(urllib.error.HTTPError):
                with BitBrowserSession(client):
                    pass  # pragma: no cover
        # Acquired profile (p1) was evicted because /browser/open 404'd.
        self.assertEqual(len(client._pool), 2)
        self.assertNotIn("p1", client._pool)
        # Cursor must be within bounds for a subsequent acquire — no IndexError.
        self.assertLess(client._cursor, len(client._pool))
        rec2 = _RecordingPost()
        with patch.object(BitBrowserPoolClient, "_post", new=rec2):
            next_pid = client.acquire_profile()
        self.assertIn(next_pid, ["p2", "p3"])
        client.release_profile(next_pid)


class TestWorkerCountExceedsPoolSize(unittest.TestCase):
    """When N workers contend over M<N profiles, excess workers must time out."""

    def test_worker_count_exceeds_pool_size_timeout_e2e(self):
        """Pool size 2, 3 contending workers → the loser raises
        RuntimeError whose message contains 'All N profiles BUSY'."""
        client = _make_pool_client(
            ids=["p1", "p2"],
            acquire_timeout_s=0.3,
            poll_interval_s=0.05,
        )
        rec = _RecordingPost()
        results = []  # list of ("ok", pid) or ("timeout", msg)
        results_lock = threading.Lock()
        proceed = threading.Event()
        acquired = threading.Semaphore(0)

        def run_one():
            try:
                with patch.object(BitBrowserPoolClient, "_post", new=rec):
                    with BitBrowserSession(client) as (pid, _ws):
                        with results_lock:
                            results.append(("ok", pid))
                        acquired.release()
                        proceed.wait(timeout=1.0)
            except RuntimeError as exc:
                with results_lock:
                    results.append(("timeout", str(exc)))

        threads = [threading.Thread(target=run_one) for _ in range(3)]
        for t in threads:
            t.start()
        # Wait until both holders have acquired, then let the loser time out.
        self.assertTrue(acquired.acquire(timeout=1.0))
        self.assertTrue(acquired.acquire(timeout=1.0))
        # Loser's acquire_timeout_s is 0.3s, so 0.5s is enough to time out.
        time.sleep(0.5)
        proceed.set()
        for t in threads:
            t.join(timeout=2.0)

        oks = [r for r in results if r[0] == "ok"]
        timeouts = [r for r in results if r[0] == "timeout"]
        self.assertEqual(len(oks), 2)
        self.assertEqual(len(timeouts), 1)
        # Message must identify the BUSY exhaustion condition.
        self.assertIn("All 2 profiles BUSY", timeouts[0][1])


class TestLegacyBackwardCompat(unittest.TestCase):
    """Non-pool clients keep the legacy create + delete flow."""

    def test_legacy_session_still_creates_and_deletes(self):
        client = um.Mock(spec=BitBrowserClient)
        client.create_profile.return_value = "legacy-id"
        client.launch_profile.return_value = {"webdriver": "http://127.0.0.1:1234"}
        # No acquire_profile / release_profile attrs ⇒ legacy path.
        # Use spec=BitBrowserClient so Mock enforces that.
        with BitBrowserSession(client) as (pid, ws):
            self.assertEqual(pid, "legacy-id")
            self.assertEqual(ws, "http://127.0.0.1:1234")
        client.create_profile.assert_called_once()
        client.launch_profile.assert_called_once_with("legacy-id")
        client.close_profile.assert_called_once_with("legacy-id")
        client.delete_profile.assert_called_once_with("legacy-id")
        # Legacy mode MUST NOT reach the pool-only randomize endpoint.
        self.assertFalse(
            hasattr(client, "randomize_fingerprint")
            and client.randomize_fingerprint.called,
            "legacy session must not call randomize_fingerprint "
            "(/browser/update/partial).",
        )


class TestPoolClientFactoryWarnings(unittest.TestCase):
    """Startup validation — pool-size warning + duplicate dedupe."""

    def test_duplicate_ids_deduped_with_warning(self):
        with patch.dict(
            os.environ,
            {
                "BITBROWSER_API_KEY": "k",
                "BITBROWSER_POOL_MODE": "1",
                "BITBROWSER_PROFILE_IDS": "a, b, a, c, b",
            },
            clear=False,
        ):
            with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
                client = get_bitbrowser_client()
        self.assertIsInstance(client, BitBrowserPoolClient)
        self.assertEqual(client._pool, ["a", "b", "c"])
        self.assertTrue(
            any("duplicate" in msg.lower() for msg in cm.output),
            f"Expected 'duplicate' warning; got {cm.output}",
        )

    def test_pool_size_less_than_2x_worker_count_warns(self):
        with patch.dict(
            os.environ,
            {
                "BITBROWSER_API_KEY": "k",
                "BITBROWSER_POOL_MODE": "1",
                "BITBROWSER_PROFILE_IDS": "a,b",
                "WORKER_COUNT": "4",
            },
            clear=False,
        ):
            with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
                client = get_bitbrowser_client()
        self.assertIsInstance(client, BitBrowserPoolClient)
        # len(pool)=2 < WORKER_COUNT*2=8 ⇒ warning must mention pool size.
        self.assertTrue(
            any(
                "pool" in msg.lower() and ("worker" in msg.lower() or "2x" in msg.lower())
                for msg in cm.output
            ),
            f"Expected pool<2x worker warning; got {cm.output}",
        )


if __name__ == "__main__":
    unittest.main()
