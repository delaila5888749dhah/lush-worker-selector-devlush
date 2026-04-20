"""PR-4 T-G4 — Retry + JSONL persistence tests."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from modules.notification import telegram_notifier


class RetryTests(unittest.TestCase):
    def setUp(self):
        telegram_notifier._reset_for_tests()
        self._tmp = tempfile.mkdtemp(prefix="tg-pending-")
        self._pending = os.path.join(self._tmp, "pending.jsonl")
        self._env_patch = patch.dict(
            os.environ, {"TELEGRAM_PENDING_FILE": self._pending}, clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        telegram_notifier._reset_for_tests()
        if os.path.exists(self._pending):
            os.remove(self._pending)
        os.rmdir(self._tmp)

    def test_retry_3_times_with_backoff_1_2_4(self):
        """On persistent failure, _send_with_retry performs exactly 3 POSTs."""
        call_times: list[float] = []

        def fail(url, data, headers=None, timeout=10):
            call_times.append(time.monotonic())
            return False

        payload = {"url": "https://x/sendMessage", "data": b"x", "headers": None}
        with patch.object(telegram_notifier, "_post", side_effect=fail), \
             patch.object(telegram_notifier, "_BACKOFFS", (0.05, 0.1, 0.2)):
            ok = telegram_notifier._send_with_retry(payload)
        self.assertFalse(ok)
        self.assertEqual(len(call_times), 3)

    def test_persists_to_jsonl_after_3_failures(self):
        """Final failure appends a JSONL record to the pending file."""
        payload = {"url": "https://x/sendMessage", "data": b"xyz", "headers": None}
        with patch.object(telegram_notifier, "_post", return_value=False), \
             patch.object(telegram_notifier, "_BACKOFFS", (0.0, 0.0, 0.0)):
            ok = telegram_notifier._send_with_retry(payload)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(self._pending))
        with open(self._pending, "r", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["payload"]["url"], "https://x/sendMessage")
        self.assertIn("ts", rec)

    def test_success_on_2nd_attempt_no_persist(self):
        """Success on the 2nd try must NOT persist."""
        attempts = []

        def maybe(url, data, headers=None, timeout=10):
            attempts.append(1)
            return len(attempts) >= 2  # fail once, succeed on attempt 2

        payload = {"url": "https://x/sendMessage", "data": b"y", "headers": None}
        with patch.object(telegram_notifier, "_post", side_effect=maybe), \
             patch.object(telegram_notifier, "_BACKOFFS", (0.0, 0.0, 0.0)):
            ok = telegram_notifier._send_with_retry(payload)
        self.assertTrue(ok)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(os.path.exists(self._pending))


if __name__ == "__main__":
    unittest.main()
