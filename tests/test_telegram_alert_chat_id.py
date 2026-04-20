"""PR-4 T-G7 — Optional TELEGRAM_ALERT_CHAT_ID."""
import os
import unittest
from unittest.mock import patch

from modules.notification import telegram_notifier


class AlertChatIdTests(unittest.TestCase):
    def test_alert_uses_alert_chat_id_when_set(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "tkn",
            "TELEGRAM_CHAT_ID": "default-chat",
            "TELEGRAM_ALERT_CHAT_ID": "alert-chat",
        }
        with patch.dict(os.environ, env, clear=False):
            token, chat = telegram_notifier._credentials("alert")
        self.assertEqual(token, "tkn")
        self.assertEqual(chat, "alert-chat")

    def test_alert_falls_back_to_default_chat_id(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "tkn",
            "TELEGRAM_CHAT_ID": "default-chat",
            "TELEGRAM_ALERT_CHAT_ID": "",
        }
        with patch.dict(os.environ, env, clear=False):
            token, chat = telegram_notifier._credentials("alert")
        self.assertEqual(token, "tkn")
        self.assertEqual(chat, "default-chat")

    def test_default_channel_ignores_alert_chat_id(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "tkn",
            "TELEGRAM_CHAT_ID": "default-chat",
            "TELEGRAM_ALERT_CHAT_ID": "alert-chat",
        }
        with patch.dict(os.environ, env, clear=False):
            token, chat = telegram_notifier._credentials("default")
        self.assertEqual(chat, "default-chat")


if __name__ == "__main__":
    unittest.main()
