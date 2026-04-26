"""PR-06 tests: Watchdog uses CDP Network.getResponseBody as primary, DOM as fallback (F-05).

Covers:
  F-05 — _on_response tries Network.getResponseBody (primary); falls back to DOM on
          parse failure, empty body, or no recognised total key.

Test categories:
  CdpBodyPrimaryTests      — CDP body parsed successfully → notify_total gets body value,
                              DOM (execute_script) is NOT called.
  CdpBodyFallbackTests     — body fails/empty/no recognised key → DOM fallback + WARNING log.
  FirstNotifyWinsTests     — first-notify-wins is strictly maintained across both paths.
"""
# pylint: disable=not-callable  # captured[0] is dynamically assigned a callable
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from integration.orchestrator import (
    _network_listener_lock,
    _notified_workers_this_cycle,
    _notify_total_from_dom,
    _setup_network_total_listener,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

_WORKER = "pr06-worker"


def _make_driver_with_body(body_dict):
    """Return a MagicMock driver whose execute_cdp_cmd returns a CDP body response."""
    driver = MagicMock()
    # execute_cdp_cmd("Network.enable", {}) succeeds silently (default MagicMock).
    driver.execute_cdp_cmd.return_value = {"body": json.dumps(body_dict), "base64Encoded": False}
    return driver


def _capture_callback(driver):
    """Wire fake_add_listener onto driver; return list whose [0] will hold the callback."""
    captured = [None]

    def fake_add_listener(_event, callback):
        captured[0] = callback

    driver.add_cdp_listener = fake_add_listener
    return captured


def _matching_params(request_id="req-001", url="/checkout/total/amounts"):
    """Return a Network.responseReceived params dict with a matching URL and requestId."""
    return {"requestId": request_id, "response": {"url": url}}


def _clear_guard():
    with _network_listener_lock:
        _notified_workers_this_cycle.discard(_WORKER)


# ── Test classes ───────────────────────────────────────────────────────────────

class CdpBodyPrimaryTests(unittest.TestCase):
    """CDP Network.getResponseBody primary path: body is parsed, DOM is NOT read."""

    def setUp(self):
        _clear_guard()

    def tearDown(self):
        _clear_guard()

    @staticmethod
    def _fire(body_dict, request_id="req-001", url="/checkout/total/amounts"):
        """Set up listener, fire callback with CDP body, return mock watchdog."""
        driver = _make_driver_with_body(body_dict)
        captured = _capture_callback(driver)
        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, _WORKER)
            captured[0](_matching_params(request_id=request_id, url=url))
        # DOM execute_script must not have been called (body was parsed)
        driver.execute_script.assert_not_called()
        return mock_wd

    def test_total_key_notifies_correct_value(self):
        """'total' key in CDP body → notify_total with that value; DOM not called."""
        mock_wd = self._fire({"total": 49.99})
        mock_wd.notify_total.assert_called_once_with(_WORKER, 49.99)

    def test_order_total_key_notifies_correct_value(self):
        """'order_total' key in CDP body → notify_total with that value."""
        mock_wd = self._fire({"order_total": 25.50})
        mock_wd.notify_total.assert_called_once_with(_WORKER, 25.50)

    def test_order_total_camelcase_key_notifies_correct_value(self):
        """'orderTotal' camelCase key in CDP body → notify_total with that value."""
        mock_wd = self._fire({"orderTotal": 30.00})
        mock_wd.notify_total.assert_called_once_with(_WORKER, 30.00)

    def test_amount_key_notifies_correct_value(self):
        """'amount' key in CDP body → notify_total with that value."""
        mock_wd = self._fire({"amount": 15.00})
        mock_wd.notify_total.assert_called_once_with(_WORKER, 15.00)

    def test_total_key_takes_priority_over_amount(self):
        """'total' key is preferred over 'amount' when both present."""
        mock_wd = self._fire({"total": 55.00, "amount": 10.00})
        mock_wd.notify_total.assert_called_once_with(_WORKER, 55.00)

    @staticmethod
    def test_zero_total_is_valid():
        """A body total of 0.0 is finite and must be notified (not skipped as falsy)."""
        driver = _make_driver_with_body({"total": 0.0})
        captured = _capture_callback(driver)
        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, _WORKER)
            captured[0](_matching_params())
        driver.execute_script.assert_not_called()
        mock_wd.notify_total.assert_called_once_with(_WORKER, 0.0)

    def test_cws40_url_pattern_no_longer_triggers_cdp_body(self):
        """After P3-F2 fix (option A), 'cws4.0' substring alone must NOT trigger callback."""
        mock_wd = self._fire(
            {"total": 88.00},
            url="https://example.com/cws4.0/submit",
        )
        mock_wd.notify_total.assert_not_called()

    def test_api_checkout_url_pattern_uses_cdp_body(self):
        """/api/checkout URL triggers CDP body primary path."""
        mock_wd = self._fire({"total": 19.99}, url="/api/checkout/confirm")
        mock_wd.notify_total.assert_called_once_with(_WORKER, 19.99)


