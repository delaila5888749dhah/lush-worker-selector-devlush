"""Tests for modules.observability.log_sink (Ext-4)."""
import unittest
from unittest.mock import patch

from modules.observability import log_sink

_SAMPLE_EVENT = {
    "ts": 1234567890.0,
    "source": "runtime",
    "level": "info",
    "event": "start",
    "data": {"worker_id": "w-1"},
}


class TestEmit(unittest.TestCase):
    def setUp(self):
        log_sink.reset()

    def test_emit_valid_event_emits_debug_log(self):
        with self.assertLogs("modules.observability.log_sink", level="DEBUG") as cm:
            log_sink.emit(_SAMPLE_EVENT)
        self.assertTrue(any("start" in line for line in cm.output))

    def test_emit_when_disabled_no_debug_log(self):
        log_sink.set_log_sink_enabled(False)
        with self.assertRaises(AssertionError):
            with self.assertLogs("modules.observability.log_sink", level="DEBUG"):
                log_sink.emit(_SAMPLE_EVENT)

    def test_emit_calls_custom_sink_with_event(self):
        received = []
        log_sink.register_sink(received.append)
        log_sink.emit(_SAMPLE_EVENT)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], _SAMPLE_EVENT)

    def test_emit_calls_multiple_sinks(self):
        calls_a = []
        calls_b = []
        log_sink.register_sink(calls_a.append)
        log_sink.register_sink(calls_b.append)
        log_sink.emit(_SAMPLE_EVENT)
        self.assertEqual(len(calls_a), 1)
        self.assertEqual(len(calls_b), 1)

    def test_emit_custom_sink_exception_does_not_propagate(self):
        def bad_sink(e):
            raise RuntimeError("boom")

        log_sink.register_sink(bad_sink)
        # Must not raise
        log_sink.emit(_SAMPLE_EVENT)

    def test_emit_count_increments_each_call(self):
        log_sink.emit(_SAMPLE_EVENT)
        log_sink.emit(_SAMPLE_EVENT)
        self.assertEqual(log_sink.get_status()["emit_count"], 2)

    def test_emit_empty_dict_does_not_raise(self):
        # Must not raise for an empty dict
        log_sink.emit({})
        self.assertEqual(log_sink.get_status()["emit_count"], 1)

    def test_emit_exception_in_entire_function_does_not_propagate(self):
        """Even if the lock raises, emit must not propagate exceptions."""
        with patch.object(log_sink, "_lock") as mock_lock:
            mock_lock.__enter__.side_effect = RuntimeError("lock error")
            mock_lock.__exit__.return_value = False
            # Must not raise
            log_sink.emit(_SAMPLE_EVENT)


class TestRegistration(unittest.TestCase):
    def setUp(self):
        log_sink.reset()

    def test_register_and_unregister_lifecycle(self):
        fn = lambda e: None
        log_sink.register_sink(fn)
        self.assertEqual(log_sink.get_status()["sink_count"], 1)
        result = log_sink.unregister_sink(fn)
        self.assertTrue(result)
        self.assertEqual(log_sink.get_status()["sink_count"], 0)

    def test_unregister_unknown_sink_returns_false(self):
        result = log_sink.unregister_sink(lambda e: None)
        self.assertFalse(result)

    def test_get_status_returns_correct_state(self):
        fn = lambda e: None
        log_sink.register_sink(fn)
        log_sink.emit(_SAMPLE_EVENT)
        log_sink.set_log_sink_enabled(False)
        status = log_sink.get_status()
        self.assertEqual(status["sink_count"], 1)
        self.assertEqual(status["emit_count"], 1)
        self.assertFalse(status["log_sink_enabled"])

    def test_reset_clears_all_state(self):
        log_sink.register_sink(lambda e: None)
        log_sink.emit(_SAMPLE_EVENT)
        log_sink.set_log_sink_enabled(False)
        log_sink.reset()
        status = log_sink.get_status()
        self.assertEqual(status["sink_count"], 0)
        self.assertEqual(status["emit_count"], 0)
        self.assertTrue(status["log_sink_enabled"])

    def test_custom_sink_exception_logged_as_warning(self):
        def bad_sink(e):
            raise ValueError("oops")

        log_sink.register_sink(bad_sink)
        with self.assertLogs("modules.observability.log_sink", level="WARNING") as cm:
            log_sink.emit(_SAMPLE_EVENT)
        self.assertTrue(any("WARNING" in line and "oops" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
