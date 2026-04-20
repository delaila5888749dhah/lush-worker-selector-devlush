"""Unit tests for P1-4: CDP exception detection, logging, and typed errors.

Covers:
- _safe_cdp_cmd(): connect errors (OSError/ConnectionError/TimeoutError) wrapped
  as SessionFlaggedError; command-level errors wrapped as CDPCommandError.
- PII redaction: card numbers, CVV, and email are never exposed in logged/raised
  exception messages.
- GivexDriver.cdp_click_absolute(): propagates SessionFlaggedError and
  CDPCommandError from _safe_cdp_cmd on the underlying execute_cdp_cmd call.
- Log output verification: structured error fields (cmd=, detail=) appear in logs.
"""

import unittest
from unittest.mock import MagicMock

from modules.cdp.driver import (
    GivexDriver,
    _safe_cdp_cmd,
)
from modules.common.exceptions import (
    CDPCommandError,
    SessionFlaggedError,
)


class SafeCdpCmdConnectErrorTests(unittest.TestCase):
    """_safe_cdp_cmd wraps transport/connection failures as SessionFlaggedError."""

    def _make_driver(self, side_effect):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = side_effect
        return driver

    def test_oserror_raises_session_flagged(self):
        driver = self._make_driver(OSError("Connection refused"))
        with self.assertRaises(SessionFlaggedError):
            _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {"type": "mousePressed"})

    def test_connection_error_raises_session_flagged(self):
        driver = self._make_driver(ConnectionError("Broken pipe"))
        with self.assertRaises(SessionFlaggedError):
            _safe_cdp_cmd(driver, "Network.enable", {})

    def test_timeout_error_raises_session_flagged(self):
        driver = self._make_driver(TimeoutError("CDP call timed out"))
        with self.assertRaises(SessionFlaggedError):
            _safe_cdp_cmd(driver, "Input.dispatchKeyEvent", {"type": "keyDown"})

    def test_session_flagged_message_contains_command(self):
        driver = self._make_driver(OSError("econnreset"))
        with self.assertRaises(SessionFlaggedError) as ctx:
            _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})
        self.assertIn("Input.dispatchMouseEvent", str(ctx.exception))

    def test_session_flagged_chained_from_original(self):
        cause = OSError("econnreset")
        driver = self._make_driver(cause)
        with self.assertRaises(SessionFlaggedError) as ctx:
            _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})
        self.assertIs(ctx.exception.__cause__, cause)


class SafeCdpCmdCommandErrorTests(unittest.TestCase):
    """_safe_cdp_cmd wraps non-retryable command failures as CDPCommandError."""

    def _make_driver(self, side_effect):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = side_effect
        return driver

    def test_generic_exception_raises_cdp_command_error(self):
        driver = self._make_driver(RuntimeError("Unknown CDP method"))
        with self.assertRaises(CDPCommandError):
            _safe_cdp_cmd(driver, "BadDomain.badMethod", {})

    def test_cdp_command_error_stores_command_name(self):
        driver = self._make_driver(ValueError("invalid params"))
        with self.assertRaises(CDPCommandError) as ctx:
            _safe_cdp_cmd(driver, "Input.dispatchKeyEvent", {"type": "keyDown"})
        self.assertEqual(ctx.exception.command, "Input.dispatchKeyEvent")

    def test_cdp_command_error_stores_sanitized_detail(self):
        driver = self._make_driver(RuntimeError("Error for card 4111111111111111"))
        with self.assertRaises(CDPCommandError) as ctx:
            _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})
        self.assertNotIn("4111111111111111", ctx.exception.detail)
        self.assertIn("[REDACTED-CARD]", ctx.exception.detail)

    def test_cdp_command_error_chained_from_original(self):
        cause = RuntimeError("cdp error")
        driver = self._make_driver(cause)
        with self.assertRaises(CDPCommandError) as ctx:
            _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})
        self.assertIs(ctx.exception.__cause__, cause)

    def test_cdp_command_error_is_session_flagged_subclass(self):
        driver = self._make_driver(RuntimeError("unknown"))
        with self.assertRaises(SessionFlaggedError):
            _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})

    def test_success_returns_driver_result(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.return_value = {"result": "ok"}
        result = _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {"type": "mousePressed"})
        self.assertEqual(result, {"result": "ok"})


class SafeCdpCmdPiiRedactionTests(unittest.TestCase):
    """PII data never appears in CDPCommandError.detail or log messages."""

    def _make_driver(self, msg):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = RuntimeError(msg)
        return driver

    def test_card_number_redacted_in_detail(self):
        driver = self._make_driver("Failed for 5500005555555559")
        with self.assertRaises(CDPCommandError) as ctx:
            _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})
        self.assertNotIn("5500005555555559", ctx.exception.detail)

    def test_email_redacted_in_detail(self):
        driver = self._make_driver("user@domain.com sent bad command")
        with self.assertRaises(CDPCommandError) as ctx:
            _safe_cdp_cmd(driver, "Network.enable", {})
        self.assertNotIn("user@domain.com", ctx.exception.detail)

    def test_cvv_redacted_in_detail(self):
        driver = self._make_driver("cvv=987 was rejected")
        with self.assertRaises(CDPCommandError) as ctx:
            _safe_cdp_cmd(driver, "Input.dispatchKeyEvent", {})
        self.assertNotIn("cvv=987", ctx.exception.detail)

    def test_card_number_redacted_in_connect_error_log(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = OSError("card 4111111111111111 timeout")
        with self.assertLogs("modules.cdp.driver", level="ERROR") as cm:
            with self.assertRaises(SessionFlaggedError):
                _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})
        combined = " ".join(cm.output)
        self.assertNotIn("4111111111111111", combined)
        self.assertIn("[REDACTED-CARD]", combined)


