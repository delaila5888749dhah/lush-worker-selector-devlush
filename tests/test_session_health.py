import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.cdp.session_health import classify_session_loss, session_alive


class SessionHealthTests(unittest.TestCase):
    def test_classify_session_loss_all_patterns(self):
        cases = {
            "invalid session id: session deleted": "invalid_session_id",
            "session deleted as the browser has closed the connection": "browser_connection_closed",
            "disconnected: not connected to DevTools": "devtools_disconnected",
            "target frame detached": "target_frame_detached",
            "chrome-error://chromewebdata/": "gateway_connection_closed",
            "net::ERR_CONNECTION_CLOSED": "gateway_connection_closed",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_session_loss(text), expected)
        self.assertIsNone(classify_session_loss("ordinary webdriver timeout"))

    def test_session_alive_returns_false_on_any_exception(self):
        driver = SimpleNamespace()
        driver.execute_cdp_cmd = MagicMock()
        driver.execute_cdp_cmd.side_effect = RuntimeError("unknown failure")

        self.assertFalse(session_alive(driver))

    def test_session_alive_supports_givex_wrapper(self):
        raw = MagicMock()
        wrapper = MagicMock()
        wrapper._driver = raw

        self.assertTrue(session_alive(wrapper))
        raw.execute_cdp_cmd.assert_called_once_with(
            "Runtime.evaluate",
            {"expression": "1", "returnByValue": True},
        )


if __name__ == "__main__":
    unittest.main()
