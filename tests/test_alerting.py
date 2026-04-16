"""Tests for modules.observability.alerting (Ext-2)."""
import unittest
import unittest.mock as mock
from modules.observability import alerting

_NORMAL_METRICS = {
    "success_count": 90,
    "error_count": 4,
    "success_rate": 0.90,
    "error_rate": 0.04,
    "memory_usage_bytes": 2048,
    "restarts_last_hour": 1,
    "baseline_success_rate": 0.92,
}


class TestEvaluateAlerts(unittest.TestCase):
    def setUp(self):
        alerting.reset()

    def test_no_alerts_normal_metrics(self):
        """All metrics within thresholds → empty list."""
        result = alerting.evaluate_alerts(_NORMAL_METRICS)
        self.assertEqual(result, [])

    def test_error_rate_above_threshold(self):
        """error_rate > 0.05 → alert returned."""
        metrics = {**_NORMAL_METRICS, "error_rate": 0.06}
        result = alerting.evaluate_alerts(metrics)
        self.assertEqual(len(result), 1)
        self.assertIn("error_rate=6.0%", result[0])
        self.assertIn("threshold 5%", result[0])

    def test_error_rate_at_threshold_no_alert(self):
        """error_rate == 0.05 → no alert (threshold is strictly >)."""
        metrics = {**_NORMAL_METRICS, "error_rate": 0.05}
        result = alerting.evaluate_alerts(metrics)
        self.assertEqual(result, [])

    def test_restarts_above_threshold(self):
        """restarts_last_hour > 3 → alert returned."""
        metrics = {**_NORMAL_METRICS, "restarts_last_hour": 4}
        result = alerting.evaluate_alerts(metrics)
        self.assertEqual(len(result), 1)
        self.assertIn("restarts_last_hour=4", result[0])
        self.assertIn("threshold 3", result[0])

    def test_restarts_at_threshold_no_alert(self):
        """restarts_last_hour == 3 → no alert (threshold is strictly >)."""
        metrics = {**_NORMAL_METRICS, "restarts_last_hour": 3}
        result = alerting.evaluate_alerts(metrics)
        self.assertEqual(result, [])

    def test_success_rate_drop_above_threshold(self):
        """success_rate dropped > 10% from baseline → alert returned."""
        metrics = {**_NORMAL_METRICS, "success_rate": 0.80, "baseline_success_rate": 0.92}
        result = alerting.evaluate_alerts(metrics)
        self.assertEqual(len(result), 1)
        self.assertIn("success_rate dropped", result[0])
        self.assertIn("baseline 92.0%", result[0])

    def test_success_rate_drop_no_alert_within_threshold(self):
        """success_rate dropped only 8% (< 10%) → no alert."""
        metrics = {**_NORMAL_METRICS, "success_rate": 0.84, "baseline_success_rate": 0.92}
        result = alerting.evaluate_alerts(metrics)
        self.assertEqual(result, [])

    def test_success_rate_drop_at_exact_boundary_no_alert(self):
        """success_rate exactly at baseline - 0.10 → no alert (threshold is strictly <)."""
        metrics = {**_NORMAL_METRICS, "success_rate": 0.80, "baseline_success_rate": 0.90}
        result = alerting.evaluate_alerts(metrics)
        alerts_about_success = [a for a in result if "success_rate" in a]
        self.assertEqual(alerts_about_success, [])

    def test_baseline_none_no_success_drop_alert(self):
        """baseline_success_rate=None → success drop alert not triggered."""
        metrics = {**_NORMAL_METRICS, "success_rate": 0.50, "baseline_success_rate": None}
        result = alerting.evaluate_alerts(metrics)
        self.assertEqual(result, [])

    def test_missing_keys_no_raise(self):
        """Metrics dict with missing keys must not raise."""
        try:
            result = alerting.evaluate_alerts({})
            self.assertIsInstance(result, list)
        except Exception as exc:
            self.fail(f"evaluate_alerts raised an exception with empty dict: {exc}")

    def test_multiple_alerts_returned(self):
        """Multiple thresholds exceeded → all 3 alerts returned."""
        metrics = {
            "error_rate": 0.10,
            "restarts_last_hour": 5,
            "success_rate": 0.50,
            "baseline_success_rate": 0.90,
        }
        result = alerting.evaluate_alerts(metrics)
        error_rate_alerts = [a for a in result if "error_rate" in a]
        restart_alerts = [a for a in result if "restarts_last_hour" in a]
        success_alerts = [a for a in result if "success_rate" in a]
        self.assertEqual(len(error_rate_alerts), 1)
        self.assertEqual(len(restart_alerts), 1)
        self.assertEqual(len(success_alerts), 1)


