"""Regression tests for process-global BitBrowser pool client sharing."""

import os
import threading
import unittest
from unittest.mock import patch

from modules.cdp.fingerprint import (
    _BITBROWSER_POOL_CLIENTS,
    _BITBROWSER_POOL_CLIENTS_LOCK,
    BitBrowserPoolClient,
    get_bitbrowser_client,
)


class TestBitBrowserPoolSingleton(unittest.TestCase):
    def setUp(self):
        with _BITBROWSER_POOL_CLIENTS_LOCK:
            _BITBROWSER_POOL_CLIENTS.clear()

    def tearDown(self):
        with _BITBROWSER_POOL_CLIENTS_LOCK:
            _BITBROWSER_POOL_CLIENTS.clear()

    def _pool_env(self, endpoint="http://127.0.0.1:54346", ids="p1,p2"):
        return {
            "BITBROWSER_API_KEY": "k",
            "BITBROWSER_ENDPOINT": endpoint,
            "BITBROWSER_POOL_MODE": "1",
            "BITBROWSER_PROFILE_IDS": ids,
        }

    def test_pool_mode_returns_process_global_singleton(self):
        with patch.dict(os.environ, self._pool_env(), clear=False):
            first = get_bitbrowser_client()
            second = get_bitbrowser_client()

        self.assertIsInstance(first, BitBrowserPoolClient)
        self.assertIs(first, second)

    def test_profile_id_order_does_not_create_separate_pool(self):
        endpoint = "http://127.0.0.1:54347"
        with patch.dict(
            os.environ,
            self._pool_env(endpoint=endpoint, ids="p1,p2"),
            clear=False,
        ):
            first = get_bitbrowser_client()
        with patch.dict(
            os.environ,
            self._pool_env(endpoint=endpoint, ids="p2,p1,p1"),
            clear=False,
        ):
            second = get_bitbrowser_client()

        self.assertIs(first, second)

    def test_concurrent_threads_share_client_and_acquire_different_profiles(self):
        with patch.dict(
            os.environ,
            self._pool_env(endpoint="http://127.0.0.1:54348", ids="p1,p2"),
            clear=False,
        ):
            clients = [get_bitbrowser_client() for _ in range(2)]

        acquired = []
        acquired_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(client):
            barrier.wait()
            pid = client.acquire_profile()
            with acquired_lock:
                acquired.append(pid)

        threads = [
            threading.Thread(target=worker, args=(client,))
            for client in clients
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertIs(clients[0], clients[1])
        self.assertEqual(len(acquired), 2)
        self.assertEqual(len(acquired), len(set(acquired)))

        with patch.object(BitBrowserPoolClient, "_close_browser"):
            for pid in acquired:
                clients[0].release_profile(pid)


if __name__ == "__main__":
    unittest.main()
