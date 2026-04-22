"""Tests for runtime MAX_WORKER_COUNT cap wiring."""

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from integration import runtime
from integration.runtime import ConfigError, reset, start, stop
from modules.billing import main as billing
from modules.monitor import main as monitor
from modules.rollout import main as rollout


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _CapRuntimeMixin:
    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()
        self._saved_env = os.environ.get("MAX_WORKER_COUNT")
        self._saved_worker = os.environ.get("WORKER_COUNT")
        self._billing_pool_dir = tempfile.mkdtemp()
        with open(os.path.join(self._billing_pool_dir, "profiles.txt"), "w", encoding="utf-8") as handle:
            handle.write("Alice|Smith|1 Main St|City|NY|10001|2125550001|a@e.com\n")
        self._billing_pool_patcher = patch.object(
            billing, "_pool_dir", return_value=Path(self._billing_pool_dir)
        )
        self._billing_pool_patcher.start()

    def tearDown(self):
        self._billing_pool_patcher.stop()
        shutil.rmtree(self._billing_pool_dir, ignore_errors=True)
        if self._saved_env is None:
            os.environ.pop("MAX_WORKER_COUNT", None)
        else:
            os.environ["MAX_WORKER_COUNT"] = self._saved_env
        if self._saved_worker is None:
            os.environ.pop("WORKER_COUNT", None)
        else:
            os.environ["WORKER_COUNT"] = self._saved_worker
        reset()
        rollout.reset()
        monitor.reset()

    def _run_for_cap(self, cap):
        os.environ["MAX_WORKER_COUNT"] = str(cap)
        os.environ.pop("WORKER_COUNT", None)
        applied = []
        lock = threading.Lock()

        def _record(target, _task_fn):
            with lock:
                applied.append(target)

        with patch("integration.runtime._apply_scale", side_effect=_record):
            self.assertTrue(start(lambda _: None, interval=0.02))
            try:
                self.assertTrue(_wait_until(lambda: len(applied) >= 3))
            finally:
                stop(timeout=2)
        return applied


class TestMaxWorkerCountCap(_CapRuntimeMixin, unittest.TestCase):
    def test_runtime_never_exceeds_cap(self):
        for cap in (1, 2, 4, 7, 10, 12):
            with self.subTest(cap=cap):
                applied = self._run_for_cap(cap)
                self.assertTrue(applied)
                self.assertTrue(all(target <= cap for target in applied))
                self.assertEqual(rollout.SCALE_STEPS[-1], cap)

    def test_validate_startup_config_rejects_invalid_max_worker_count(self):
        cases = (
            ({"MAX_WORKER_COUNT": "abc"}, "non-int"),
            ({"MAX_WORKER_COUNT": "0"}, "zero"),
            ({"MAX_WORKER_COUNT": "51"}, "over-50"),
            ({"MAX_WORKER_COUNT": "3", "WORKER_COUNT": "5"}, "worker>max"),
        )
        for env, label in cases:
            with self.subTest(case=label):
                os.environ.pop("MAX_WORKER_COUNT", None)
                os.environ.pop("WORKER_COUNT", None)
                os.environ.update(env)
                with self.assertRaises(ConfigError):
                    runtime._validate_startup_config()  # pylint: disable=protected-access

    def test_validate_startup_config_accepts_valid_pairs(self):
        os.environ["MAX_WORKER_COUNT"] = "5"
        os.environ["WORKER_COUNT"] = "5"
        runtime._validate_startup_config()  # pylint: disable=protected-access
        os.environ.pop("MAX_WORKER_COUNT", None)
        os.environ["WORKER_COUNT"] = "3"
        runtime._validate_startup_config()  # pylint: disable=protected-access

    def test_apply_scale_clamps_above_cap_and_logs_warning(self):
        os.environ["MAX_WORKER_COUNT"] = "4"
        rollout.configure_max_workers(4)
        launches = []

        def _fake_start(_task_fn):
            launches.append(None)

        runtime._state = "RUNNING"  # pylint: disable=protected-access
        try:
            with patch("integration.runtime.start_worker", side_effect=_fake_start), \
                 patch("integration.runtime._logger") as mock_logger:
                runtime._apply_scale(99, lambda _: None)  # pylint: disable=protected-access
            self.assertEqual(len(launches), 4)
            self.assertTrue(
                any("clamp target" in str(call.args[0]) for call in mock_logger.warning.call_args_list)
            )
        finally:
            runtime._state = "INIT"  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
