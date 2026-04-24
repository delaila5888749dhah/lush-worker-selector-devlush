"""Phase 6 Task 4 — ``request_pool_reload`` clears all caches.

Verifies that after calling ``request_pool_reload``:
  * the in-memory master pool is rebuilt from disk,
  * new profiles added to ``BILLING_POOL_DIR`` are picked up,
  * per-worker shuffled caches (``_WORKER_STATES``) are cleared so that each
    worker re-shuffles from the fresh master on next access.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from modules.billing import main as billing_main


_PROFILE_LINES = [
    "Alice|Smith|1 Main St|LA|CA|90210|5551110001|alice@example.com",
    "Bob|Jones|2 Oak Ave|NY|NY|10001|5551110002|bob@example.com",
    "Carol|Lee|3 Pine Rd|SF|CA|94103|5551110003|carol@example.com",
    "Dan|Kim|4 Elm St|SEA|WA|98101|5551110004|dan@example.com",
    "Eve|Park|5 Fir Ln|PDX|OR|97201|5551110005|eve@example.com",
]


class PoolReloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="billing_pool_test_")
        self._prev_env = os.environ.get("BILLING_POOL_DIR")
        os.environ["BILLING_POOL_DIR"] = self._tmp
        billing_main._reset_state()

    def tearDown(self):
        billing_main._reset_state()
        if self._prev_env is None:
            os.environ.pop("BILLING_POOL_DIR", None)
        else:
            os.environ["BILLING_POOL_DIR"] = self._prev_env
        # Best-effort cleanup.
        for name in os.listdir(self._tmp):
            os.unlink(os.path.join(self._tmp, name))
        os.rmdir(self._tmp)

    def _write_profiles(self, name: str, lines) -> None:
        with open(os.path.join(self._tmp, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_reload_picks_up_new_profiles_on_disk(self):
        self._write_profiles("seed.txt", _PROFILE_LINES[:3])
        size_before = billing_main.load_billing_pool()
        self.assertEqual(size_before, 3)

        # Add two more profiles in a new file.
        self._write_profiles("added.txt", _PROFILE_LINES[3:5])

        # Without reload, the cached size stays the same.
        self.assertEqual(len(billing_main._MASTER_POOL), 3)

        billing_main.request_pool_reload()

        self.assertTrue(billing_main._MASTER_POOL_LOADED)
        self.assertEqual(len(billing_main._MASTER_POOL), 5)

    def test_reload_clears_worker_states(self):
        self._write_profiles("seed.txt", _PROFILE_LINES[:3])
        billing_main.load_billing_pool()

        # Prime per-worker state cache.
        state = billing_main.get_worker_state("worker-1")
        self.assertEqual(len(state.profiles), 3)
        self.assertIn("worker-1", billing_main._WORKER_STATES)

        # Add more files and reload.
        self._write_profiles("added.txt", _PROFILE_LINES[3:5])
        billing_main.request_pool_reload()

        # _WORKER_STATES must have been cleared so next access rebuilds from
        # the fresh master pool.
        self.assertNotIn("worker-1", billing_main._WORKER_STATES)
        new_state = billing_main.get_worker_state("worker-1")
        self.assertEqual(len(new_state.profiles), 5)

    def test_reload_clears_legacy_deque(self):
        self._write_profiles("seed.txt", _PROFILE_LINES[:3])
        billing_main.load_billing_pool()
        # Touch legacy deque via select_profile (no worker_id).
        billing_main.select_profile(None)
        self.assertTrue(len(billing_main._profiles) > 0)

        self._write_profiles("added.txt", _PROFILE_LINES[3:5])
        billing_main.request_pool_reload()
        # After reload, legacy deque has been rebuilt via eager load.
        self.assertEqual(len(billing_main._profiles), 5)


if __name__ == "__main__":
    unittest.main()