class SafeCdpCmdLoggingTests(unittest.TestCase):
    """Structured log fields (cmd=, detail=) appear on error."""

    def test_connect_error_logs_command_name(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = OSError("refused")
        with self.assertLogs("modules.cdp.driver", level="ERROR") as cm:
            with self.assertRaises(SessionFlaggedError):
                _safe_cdp_cmd(driver, "Input.dispatchMouseEvent", {})
        self.assertTrue(
            any("Input.dispatchMouseEvent" in line for line in cm.output),
            "command name must appear in log",
        )

    def test_command_error_logs_command_name(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = RuntimeError("bad method")
        with self.assertLogs("modules.cdp.driver", level="ERROR") as cm:
            with self.assertRaises(CDPCommandError):
                _safe_cdp_cmd(driver, "BadDomain.badMethod", {})
        self.assertTrue(
            any("BadDomain.badMethod" in line for line in cm.output),
            "command name must appear in log",
        )

    def test_connect_error_log_prefix(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = ConnectionError("pipe broken")
        with self.assertLogs("modules.cdp.driver", level="ERROR") as cm:
            with self.assertRaises(SessionFlaggedError):
                _safe_cdp_cmd(driver, "Network.enable", {})
        self.assertTrue(
            any("cdp_connect_error" in line for line in cm.output),
        )

    def test_command_error_log_prefix(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = ValueError("bad param")
        with self.assertLogs("modules.cdp.driver", level="ERROR") as cm:
            with self.assertRaises(CDPCommandError):
                _safe_cdp_cmd(driver, "Input.dispatchKeyEvent", {})
        self.assertTrue(
            any("cdp_command_error" in line for line in cm.output),
        )


class CdpClickAbsoluteExceptionTests(unittest.TestCase):
    """GivexDriver.cdp_click_absolute() propagates CDP exceptions correctly."""

    def setUp(self):
        self.raw_driver = MagicMock()
        self.gd = GivexDriver(self.raw_driver)

    def test_connect_error_raises_session_flagged(self):
        self.raw_driver.execute_cdp_cmd.side_effect = OSError("refused")
        with self.assertRaises(SessionFlaggedError):
            self.gd.cdp_click_absolute(100.0, 200.0)

    def test_timeout_error_raises_session_flagged(self):
        self.raw_driver.execute_cdp_cmd.side_effect = TimeoutError("timed out")
        with self.assertRaises(SessionFlaggedError):
            self.gd.cdp_click_absolute(50.0, 75.0)

    def test_command_error_raises_cdp_command_error(self):
        self.raw_driver.execute_cdp_cmd.side_effect = RuntimeError("cdp failure")
        with self.assertRaises(CDPCommandError):
            self.gd.cdp_click_absolute(10.0, 20.0)

    def test_success_dispatches_three_events(self):
        self.raw_driver.execute_cdp_cmd.return_value = None
        self.gd.cdp_click_absolute(100.0, 200.0)
        self.assertEqual(self.raw_driver.execute_cdp_cmd.call_count, 3)
        event_types = [
            call.args[1]["type"]
            for call in self.raw_driver.execute_cdp_cmd.call_args_list
        ]
        self.assertEqual(event_types, ["mouseMoved", "mousePressed", "mouseReleased"])

    def test_error_on_first_event_stops_remaining(self):
        self.raw_driver.execute_cdp_cmd.side_effect = OSError("refused")
        with self.assertRaises(SessionFlaggedError):
            self.gd.cdp_click_absolute(5.0, 5.0)
        self.assertEqual(self.raw_driver.execute_cdp_cmd.call_count, 1)


class CDPCommandErrorClassTests(unittest.TestCase):
    """CDPCommandError structure and inheritance."""

    def test_is_session_flagged_subclass(self):
        exc = CDPCommandError("Input.dispatchMouseEvent", "failed")
        self.assertIsInstance(exc, SessionFlaggedError)

    def test_command_attribute(self):
        exc = CDPCommandError("Network.enable", "refused")
        self.assertEqual(exc.command, "Network.enable")

    def test_detail_attribute(self):
        exc = CDPCommandError("Input.dispatchKeyEvent", "bad params")
        self.assertEqual(exc.detail, "bad params")

    def test_str_contains_command_and_detail(self):
        exc = CDPCommandError("Input.dispatchMouseEvent", "connection reset")
        msg = str(exc)
        self.assertIn("Input.dispatchMouseEvent", msg)
        self.assertIn("connection reset", msg)


if __name__ == "__main__":
    unittest.main()
