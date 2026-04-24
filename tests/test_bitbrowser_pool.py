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
import urllib.error
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


class TestPoolPhase3BCompletion(unittest.TestCase):
    """Phase 3B — 404 evict wiring, pool size guard, dedupe, cursor rewind."""

    def _make_client(self, ids=None):
        ids = ids or ["id1", "id2", "id3", "id4", "id5"]
        return BitBrowserPoolClient(
            endpoint="http://127.0.0.1:54345",
            api_key="k",
            profile_ids=ids,
        )

    @staticmethod
    def _raise_http_error(code):
        def _side_effect(path, payload, timeout=30):
            raise urllib.error.HTTPError(
                url="http://x" + path,
                code=code,
                msg=f"HTTP {code}",
                hdrs=None,
                fp=None,
            )

        return _side_effect

    # Task 1 ─ 404 eviction on /browser/open ──────────────────
    def test_runtime_404_on_browser_open_evicts_profile(self):
        c = self._make_client()
        # Mark target as BUSY to prove _busy is cleared too.
        with c._lock:
            c._busy.add("id3")

        with patch.object(
            BitBrowserPoolClient, "_post", side_effect=self._raise_http_error(404)
        ):
            with self.assertRaises(urllib.error.HTTPError):
                c.launch_profile("id3")
        self.assertNotIn("id3", c._pool)
        self.assertNotIn("id3", c._busy)
        # Next acquire must skip the evicted id.
        pid = c.acquire_profile()
        self.assertNotEqual(pid, "id3")

    # Task 1 ─ 404 eviction on /browser/update/partial ────────
    def test_runtime_404_on_update_partial_evicts_profile(self):
        c = self._make_client()
        with c._lock:
            c._busy.add("id2")

        with patch.object(
            BitBrowserPoolClient, "_post", side_effect=self._raise_http_error(404)
        ):
            with self.assertRaises(RuntimeError):
                c.randomize_fingerprint("id2")
        self.assertNotIn("id2", c._pool)
        self.assertNotIn("id2", c._busy)

    def test_non_404_httperror_propagates_without_evicting(self):
        for method_name in ("launch_profile", "randomize_fingerprint"):
            with self.subTest(method=method_name):
                c = self._make_client(ids=["a", "b", "c"])
                with c._lock:
                    c._busy.add("b")
                with patch.object(
                    BitBrowserPoolClient,
                    "_post",
                    side_effect=self._raise_http_error(500),
                ):
                    with self.assertRaises(urllib.error.HTTPError):
                        getattr(c, method_name)("b")
                self.assertEqual(c._pool, ["a", "b", "c"])
                self.assertIn("b", c._busy)

    # Task 2 ─ raise when pool < WORKER_COUNT ─────────────────
    def test_pool_size_less_than_worker_count_raises(self):
        with patch.dict(os.environ, {"WORKER_COUNT": "10"}, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                BitBrowserPoolClient(
                    endpoint="http://127.0.0.1:54345",
                    api_key="k",
                    profile_ids=["a", "b", "c", "d", "e"],
                )
        self.assertIn("WORKER_COUNT", str(ctx.exception))

    # Task 2 ─ warn when pool < 2×WORKER_COUNT ────────────────
    def test_pool_size_less_than_2x_worker_count_warns(self):
        with patch.dict(os.environ, {"WORKER_COUNT": "5"}, clear=False):
            with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
                client = BitBrowserPoolClient(
                    endpoint="http://127.0.0.1:54345",
                    api_key="k",
                    profile_ids=["a", "b", "c", "d", "e", "f", "g"],
                )
        self.assertEqual(len(client._pool), 7)  # no raise
        self.assertTrue(
            any("2x WORKER_COUNT" in msg or "2x worker" in msg.lower()
                for msg in cm.output),
            f"Expected <2x warning; got {cm.output}",
        )

    # Task 3 ─ dedupe warning ─────────────────────────────────
    def test_duplicate_profile_ids_deduped_with_warning(self):
        # Ensure WORKER_COUNT doesn't force a raise on the 2-entry pool.
        saved = os.environ.pop("WORKER_COUNT", None)
        try:
            with self.assertLogs("modules.cdp.fingerprint", level="WARNING") as cm:
                client = BitBrowserPoolClient(
                    endpoint="http://127.0.0.1:54345",
                    api_key="k",
                    profile_ids=["a", "a", "b"],
                )
        finally:
            if saved is not None:
                os.environ["WORKER_COUNT"] = saved
        self.assertEqual(client._pool, ["a", "b"])
        # Expect exactly one duplicate warning for the repeated "a" entry.
        dup_msgs = [
            m for m in cm.output
            if "Duplicate BitBrowser profile ID ignored: a" in m
        ]
        self.assertEqual(len(dup_msgs), 1, f"got {cm.output}")

    def test_worker_count_env_unset_skips_guards(self):
        saved = os.environ.pop("WORKER_COUNT", None)
        try:
            with patch("modules.cdp.fingerprint._log.warning") as m_warning:
                client = BitBrowserPoolClient(
                    endpoint="http://127.0.0.1:54345",
                    api_key="k",
                    profile_ids=["a"],
                )
        finally:
            if saved is not None:
                os.environ["WORKER_COUNT"] = saved
        self.assertEqual(client._pool, ["a"])
        m_warning.assert_not_called()

    def test_worker_count_empty_string_skips_guards(self):
        with patch.dict(os.environ, {"WORKER_COUNT": ""}, clear=False):
            with patch("modules.cdp.fingerprint._log.warning") as m_warning:
                client = BitBrowserPoolClient(
                    endpoint="http://127.0.0.1:54345",
                    api_key="k",
                    profile_ids=["a"],
                )
        self.assertEqual(client._pool, ["a"])
        m_warning.assert_not_called()

    def test_worker_count_zero_skips_guards(self):
        with patch.dict(os.environ, {"WORKER_COUNT": "0"}, clear=False):
            with patch("modules.cdp.fingerprint._log.warning") as m_warning:
                client = BitBrowserPoolClient(
                    endpoint="http://127.0.0.1:54345",
                    api_key="k",
                    profile_ids=["a"],
                )
        self.assertEqual(client._pool, ["a"])
        m_warning.assert_not_called()

    # Task 3 edge ─ empty-string profile IDs are filtered out ──
    def test_empty_string_profile_ids_are_filtered(self):
        saved = os.environ.pop("WORKER_COUNT", None)
        try:
            client = BitBrowserPoolClient(
                endpoint="http://127.0.0.1:54345",
                api_key="k",
                profile_ids=["a", "", "  ", "b", ""],
            )
        finally:
            if saved is not None:
                os.environ["WORKER_COUNT"] = saved
        self.assertEqual(client._pool, ["a", "b"])
        self.assertNotIn("", client._pool)

    def test_all_empty_string_profile_ids_raises(self):
        with self.assertRaises(ValueError):
            BitBrowserPoolClient(
                endpoint="http://127.0.0.1:54345",
                api_key="k",
                profile_ids=["", "  ", ""],
            )

    # Task 4 ─ cursor rewind after eviction ───────────────────
    def test_evict_rewinds_cursor_correctly(self):
        c = self._make_client(ids=["a", "b", "c"])
        # Position cursor past "b" (cursor=2 means next acquire returns "c").
        c._cursor = 2
        c._evict_profile("b")
        self.assertEqual(c._pool, ["a", "c"])
        # cursor was strictly > idx(1) ⇒ decremented to 1 ⇒ next is "c".
        self.assertEqual(c._cursor, 1)
        pid = c.acquire_profile()
        self.assertEqual(pid, "c")

    def test_evict_keeps_cursor_when_equal_idx(self):
        c = self._make_client(ids=["a", "b", "c"])
        c._cursor = 1
        c._evict_profile("b")
        self.assertEqual(c._pool, ["a", "c"])
        self.assertEqual(c._cursor, 1)
        self.assertEqual(c.acquire_profile(), "c")

    def test_evict_keeps_cursor_when_less_than_idx(self):
        c = self._make_client(ids=["a", "b", "c"])
        c._cursor = 0
        c._evict_profile("c")
        self.assertEqual(c._pool, ["a", "b"])
        self.assertEqual(c._cursor, 0)
        self.assertEqual(c.acquire_profile(), "a")

    def test_evict_last_slot_wraps_cursor_to_zero(self):
        c = self._make_client(ids=["a", "b", "c"])
        c._cursor = 2
        c._evict_profile("c")
        self.assertEqual(c._pool, ["a", "b"])
        self.assertEqual(c._cursor, 0)
        self.assertEqual(c.acquire_profile(), "a")

    def test_evict_when_pool_becomes_empty(self):
        c = self._make_client(ids=["only"])
        with c._lock:
            c._busy.add("only")
        c._evict_profile("only")
        self.assertEqual(c._pool, [])
        self.assertNotIn("only", c._busy)
        with self.assertRaises(RuntimeError) as ctx:
            c.acquire_profile()
        self.assertIn("Profile pool is empty", str(ctx.exception))

    # Regression ─ POOL_MODE=0 unaffected ─────────────────────
    def test_legacy_pool_mode_0_unaffected(self):
        with patch.dict(
            os.environ,
            {
                "BITBROWSER_API_KEY": "k",
                "BITBROWSER_POOL_MODE": "0",
                # Empty profile ids — must not trigger any pool-mode path.
                "BITBROWSER_PROFILE_IDS": "",
                "WORKER_COUNT": "99",
            },
            clear=False,
        ):
            with patch.object(
                BitBrowserClient, "is_available", return_value=True
            ):
                client = get_bitbrowser_client()
        self.assertIsInstance(client, BitBrowserClient)
        self.assertNotIsInstance(client, BitBrowserPoolClient)

    # E2E ─ workers > pool size triggers acquire timeout ──────
    def test_worker_count_exceeds_pool_size_timeout_e2e(self):
        # WORKER_COUNT=2 satisfies the init guard (pool=2 ≥ 2) but simulates
        # 3 concurrent workers by pre-marking both profiles BUSY.
        with patch.dict(os.environ, {"WORKER_COUNT": "2"}, clear=False):
            c = BitBrowserPoolClient(
                endpoint="http://127.0.0.1:54345",
                api_key="k",
                profile_ids=["a", "b"],
                acquire_timeout_s=0.5,
                poll_interval_s=0.05,
            )
        with c._lock:
            c._busy.update({"a", "b"})
        with self.assertRaises(RuntimeError) as ctx:
            c.acquire_profile()
        self.assertIn("BUSY", str(ctx.exception))
        self.assertIn("2 profiles", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