class CdpBodyFallbackTests(unittest.TestCase):
    """CDP body unavailable → DOM fallback, WARNING logged."""

    def setUp(self):
        _clear_guard()

    def tearDown(self):
        _clear_guard()

    @staticmethod
    def _fire_and_capture(driver, url="/checkout/total/amounts", request_id="req-002"):
        """Register listener, fire callback, return mock watchdog."""
        captured = _capture_callback(driver)
        driver.execute_script.return_value = "42.00"
        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, _WORKER)
            captured[0](_matching_params(request_id=request_id, url=url))
        return mock_wd, driver

    def test_cdp_getresponsebody_exception_falls_back_to_dom(self):
        """Network.getResponseBody raises → WARNING logged, DOM fallback called."""
        driver = MagicMock()
        # Network.enable succeeds; getResponseBody raises

        def cdp_side_effect(cmd, _params=None):
            if cmd == "Network.getResponseBody":
                raise RuntimeError("CDP body error")
            return MagicMock()

        driver.execute_cdp_cmd.side_effect = cdp_side_effect

        with self.assertLogs("integration.orchestrator", level="WARNING") as log_ctx:
            mock_wd, _ = self._fire_and_capture(driver)

        # DOM fallback must have been used
        driver.execute_script.assert_called_once()
        mock_wd.notify_total.assert_called_once_with(_WORKER, 42.0)
        # WARNING about falling back to DOM must appear
        self.assertTrue(
            any("falling back to DOM" in m or "DOM fallback" in m for m in log_ctx.output),
            f"Expected fallback WARNING in logs: {log_ctx.output}",
        )

    def test_cdp_empty_body_falls_back_to_dom(self):
        """Network.getResponseBody returns empty body → DOM fallback."""
        driver = MagicMock()

        def cdp_side_effect(cmd, _params=None):
            if cmd == "Network.getResponseBody":
                return {"body": "", "base64Encoded": False}
            return MagicMock()

        driver.execute_cdp_cmd.side_effect = cdp_side_effect

        with self.assertLogs("integration.orchestrator", level="WARNING") as log_ctx:
            mock_wd, _ = self._fire_and_capture(driver)

        driver.execute_script.assert_called_once()
        mock_wd.notify_total.assert_called_once_with(_WORKER, 42.0)
        self.assertTrue(
            any("DOM fallback" in m for m in log_ctx.output),
            f"Expected DOM fallback WARNING: {log_ctx.output}",
        )

    def test_cdp_non_dict_response_falls_back_to_dom(self):
        """Network.getResponseBody returns non-dict → DOM fallback."""
        driver = MagicMock()

        def cdp_side_effect(cmd, _params=None):
            if cmd == "Network.getResponseBody":
                return "not-a-dict"
            return MagicMock()

        driver.execute_cdp_cmd.side_effect = cdp_side_effect

        with self.assertLogs("integration.orchestrator", level="WARNING"):
            mock_wd, _ = self._fire_and_capture(driver)

        driver.execute_script.assert_called_once()
        mock_wd.notify_total.assert_called_once_with(_WORKER, 42.0)

    def test_cdp_body_no_recognised_key_falls_back_to_dom(self):
        """No recognised total key in body -> DOM fallback."""
        driver = MagicMock()

        def cdp_side_effect(cmd, _params=None):
            if cmd == "Network.getResponseBody":
                return {"body": json.dumps({"subtotal": 99.0, "tax": 5.0})}
            return MagicMock()

        driver.execute_cdp_cmd.side_effect = cdp_side_effect

        with self.assertLogs("integration.orchestrator", level="WARNING") as log_ctx:
            mock_wd, _ = self._fire_and_capture(driver)

        driver.execute_script.assert_called_once()
        mock_wd.notify_total.assert_called_once_with(_WORKER, 42.0)
        self.assertTrue(
            any("DOM fallback" in m for m in log_ctx.output),
        )

    def test_cdp_body_invalid_json_falls_back_to_dom(self):
        """Body is not valid JSON → parse error → DOM fallback with WARNING."""
        driver = MagicMock()

        def cdp_side_effect(cmd, _params=None):
            if cmd == "Network.getResponseBody":
                return {"body": "not-json-{{{"}
            return MagicMock()

        driver.execute_cdp_cmd.side_effect = cdp_side_effect

        with self.assertLogs("integration.orchestrator", level="WARNING") as log_ctx:
            mock_wd, _ = self._fire_and_capture(driver)

        driver.execute_script.assert_called_once()
        mock_wd.notify_total.assert_called_once_with(_WORKER, 42.0)
        self.assertTrue(
            any("falling back to DOM" in m or "DOM fallback" in m for m in log_ctx.output),
        )

    def test_no_request_id_falls_back_to_dom(self):
        """Params with no requestId → CDP body not attempted, DOM fallback with WARNING."""
        driver = MagicMock()
        driver.execute_script.return_value = "33.33"
        params_no_id = {"response": {"url": "/checkout/total/amounts"}}
        captured = _capture_callback(driver)

        with self.assertLogs("integration.orchestrator", level="WARNING") as log_ctx:
            with patch("integration.orchestrator.watchdog") as mock_wd:
                _setup_network_total_listener(driver, _WORKER)
                captured[0](params_no_id)

        # CDP getResponseBody must NOT have been called (no requestId)
        for call_args in driver.execute_cdp_cmd.call_args_list:
            self.assertNotEqual(call_args[0][0], "Network.getResponseBody")

        driver.execute_script.assert_called_once()
        mock_wd.notify_total.assert_called_once_with(_WORKER, 33.33)
        self.assertTrue(
            any("DOM fallback" in m for m in log_ctx.output),
        )

    @staticmethod
    def test_dom_fallback_not_called_for_non_matching_url():
        """Non-matching URL → neither CDP nor DOM reads are triggered."""
        driver = MagicMock()
        driver.execute_script.return_value = "99.99"
        captured = _capture_callback(driver)

        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, _WORKER)
            captured[0]({"requestId": "req-x", "response": {"url": "/unrelated/endpoint"}})

        driver.execute_script.assert_not_called()
        mock_wd.notify_total.assert_not_called()