class TestSendAlert(unittest.TestCase):
    def setUp(self):
        alerting.reset()

    def test_send_alert_log_backend_enabled(self):
        """send_alert emits WARNING log when log backend is enabled."""
        with self.assertLogs("modules.observability.alerting", level="WARNING") as cm:
            alerting.send_alert("test alert message")
        self.assertTrue(any("ALERT: test alert message" in line for line in cm.output))

    def test_send_alert_log_backend_disabled(self):
        """send_alert does NOT emit WARNING log when log backend is disabled."""
        from modules.observability import alerting as _alerting_mod
        alerting.set_log_alert_enabled(False)
        with mock.patch.object(_alerting_mod._logger, "warning") as mock_warn:
            alerting.send_alert("silent alert")
            # Verify no WARNING call was made for "ALERT: ..." pattern
            alert_calls = [c for c in mock_warn.call_args_list
                           if c.args and "ALERT" in str(c.args[0])]
            self.assertEqual(alert_calls, [])

    def test_send_alert_calls_custom_handler(self):
        """Custom handler receives the alert message."""
        received = []
        alerting.register_alert_handler(lambda msg: received.append(msg))
        alerting.send_alert("custom handler test")
        self.assertEqual(received, ["custom handler test"])

    def test_custom_handler_exception_does_not_propagate(self):
        """Exception raised by a custom handler must not propagate."""
        def bad_handler(msg):
            raise RuntimeError("handler failure")

        alerting.register_alert_handler(bad_handler)
        try:
            alerting.send_alert("trigger bad handler")
        except Exception as exc:
            self.fail(f"send_alert propagated exception from bad handler: {exc}")

    def test_custom_handler_exception_logged_as_warning(self):
        """Exception from a custom handler must be logged as a WARNING."""
        def bad_handler(msg):
            raise RuntimeError("handler failure")

        alerting.register_alert_handler(bad_handler)
        with self.assertLogs("modules.observability.alerting", level="WARNING") as cm:
            alerting.send_alert("trigger bad handler")
        self.assertTrue(any("handler" in line and "handler failure" in line for line in cm.output))

    def test_handler_failure_count_increments(self):
        """handler_failure_count in get_status() increments on each handler exception."""
        def bad_handler(msg):
            raise RuntimeError("fail")

        alerting.register_alert_handler(bad_handler)
        alerting.send_alert("msg1")
        alerting.send_alert("msg2")
        status = alerting.get_status()
        self.assertEqual(status["handler_failure_count"], 2)

    def test_handler_failure_count_zero_on_success(self):
        """handler_failure_count remains zero when handlers succeed."""
        alerting.register_alert_handler(lambda msg: None)
        alerting.send_alert("ok")
        self.assertEqual(alerting.get_status()["handler_failure_count"], 0)

    def test_alert_count_increments(self):
        """get_status returns incremented alert_count after send_alert calls."""
        alerting.send_alert("msg1")
        alerting.send_alert("msg2")
        status = alerting.get_status()
        self.assertEqual(status["alert_count"], 2)


class TestAlertingRegistry(unittest.TestCase):
    def setUp(self):
        alerting.reset()

    def test_register_and_unregister_handler(self):
        """register/unregister round-trip works and returns True on removal."""
        fn = lambda msg: None
        alerting.register_alert_handler(fn)
        self.assertEqual(alerting.get_status()["handler_count"], 1)
        result = alerting.unregister_alert_handler(fn)
        self.assertTrue(result)
        self.assertEqual(alerting.get_status()["handler_count"], 0)

    def test_unregister_nonexistent_returns_false(self):
        """unregister_alert_handler returns False for unknown handler."""
        result = alerting.unregister_alert_handler(lambda msg: None)
        self.assertFalse(result)

    def test_get_status_initial(self):
        """get_status returns correct initial values."""
        status = alerting.get_status()
        self.assertEqual(status["handler_count"], 0)
        self.assertEqual(status["alert_count"], 0)
        self.assertEqual(status["handler_failure_count"], 0)
        self.assertTrue(status["log_alert_enabled"])

    def test_reset_clears_all_state(self):
        """reset() clears handlers, alert_count, handler_failure_count, and restores log_alert_enabled."""
        def bad_handler(msg):
            raise RuntimeError("fail")

        alerting.register_alert_handler(bad_handler)
        alerting.send_alert("msg")
        alerting.set_log_alert_enabled(False)
        alerting.reset()
        status = alerting.get_status()
        self.assertEqual(status["handler_count"], 0)
        self.assertEqual(status["alert_count"], 0)
        self.assertEqual(status["handler_failure_count"], 0)
        self.assertTrue(status["log_alert_enabled"])


if __name__ == "__main__":
    unittest.main()
