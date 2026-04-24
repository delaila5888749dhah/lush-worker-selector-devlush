"""Unit tests for BitBrowserPoolClient (Blueprint §2.1).

Ten checkpoints:
  1. round-robin sequential order
  2. wrap-around on full traversal
  3. BUSY profiles are skipped
  4. all-BUSY → timeout with RuntimeError
  5. thread-safety — no duplicate acquire under concurrency
  6. 404 eviction removes the profile from the pool
  7. release always clears BUSY even if _close_browser raises
  8. empty profile_ids list → ValueError at construction
  9. randomize_fingerprint posts the exact contract payload
 10. backward compatibility — POOL_MODE=0 returns the legacy client
"""
import os
import threading
import time
import unittest
from unittest.mock import patch

from modules.cdp.fingerprint import (
    BitBrowserClient,
    BitBrowserPoolClient,
    get_bitbrowser_client,
)


class TestPoolRoundRobin(unittest.TestCase):
    def _make_client(self, ids=None):
        ids = ids or ["id1", "id2", "id3", "id4", "id5"]
        return BitBrowserPoolClient(
            endpoint="http://127.0.0.1:54345",
            api_key="k",
            profile_ids=ids,
        )

    # 1 ─────────────────────────────────────────────────────
    def test_round_robin_sequential(self):
        c = self._make_client()
        picks = []
        for _ in range(5):
            pid = c.acquire_profile()
            picks.append(pid)
            with c._lock:
                c._busy.discard(pid)
        self.assertEqual(picks, ["id1", "id2", "id3", "id4", "id5"])

    # 2 ─────────────────────────────────────────────────────
    def test_wrap_around(self):
        c = self._make_client()
        for _ in range(5):
            pid = c.acquire_profile()
            with c._lock:
                c._busy.discard(pid)
        # 6th cycle wraps back to id1
        pid = c.acquire_profile()
        self.assertEqual(pid, "id1")

    # 3 ─────────────────────────────────────────────────────
    def test_busy_skip(self):
        c = self._make_client()
        with c._lock:
            c._busy.add("id1")
        pid = c.acquire_profile()
        self.assertEqual(pid, "id2")

    # 4 ─────────────────────────────────────────────────────
    def test_all_busy_timeout(self):
        c = self._make_client(ids=["only"])
        c._acquire_timeout_s = 0.2
        c._poll_interval_s = 0.05
        with c._lock:
            c._busy.add("only")
        t0 = time.time()
        with self.assertRaises(RuntimeError):
            c.acquire_profile()
        self.assertGreaterEqual(time.time() - t0, 0.2)

    # 5 ─────────────────────────────────────────────────────
    def test_thread_safety_no_duplicate_acquire(self):
        c = self._make_client(ids=[f"id{i}" for i in range(20)])
        acquired = []
        acquired_lock = threading.Lock()

        def worker():
            pid = c.acquire_profile()
            with acquired_lock:
                acquired.append(pid)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(acquired), 10)
        self.assertEqual(len(acquired), len(set(acquired)))

    # 6 ─────────────────────────────────────────────────────
    def test_profile_404_evicted_from_pool(self):
        c = self._make_client()
        c._evict_profile("id3")
        self.assertNotIn("id3", c._pool)
        self.assertEqual(len(c._pool), 4)

    # 7 ─────────────────────────────────────────────────────
    @patch.object(BitBrowserPoolClient, "_close_browser")
    def test_release_always_clears_busy_even_on_close_error(self, m_close):
        m_close.side_effect = RuntimeError("network")
        c = self._make_client()
        pid = c.acquire_profile()
        c.release_profile(pid)  # should not raise
        self.assertNotIn(pid, c._busy)

    # 8 ─────────────────────────────────────────────────────
    def test_pool_mode_empty_ids_raises(self):
        with self.assertRaises(ValueError):
            BitBrowserPoolClient(
                endpoint="http://x", api_key="k", profile_ids=[]
            )

    # 9 ─────────────────────────────────────────────────────
    @patch.object(BitBrowserPoolClient, "_post")
    def test_randomize_payload_exact(self, m_post):
        m_post.return_value = {}
        c = self._make_client()
        c.randomize_fingerprint("id1")
        m_post.assert_called_once_with(
            "/browser/update/partial",
            {
                "ids": ["id1"],
                "browserFingerPrint": {
                    "batchRandom": True,
                    "batchUpdateFingerPrint": True,
                },
            },
            timeout=10,
        )

    # 10 ────────────────────────────────────────────────────
    def test_backward_compat_legacy_mode_unaffected(self):
        with patch.dict(
            os.environ,
            {"BITBROWSER_API_KEY": "k", "BITBROWSER_POOL_MODE": "0"},
            clear=False,
        ):
            with patch.object(
                BitBrowserClient, "is_available", return_value=True
            ):
                client = get_bitbrowser_client()
        self.assertIsInstance(client, BitBrowserClient)
        self.assertNotIsInstance(client, BitBrowserPoolClient)


if __name__ == "__main__":
    unittest.main()
