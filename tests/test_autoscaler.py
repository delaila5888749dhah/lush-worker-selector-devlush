import threading
import unittest
from unittest.mock import patch

from integration import runtime as runtime_module
from modules.common.thresholds import ERROR_RATE_THRESHOLD
from modules.rollout import autoscaler as autoscaler_module


class AutoScalerResetMixin:
    def setUp(self):
        autoscaler_module._autoscaler_instance = None  # pylint: disable=protected-access

    def tearDown(self):
        autoscaler_module._autoscaler_instance = None  # pylint: disable=protected-access


class TestConsecutiveFailures(AutoScalerResetMixin, unittest.TestCase):
    def test_five_consecutive_failures_triggers_scale_down(self):
        scaler = autoscaler_module.AutoScaler()
        with patch.object(scaler, "_scale_down_worker") as mock_scale_down_worker:
            for _ in range(5):
                scaler.record_failure("w1")
            mock_scale_down_worker.assert_called_once_with("w1")

    def test_success_resets_counter(self):
        scaler = autoscaler_module.AutoScaler()
        with patch.object(scaler, "_scale_down_worker") as mock_scale_down_worker:
            for _ in range(4):
                scaler.record_failure("w1")
            scaler.record_success("w1")
            for _ in range(4):
                scaler.record_failure("w1")
            mock_scale_down_worker.assert_not_called()
            self.assertEqual(scaler.get_consecutive_failures("w1"), 4)

    def test_record_methods_are_thread_safe(self):
        scaler = autoscaler_module.AutoScaler()
        errors = []

        def worker_failure():
            try:
                for _ in range(500):
                    scaler.record_failure("w1")
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        def worker_success():
            try:
                for _ in range(100):
                    scaler.record_success("w1")
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        with patch.object(scaler, "_scale_down_worker"):
            threads = [threading.Thread(target=worker_failure) for _ in range(4)]
            threads += [threading.Thread(target=worker_success) for _ in range(4)]

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertGreaterEqual(scaler.get_consecutive_failures("w1"), 0)

    def test_evaluate_scale_down_on_error_rate_threshold_breach(self):
        scaler = autoscaler_module.AutoScaler()
        with patch.object(scaler, "_scale_down") as mock_scale_down:
            scaler._evaluate_scale_down(ERROR_RATE_THRESHOLD + 0.01)  # pylint: disable=protected-access
            mock_scale_down.assert_called_once()

    def test_evaluate_scale_down_checks_worker_failure_thresholds(self):
        scaler = autoscaler_module.AutoScaler()
        scaler._consecutive_failures = {"w1": 5, "w2": 4, "w3": 6}  # pylint: disable=protected-access
        with patch.object(scaler, "_scale_down_worker") as mock_scale_down_worker:
            scaler._evaluate_scale_down(0.0)  # pylint: disable=protected-access
            self.assertEqual(mock_scale_down_worker.call_count, 2)
            mock_scale_down_worker.assert_any_call("w1")
            mock_scale_down_worker.assert_any_call("w3")


class TestAutoscalerReset(AutoScalerResetMixin, unittest.TestCase):
    def test_reset_clears_failure_counts(self):
        scaler = autoscaler_module.get_autoscaler()
        with patch.object(scaler, "_scale_down_worker"):
            for _ in range(3):
                scaler.record_failure("worker-1")
        self.assertEqual(scaler.get_consecutive_failures("worker-1"), 3)
        autoscaler_module.reset()
        self.assertEqual(autoscaler_module.get_autoscaler().get_consecutive_failures("worker-1"), 0)

    def test_reset_is_idempotent_when_no_instance(self):
        autoscaler_module.reset()  # must not raise when no instance exists
        autoscaler_module.reset()  # calling twice must also be safe
        self.assertIsNone(autoscaler_module._autoscaler_instance)  # pylint: disable=protected-access

    def test_runtime_reset_clears_autoscaler_state(self):
        runtime_module.reset()
        scaler = autoscaler_module.get_autoscaler()
        with patch.object(scaler, "_scale_down_worker"):
            for _ in range(3):
                scaler.record_failure("worker-1")
        self.assertEqual(scaler.get_consecutive_failures("worker-1"), 3)
        runtime_module.reset()
        self.assertEqual(autoscaler_module.get_autoscaler().get_consecutive_failures("worker-1"), 0)
