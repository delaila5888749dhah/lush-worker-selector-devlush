"""Tests for modules.observability.healthcheck (Ext-3)."""
import http.client
import json
import time
import unittest
from unittest.mock import MagicMock

from modules.observability import healthcheck
from modules.observability.healthcheck import (
    get_health,
    is_running,
    reset,
    start_server,
    stop_server,
)


def _make_status(running=True, state="RUNNING", worker_count=2,
                 consecutive_rollbacks=0, metrics=None):
    return {
        "running": running,
        "state": state,
        "worker_count": worker_count,
        "active_workers": [],
        "consecutive_rollbacks": consecutive_rollbacks,
        "trace_id": None,
        "metrics": metrics,
    }


class TestGetHealth(unittest.TestCase):
    def setUp(self):
        reset()

    def test_status_fn_none_returns_unknown(self):
        result = get_health(status_fn=None)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("status_fn not configured", result["errors"])

    def test_healthy_when_running_no_errors(self):
        fn = MagicMock(return_value=_make_status(running=True, consecutive_rollbacks=0,
                                                  metrics={"error_rate": 0.0}))
        result = get_health(fn)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["errors"], [])

    def test_degraded_when_not_running(self):
        fn = MagicMock(return_value=_make_status(running=False, state="STOPPED"))
        result = get_health(fn)
        self.assertEqual(result["status"], "degraded")
        self.assertGreater(len(result["errors"]), 0)

    def test_degraded_when_rollbacks_positive(self):
        fn = MagicMock(return_value=_make_status(running=True, consecutive_rollbacks=2))
        result = get_health(fn)
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(any("rollback" in e for e in result["errors"]))

    def test_degraded_when_error_rate_above_5pct(self):
        fn = MagicMock(return_value=_make_status(running=True, metrics={"error_rate": 0.10}))
        result = get_health(fn)
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(any("error_rate" in e for e in result["errors"]))

    def test_status_fn_exception_returns_unknown(self):
        fn = MagicMock(side_effect=RuntimeError("boom"))
        result = get_health(fn)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("boom", result["errors"][0])

    def test_return_keys_complete(self):
        fn = MagicMock(return_value=_make_status())
        result = get_health(fn)
        for key in ("status", "running", "state", "worker_count", "consecutive_rollbacks", "errors"):
            self.assertIn(key, result)


class TestHealthServer(unittest.TestCase):
    def setUp(self):
        stop_server()
        reset()

    def tearDown(self):
        stop_server()
        reset()

    def _actual_port(self):
        with healthcheck._lock:
            return healthcheck._server_instance.server_address[1]

    def test_start_returns_true(self):
        result = start_server(port=0)
        self.assertTrue(result)

    def test_start_twice_returns_false(self):
        start_server(port=0)
        result = start_server(port=0)
        self.assertFalse(result)

    def test_stop_when_not_running_returns_false(self):
        result = stop_server()
        self.assertFalse(result)

    def test_health_endpoint_returns_200(self):
        start_server(port=0)
        time.sleep(0.05)
        port = self._actual_port()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        data = json.loads(resp.read())
        self.assertIn("status", data)
        conn.close()

    def test_unknown_path_returns_404(self):
        start_server(port=0)
        time.sleep(0.05)
        port = self._actual_port()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/xyz")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        conn.close()

    def test_stop_server_stops_cleanly(self):
        start_server(port=0)
        time.sleep(0.05)
        result = stop_server()
        self.assertTrue(result)
        self.assertFalse(is_running())


class TestIsRunning(unittest.TestCase):
    def setUp(self):
        stop_server()
        reset()

    def tearDown(self):
        stop_server()
        reset()

    def test_not_running_initially(self):
        self.assertFalse(is_running())

    def test_running_after_start(self):
        start_server(port=0)
        time.sleep(0.05)
        self.assertTrue(is_running())

    def test_not_running_after_stop(self):
        start_server(port=0)
        time.sleep(0.05)
        stop_server()
        self.assertFalse(is_running())


class TestStopServerTimeout(unittest.TestCase):
    def setUp(self):
        stop_server()
        reset()

    def tearDown(self):
        stop_server()
        reset()

    def test_stop_timeout_preserves_refs(self):
        start_server(port=0)
        time.sleep(0.05)
        # timeout=0.0 guarantees join times out immediately
        result = stop_server(timeout=0.0)
        self.assertFalse(result)
        self.assertTrue(is_running())
        with healthcheck._lock:
            self.assertIsNotNone(healthcheck._server_instance)
        # Normal stop should now succeed
        result = stop_server()
        self.assertTrue(result)
        self.assertFalse(is_running())

    def test_reset_after_timeout_no_orphan(self):
        start_server(port=0)
        time.sleep(0.05)
        # timeout=0.0 preserves refs
        stop_server(timeout=0.0)
        reset()
        self.assertFalse(is_running())
        with healthcheck._lock:
            self.assertIsNone(healthcheck._server_thread)
            self.assertIsNone(healthcheck._server_instance)
            self.assertFalse(healthcheck._stopping)


if __name__ == "__main__":
    unittest.main()
