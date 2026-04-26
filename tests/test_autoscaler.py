import threading
import unittest
from unittest.mock import patch

from integration import runtime as runtime_module
from modules.common.thresholds import ERROR_RATE_THRESHOLD
from modules.rollout import autoscaler as autoscaler_module
from modules.rollout import main as rollout_module


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

    def test_evaluate_scale_down_failure_preserves_counter(self):
        """If _evaluate_scale_down raises during worker scale-down, counter must be preserved."""
        scaler = autoscaler_module.AutoScaler()
        scaler._consecutive_failures = {"w1": 5}  # pylint: disable=protected-access
        with patch.object(
            scaler, "_scale_down_worker", side_effect=RuntimeError("scale-down failed")
        ):
            with self.assertRaises(RuntimeError):
                scaler._evaluate_scale_down(0.0)  # pylint: disable=protected-access
        self.assertEqual(scaler.get_consecutive_failures("w1"), 5)

    def test_scale_down_failure_preserves_counter(self):
        """If _scale_down_worker raises, failure count must not be reset."""
        scaler = autoscaler_module.AutoScaler()
        with patch.object(
            scaler, "_scale_down_worker", side_effect=RuntimeError("scale-down failed")
        ):
            for _ in range(5):
                scaler.record_failure("w1")
        self.assertGreaterEqual(
            scaler.get_consecutive_failures("w1"),
            scaler._CONSECUTIVE_FAILURE_THRESHOLD,  # pylint: disable=protected-access
        )

    def test_scale_down_success_resets_counter(self):
        """Successful _scale_down_worker must reset the failure counter to 0."""
        scaler = autoscaler_module.AutoScaler()
        with patch.object(scaler, "_scale_down_worker"):
            for _ in range(5):
                scaler.record_failure("w1")
        self.assertEqual(scaler.get_consecutive_failures("w1"), 0)

    def test_scale_down_failure_allows_retry_on_next_failure(self):
        """After scale-down fails, the next failure event re-triggers scale-down."""
        scaler = autoscaler_module.AutoScaler()
        call_count = {"n": 0}

        def flaky_scale_down(_):
            """Fail on the first call; succeed on subsequent calls."""
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient failure")

        with patch.object(scaler, "_scale_down_worker", side_effect=flaky_scale_down):
            for _ in range(5):
                scaler.record_failure("w1")  # 5th triggers; scale-down fails
            scaler.record_failure("w1")  # counter now >= threshold → retriggers

        self.assertEqual(call_count["n"], 2)


class TestEvaluateScaleDownExternalOnlyContract(AutoScalerResetMixin, unittest.TestCase):
    """Blueprint §14.5: ``_evaluate_scale_down(error_rate)`` is two-mode.

    The per-worker failure sub-path (``error_rate=0.0``) is production-wired
    via ``get_recommended_scale_down_target()``; the global error-rate sub-path
    (``error_rate > ERROR_RATE_THRESHOLD``) is *external-only / opt-in* and
    must not be invoked by any automatic production loop.
    """

    def test_manual_external_trigger_invokes_global_scale_down(self):
        """Manual external caller passes a real error_rate → global scale-down fires."""
        scaler = autoscaler_module.AutoScaler()
        with patch.object(scaler, "_scale_down", return_value=1) as mock_scale_down:
            target = scaler._evaluate_scale_down(  # pylint: disable=protected-access
                ERROR_RATE_THRESHOLD + 0.05
            )
        mock_scale_down.assert_called_once()
        reason = mock_scale_down.call_args.kwargs.get("reason") or (
            mock_scale_down.call_args.args[0] if mock_scale_down.call_args.args else ""
        )
        self.assertIn("error_rate", reason)
        self.assertEqual(target, 1)

    def test_default_invocation_skips_global_threshold_branch(self):
        """``get_recommended_scale_down_target`` must not trigger the global path."""
        scaler = autoscaler_module.AutoScaler()
        with patch.object(scaler, "_scale_down") as mock_scale_down:
            with patch.object(scaler, "_scale_down_worker"):
                scaler.get_recommended_scale_down_target()
        mock_scale_down.assert_not_called()

    def test_negative_error_rate_skips_global_threshold_branch(self):
        """Negative error_rate must behave like 0.0 (threshold not triggered)."""
        scaler = autoscaler_module.AutoScaler()
        with patch.object(scaler, "_scale_down") as mock_scale_down:
            result = scaler._evaluate_scale_down(-1.0)  # pylint: disable=protected-access
        mock_scale_down.assert_not_called()
        self.assertIsNone(result)

    def test_runtime_loop_does_not_feed_error_rate(self):
        """Production runtime loop must call the autoscaler with default error_rate=0.0.

        Pins the Option B contract: ``integration.runtime`` only invokes the
        per-worker sub-path; it never plumbs a runtime ``error_rate`` value
        into ``_evaluate_scale_down``.
        """
        recorded = []  # list of (args, kwargs) tuples
        scaler = autoscaler_module.get_autoscaler()
        original = scaler._evaluate_scale_down  # pylint: disable=protected-access

        def spy(*args, **kwargs):
            recorded.append((args, kwargs))
            return original(*args, **kwargs)

        with patch.object(scaler, "_evaluate_scale_down", side_effect=spy):
            scaler.get_recommended_scale_down_target()

        self.assertEqual(len(recorded), 1)
        args, kwargs = recorded[0]
        # Either positional default omitted, or explicit 0.0 — never a real rate.
        passed_rate = kwargs.get("error_rate", args[0] if args else 0.0)
        self.assertEqual(passed_rate, 0.0)


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


class TestScaleDownSingleStep(AutoScalerResetMixin, unittest.TestCase):
    """N=1 safety: ``_scale_down`` must skip ``force_rollback`` via the
    lock-safe :func:`rollout.get_status` API (not raw ``SCALE_STEPS``)."""

    def setUp(self):
        super().setUp()
        rollout_module.configure_max_workers(1)

    def tearDown(self):
        super().tearDown()
        with rollout_module._lock:  # pylint: disable=protected-access
            rollout_module._runtime_max_worker_count = None  # pylint: disable=protected-access
            rollout_module._runtime_scale_steps = None  # pylint: disable=protected-access
        rollout_module.reset()

    def test_scale_down_skips_force_rollback_when_single_step(self):
        """With N=1, ``_scale_down`` must skip ``force_rollback`` and return 1."""
        scaler = autoscaler_module.AutoScaler()
        with patch.object(rollout_module, "force_rollback") as mock_force_rollback:
            workers = scaler._scale_down(reason="test")  # pylint: disable=protected-access
        mock_force_rollback.assert_not_called()
        self.assertEqual(workers, 1)

    def test_scale_down_reads_status_via_lock_safe_api(self):
        """``_scale_down`` must read rollout state via the lock-safe ``get_status`` API."""
        scaler = autoscaler_module.AutoScaler()
        with patch.object(
            rollout_module,
            "get_status",
            wraps=rollout_module.get_status,
        ) as mock_get_status:
            with patch.object(rollout_module, "force_rollback"):
                scaler._scale_down(reason="test")  # pylint: disable=protected-access
        mock_get_status.assert_called_once()