class FirstNotifyWinsTests(unittest.TestCase):
    """first-notify-wins invariant is maintained across CDP body and DOM fallback paths."""

    def setUp(self):
        _clear_guard()

    def tearDown(self):
        _clear_guard()

    @staticmethod
    def test_cdp_body_wins_over_subsequent_dom_fallback():
        """CDP body notifies first; a subsequent DOM-fallback call for same worker is a no-op."""
        driver = _make_driver_with_body({"total": 50.00})
        driver.execute_script.return_value = "99.99"  # DOM would give different value
        captured = _capture_callback(driver)

        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, _WORKER)
            # Primary: CDP body fires
            captured[0](_matching_params())
            # Simulate a late DOM fallback call (first-notify-wins must block it)
            _notify_total_from_dom(driver, _WORKER)

        # notify_total must be called exactly once with the CDP body value
        mock_wd.notify_total.assert_called_once_with(_WORKER, 50.0)

    def test_dom_fallback_wins_when_cdp_body_fails_late_cdp_is_noop(self):
        """DOM fallback notifies when CDP body fails; a later matching-URL callback is no-op."""
        driver = MagicMock()
        driver.execute_script.return_value = "75.00"

        def cdp_side_effect(cmd, _params=None):
            if cmd == "Network.getResponseBody":
                raise RuntimeError("body unavailable")
            return MagicMock()

        driver.execute_cdp_cmd.side_effect = cdp_side_effect
        captured = _capture_callback(driver)

        with patch("integration.orchestrator.watchdog") as mock_wd:
            with self.assertLogs("integration.orchestrator", level="WARNING"):
                _setup_network_total_listener(driver, _WORKER)
                # First callback: CDP fails → DOM fallback notifies 75.00
                captured[0](_matching_params(request_id="req-A"))

            # Now restore a working CDP body for the second callback
            driver.execute_cdp_cmd.side_effect = None
            driver.execute_cdp_cmd.return_value = {
                "body": json.dumps({"total": 200.00}),
                "base64Encoded": False,
            }
            # Second callback for same worker: first-notify-wins must block it
            captured[0](_matching_params(request_id="req-B"))

        # Exactly one notification with the DOM value (75.00 from first callback)
        mock_wd.notify_total.assert_called_once_with(_WORKER, 75.0)

    def test_concurrent_cdp_body_callbacks_notify_at_most_once(self):
        """Concurrent CDP body callbacks for the same worker notify watchdog at most once."""
        notify_count = [0]
        barrier = threading.Barrier(3)

        driver = _make_driver_with_body({"total": 60.00})
        captured = _capture_callback(driver)

        def _fake_notify(_wid, _val):
            notify_count[0] += 1

        with patch("integration.orchestrator.watchdog") as mock_wd:
            mock_wd.notify_total.side_effect = _fake_notify
            _setup_network_total_listener(driver, _WORKER)

            def _fire():
                barrier.wait(timeout=5)
                captured[0](_matching_params())

            threads = [threading.Thread(target=_fire) for _ in range(2)]
            for thread in threads:
                thread.start()
            # Main thread is the 3rd barrier party; all 3 rendezvous so both
            # worker threads fire the callback at the same time.
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(notify_count[0], 1, "notify_total must be called exactly once")
