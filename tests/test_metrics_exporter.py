"""Tests for modules.observability.metrics_exporter (Ext-1)."""
import unittest
from modules.observability import metrics_exporter

_SAMPLE = {
    "success_count": 10,
    "error_count": 2,
    "success_rate": 0.833,
    "error_rate": 0.167,
    "memory_usage_bytes": 1024,
    "restarts_last_hour": 0,
    "baseline_success_rate": None,
}


class TestExportMetrics(unittest.TestCase):
    def setUp(self):
        metrics_exporter.reset()

    def test_default_log_export_emits_debug_log(self):
        with self.assertLogs("modules.observability.metrics_exporter", level="DEBUG") as cm:
            metrics_exporter.export_metrics(_SAMPLE)
        self.assertTrue(any("metrics_export" in line for line in cm.output))

    def test_custom_exporter_called_with_metrics(self):
        received = []
        metrics_exporter.register_exporter(received.append)
        metrics_exporter.export_metrics(_SAMPLE)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], _SAMPLE)

    def test_multiple_exporters_all_called(self):
        calls_a = []
        calls_b = []
        metrics_exporter.register_exporter(calls_a.append)
        metrics_exporter.register_exporter(calls_b.append)
        metrics_exporter.export_metrics(_SAMPLE)
        self.assertEqual(len(calls_a), 1)
        self.assertEqual(len(calls_b), 1)

    def test_exporter_exception_does_not_propagate(self):
        def bad_fn(m):
            raise RuntimeError("boom")

        metrics_exporter.register_exporter(bad_fn)
        # Must not raise
        metrics_exporter.export_metrics(_SAMPLE)

    def test_exporter_exception_logged_as_warning(self):
        def bad_fn(m):
            raise ValueError("oops")

        metrics_exporter.register_exporter(bad_fn)
        with self.assertLogs("modules.observability.metrics_exporter", level="WARNING") as cm:
            metrics_exporter.export_metrics(_SAMPLE)
        self.assertTrue(any("WARNING" in line and "oops" in line for line in cm.output))

    def test_export_count_increments(self):
        metrics_exporter.export_metrics(_SAMPLE)
        metrics_exporter.export_metrics(_SAMPLE)
        self.assertEqual(metrics_exporter.get_status()["export_count"], 2)

    def test_log_export_disabled(self):
        metrics_exporter.set_log_export_enabled(False)
        with self.assertRaises(AssertionError):
            with self.assertLogs("modules.observability.metrics_exporter", level="DEBUG") as cm:
                metrics_exporter.export_metrics(_SAMPLE)

    def test_empty_metrics_dict_accepted(self):
        # Must not raise for an empty dict
        metrics_exporter.export_metrics({})
        self.assertEqual(metrics_exporter.get_status()["export_count"], 1)


class TestExporterRegistry(unittest.TestCase):
    def setUp(self):
        metrics_exporter.reset()

    def test_register_exporter_appears_in_status(self):
        metrics_exporter.register_exporter(lambda m: None)
        self.assertEqual(metrics_exporter.get_status()["exporter_count"], 1)

    def test_unregister_exporter_returns_true(self):
        fn = lambda m: None
        metrics_exporter.register_exporter(fn)
        result = metrics_exporter.unregister_exporter(fn)
        self.assertTrue(result)
        self.assertEqual(metrics_exporter.get_status()["exporter_count"], 0)

    def test_unregister_unknown_returns_false(self):
        result = metrics_exporter.unregister_exporter(lambda m: None)
        self.assertFalse(result)

    def test_reset_clears_all_state(self):
        metrics_exporter.register_exporter(lambda m: None)
        metrics_exporter.export_metrics(_SAMPLE)
        metrics_exporter.set_log_export_enabled(False)
        metrics_exporter.reset()
        status = metrics_exporter.get_status()
        self.assertEqual(status["exporter_count"], 0)
        self.assertEqual(status["export_count"], 0)
        self.assertTrue(status["log_export_enabled"])


if __name__ == "__main__":
    unittest.main()
