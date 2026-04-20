"""PR-4 T-G3 — Background async sender tests."""
import time
import unittest
from unittest.mock import MagicMock, patch

from modules.notification import telegram_notifier


class _Task:
    class _Card:
        card_number = "4111111111111111"
    primary_card = _Card()
    recipient_email = "a@b.c"


_ENV = {
    "TELEGRAM_ENABLED": "1",
    "TELEGRAM_BOT_TOKEN": "tkn",
    "TELEGRAM_CHAT_ID": "42",
}


class AsyncSenderTests(unittest.TestCase):
    def setUp(self):
        telegram_notifier._reset_for_tests()

    def tearDown(self):
        telegram_notifier._reset_for_tests()

    def test_send_returns_immediately_no_block(self):
        """send_success_notification must not block on a slow _post."""
        slow = MagicMock(side_effect=lambda *a, **kw: (time.sleep(2.0) or True))
        with patch.dict("os.environ", _ENV, clear=False), \
             patch.object(telegram_notifier, "_post", slow):
            start = time.monotonic()
            ok = telegram_notifier.send_success_notification(
                "w1", _Task(), 5.0, None,
            )
            elapsed = time.monotonic() - start
        self.assertTrue(ok)
        # Enqueue + thread start should be well under 500ms even on slow CI.
        self.assertLess(elapsed, 0.5)

    def test_sender_thread_processes_queue(self):
        """Enqueued messages are processed by the background sender."""
        calls = []

        def fake_post(url, data, headers=None, timeout=10):
            calls.append(url)
            return True

        with patch.dict("os.environ", _ENV, clear=False), \
             patch.object(telegram_notifier, "_post", side_effect=fake_post):
            for i in range(3):
                telegram_notifier.send_success_notification(
                    f"w{i}", _Task(), 5.0, None,
                )
            telegram_notifier._flush_for_tests(timeout=3.0)
        self.assertEqual(len(calls), 3)
        for url in calls:
            self.assertIn("/sendMessage", url)

    def test_queue_full_drops_with_warning(self):
        """Full queue must drop with a warning, never block the caller."""
        import queue as _queue  # noqa: PLC0415

        tiny = _queue.Queue(maxsize=2)
        with patch.dict("os.environ", _ENV, clear=False), \
             patch.object(telegram_notifier, "_TG_QUEUE", tiny), \
             patch.object(telegram_notifier, "start_telegram_sender"):
            ok1 = telegram_notifier.send_success_notification(
                "w1", _Task(), 1.0, None,
            )
            ok2 = telegram_notifier.send_success_notification(
                "w2", _Task(), 1.0, None,
            )
            with self.assertLogs(
                "modules.notification.telegram_notifier", level="WARNING"
            ) as cm:
                ok3 = telegram_notifier.send_success_notification(
                    "w3", _Task(), 1.0, None,
                )
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertFalse(ok3)
        self.assertTrue(any("queue full" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
