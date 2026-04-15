import threading
import unittest
from unittest.mock import patch

from modules.rollout import autoscaler as autoscaler_module


class AutoScalerResetMixin:
    def setUp(self):
        autoscaler_module._autoscaler_instance = None

    def tearDown(self):
        autoscaler_module._autoscaler_instance = None


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
        scaler._CONSECUTIVE_FAILURE_THRESHOLD = 10_000
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

        threads = [threading.Thread(target=worker_failure) for _ in range(4)]
        threads += [threading.Thread(target=worker_success) for _ in range(4)]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertGreaterEqual(scaler.get_consecutive_failures("w1"), 0)

