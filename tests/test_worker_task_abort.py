"""Unit and integration tests for P1-5: worker_task abort.

Covers:
  - abort_task / is_task_aborted / _register_abort / _clear_abort helpers
  - Pre-cycle abort: task_fn returns early without touching BitBrowser/CDP
  - Abort while run_cycle is executing: loop breaks at next check-point
  - Abort during non-retry (single-shot) run_cycle path
  - Abort flag cleared after task_fn exits (cleanup guarantee)
  - Abort is idempotent (calling abort_task multiple times is safe)
  - Abort of unknown worker_id is safe (no KeyError)
  - is_task_aborted returns False for unregistered worker
  - abort_check parameter wired into run_cycle from task_fn
  - Debug log emitted on every abort event
"""

import logging
import threading
import unittest
from unittest.mock import MagicMock, call, patch


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_selenium_driver(pid=None):
    drv = MagicMock()
    drv.add_cdp_listener = MagicMock()
    if pid is not None:
        drv.browser_pid = pid
    else:
        drv.configure_mock(browser_pid=None)
        drv.service = None
    return drv


def _make_bitbrowser_client(profile_id="profile-abc", webdriver_url="ws://127.0.0.1:9222/x"):
    client = MagicMock()
    client.create_profile.return_value = profile_id
    client.launch_profile.return_value = {"webdriver": webdriver_url}
    return client


def _reset_abort_registry():
    """Clear the module-level abort registry between tests."""
    from integration import worker_task as wt
    with wt._abort_lock:
        wt._abort_flags.clear()


# ── abort_task / is_task_aborted API tests ────────────────────────────────────


class TestAbortTaskApi(unittest.TestCase):
    """Low-level abort registry helpers."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_is_task_aborted_false_when_not_registered(self):
        from integration.worker_task import is_task_aborted
        self.assertFalse(is_task_aborted("worker-unknown"))

    def test_is_task_aborted_false_after_register(self):
        from integration.worker_task import _register_abort, is_task_aborted
        _register_abort("w1")
        self.assertFalse(is_task_aborted("w1"))

    def test_abort_task_sets_flag(self):
        from integration.worker_task import abort_task, _register_abort, is_task_aborted
        _register_abort("w1")
        abort_task("w1")
        self.assertTrue(is_task_aborted("w1"))

    def test_abort_task_without_prior_register_is_safe(self):
        """abort_task for an unregistered worker must not raise."""
        from integration.worker_task import abort_task, is_task_aborted
        abort_task("w-new")
        self.assertTrue(is_task_aborted("w-new"))

    def test_abort_task_idempotent(self):
        from integration.worker_task import abort_task, _register_abort, is_task_aborted
        _register_abort("w2")
        abort_task("w2")
        abort_task("w2")  # second call must not raise
        self.assertTrue(is_task_aborted("w2"))

    def test_clear_abort_removes_flag(self):
        from integration.worker_task import (
            abort_task, _clear_abort, _register_abort, is_task_aborted,
        )
        _register_abort("w3")
        abort_task("w3")
        _clear_abort("w3")
        self.assertFalse(is_task_aborted("w3"))

    def test_clear_abort_unknown_worker_safe(self):
        """_clear_abort on unknown worker_id must not raise."""
        from integration.worker_task import _clear_abort
        _clear_abort("nobody")

    def test_abort_logs_debug(self):
        from integration.worker_task import abort_task, _register_abort
        _register_abort("w-log")
        with self.assertLogs("integration.worker_task", level="DEBUG") as cm:
            abort_task("w-log")
        self.assertTrue(
            any("abort_task=requested" in line for line in cm.output),
            f"Expected abort_task=requested in logs: {cm.output}",
        )


# ── Pre-cycle abort: task_fn exits before opening BitBrowser ──────────────────


class TestPreCycleAbort(unittest.TestCase):
    """task_fn returns early when abort is set before the cycle starts."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def _run_aborted_task_fn(self, worker_id="w-abort"):
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        from integration.worker_task import abort_task, make_task_fn
        # Set the abort flag before task_fn runs.
        abort_task(worker_id)

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=selenium_drv),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            task_fn = make_task_fn()
            task_fn(worker_id)
        return bb_client, mock_cdp

    def test_bitbrowser_not_opened_on_pre_cycle_abort(self):
        bb_client, _ = self._run_aborted_task_fn()
        bb_client.create_profile.assert_not_called()

    def test_cdp_register_not_called_on_pre_cycle_abort(self):
        _, mock_cdp = self._run_aborted_task_fn()
        mock_cdp.register_driver.assert_not_called()

    def test_cdp_unregister_not_called_on_pre_cycle_abort(self):
        """unregister_driver is not called because task_fn returned before registration."""
        _, mock_cdp = self._run_aborted_task_fn()
        mock_cdp.unregister_driver.assert_not_called()

    def test_abort_flag_cleared_after_pre_cycle_abort(self):
        from integration.worker_task import is_task_aborted
        self._run_aborted_task_fn(worker_id="w-pre")
        self.assertFalse(is_task_aborted("w-pre"))

    def test_pre_cycle_abort_logs_debug(self):
        from integration.worker_task import abort_task, make_task_fn
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()
        abort_task("w-dbg")

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=selenium_drv),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            self.assertLogs("integration.worker_task", level="DEBUG") as cm,
        ):
            make_task_fn()("w-dbg")

        self.assertTrue(
            any("pre_cycle_abort" in line for line in cm.output),
            f"Expected pre_cycle_abort in logs: {cm.output}",
        )


# ── Abort flag cleared on normal exit ────────────────────────────────────────


class TestAbortFlagCleanup(unittest.TestCase):
    """Abort flag is always cleared when task_fn exits, even on exception."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_abort_flag_cleared_after_normal_run(self):
        from integration.worker_task import is_task_aborted, make_task_fn
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=selenium_drv),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            make_task_fn()("w-clean")
        self.assertFalse(is_task_aborted("w-clean"))

    def test_abort_flag_cleared_after_exception(self):
        from integration.worker_task import is_task_aborted, make_task_fn
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=selenium_drv),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch(
                "integration.runtime.probe_cdp_listener_support",
                side_effect=RuntimeError("probe failed"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                make_task_fn()("w-exc")
        self.assertFalse(is_task_aborted("w-exc"))


# ── abort_check wired into run_cycle ─────────────────────────────────────────


class TestAbortCheckWiredIntoRunCycle(unittest.TestCase):
    """task_fn passes abort_check=lambda: is_task_aborted(worker_id) to run_cycle."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_abort_check_kwarg_passed_to_run_cycle(self):
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()
        task = MagicMock()
        mock_task_source = MagicMock(return_value=task)

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=selenium_drv),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            patch("integration.worker_task._get_current_ip_best_effort", return_value=None),
            patch("integration.worker_task.maxmind_lookup_zip", return_value=None),
            patch("integration.orchestrator.run_cycle") as mock_run_cycle,
        ):
            from integration.worker_task import make_task_fn
            make_task_fn(task_source=mock_task_source)("w1")

        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        abort_check = kwargs.get("abort_check")
        self.assertIsNotNone(abort_check, "abort_check must be passed to run_cycle")
        self.assertTrue(callable(abort_check))

    def test_abort_check_returns_false_initially(self):
        """abort_check lambda returns False when no abort has been requested."""
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()
        task = MagicMock()

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=selenium_drv),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            patch("integration.worker_task._get_current_ip_best_effort", return_value=None),
            patch("integration.worker_task.maxmind_lookup_zip", return_value=None),
            patch("integration.orchestrator.run_cycle") as mock_run_cycle,
        ):
            from integration.worker_task import make_task_fn
            make_task_fn(task_source=MagicMock(return_value=task))("w-check")

        _, kwargs = mock_run_cycle.call_args
        abort_check = kwargs["abort_check"]
        self.assertFalse(abort_check())


# ── run_cycle abort_check parameter tests ────────────────────────────────────


class TestRunCycleAbortCheck(unittest.TestCase):
    """run_cycle honours abort_check in both single-shot and retry-loop paths."""

    def _make_task(self):
        from modules.common.types import WorkerTask, CardInfo
        card = CardInfo(card_number="4111111111111111", exp_month="12", exp_year="28", cvv="123")
        return WorkerTask(
            task_id="task-abc",
            recipient_email="test@example.com",
            amount=50,
            primary_card=card,
            order_queue=(card,),
        )

    def test_abort_check_true_before_single_shot(self):
        """Single-shot path (ENABLE_RETRY_LOOP=0) aborts before initialize_cycle."""
        from integration.orchestrator import run_cycle
        task = self._make_task()

        with patch.dict("os.environ", {"ENABLE_RETRY_LOOP": "0"}):
            with patch("integration.orchestrator.initialize_cycle") as mock_init:
                with patch("integration.orchestrator.billing") as mock_billing:
                    mock_billing.select_profile.return_value = MagicMock()
                    action, state, total = run_cycle(
                        task,
                        worker_id="w1",
                        abort_check=lambda: True,
                    )
        self.assertEqual(action, "abort_cycle")
        mock_init.assert_not_called()

    def test_abort_check_false_single_shot_proceeds(self):
        """Single-shot path proceeds normally when abort_check returns False."""
        from integration.orchestrator import run_cycle
        task = self._make_task()
        mock_state = MagicMock()
        mock_state.name = "success"

        with patch.dict("os.environ", {"ENABLE_RETRY_LOOP": "0"}):
            with (
                patch("integration.orchestrator.initialize_cycle"),
                patch("integration.orchestrator.billing") as mock_billing,
                patch("integration.orchestrator.run_payment_step", return_value=(mock_state, 50.0)),
                patch("integration.orchestrator.handle_outcome", return_value="complete"),
                patch("integration.orchestrator._record_autoscaler_success"),
                patch("integration.orchestrator._notify_success"),
                patch("integration.orchestrator.cdp") as mock_cdp,
                patch("integration.orchestrator.fsm"),
                patch("integration.orchestrator._get_idempotency_store") as mock_store,
            ):
                mock_billing.select_profile.return_value = MagicMock()
                mock_cdp.unregister_driver = MagicMock()
                store = MagicMock()
                store.is_duplicate.return_value = False
                mock_store.return_value = store
                action, _, _ = run_cycle(
                    task,
                    worker_id="w1",
                    abort_check=lambda: False,
                )
        self.assertEqual(action, "complete")

    def test_abort_check_true_in_retry_loop_iter0(self):
        """Retry loop aborts at first iteration if abort_check is True."""
        from integration.orchestrator import run_cycle
        task = self._make_task()

        with (
            patch("integration.orchestrator.initialize_cycle") as mock_init,
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator._get_idempotency_store") as mock_store,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator.cdp") as mock_cdp,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            store = MagicMock()
            store.is_duplicate.return_value = False
            mock_store.return_value = store
            mock_cdp.unregister_driver = MagicMock()
            action, state, total = run_cycle(
                task,
                worker_id="w1",
                abort_check=lambda: True,
            )

        self.assertEqual(action, "abort_cycle")
        mock_init.assert_not_called()

    def test_abort_check_true_mid_loop_stops_retries(self):
        """Abort at iteration 1 stops the loop without starting a second payment step."""
        from integration.orchestrator import run_cycle
        task = self._make_task()

        call_count = {"n": 0}

        def abort_after_first():
            call_count["n"] += 1
            return call_count["n"] > 1

        mock_state = MagicMock()
        mock_state.name = "declined"

        with (
            patch("integration.orchestrator.initialize_cycle"),
            patch("integration.orchestrator.billing") as mock_billing,
            patch(
                "integration.orchestrator.run_payment_step",
                return_value=(mock_state, None),
            ) as mock_pay,
            patch("integration.orchestrator.handle_outcome", return_value="retry"),
            patch("integration.orchestrator._record_autoscaler_failure"),
            patch("integration.orchestrator._get_idempotency_store") as mock_store,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator.cdp") as mock_cdp,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            store = MagicMock()
            store.is_duplicate.return_value = False
            mock_store.return_value = store
            mock_cdp.unregister_driver = MagicMock()
            action, _, _ = run_cycle(
                task,
                worker_id="w1",
                abort_check=abort_after_first,
            )

        self.assertEqual(action, "abort_cycle")
        # First iteration ran, second was aborted before run_payment_step
        self.assertEqual(mock_pay.call_count, 1)

    def test_abort_check_none_default_no_change(self):
        """run_cycle with abort_check=None (default) behaves identically to before."""
        from integration.orchestrator import run_cycle
        task = self._make_task()
        mock_state = MagicMock()
        mock_state.name = "success"

        with (
            patch("integration.orchestrator.initialize_cycle"),
            patch("integration.orchestrator.billing") as mock_billing,
            patch(
                "integration.orchestrator.run_payment_step",
                return_value=(mock_state, 50.0),
            ),
            patch("integration.orchestrator.handle_outcome", return_value="complete"),
            patch("integration.orchestrator._record_autoscaler_success"),
            patch("integration.orchestrator._notify_success"),
            patch("integration.orchestrator._get_idempotency_store") as mock_store,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator.cdp") as mock_cdp,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            store = MagicMock()
            store.is_duplicate.return_value = False
            mock_store.return_value = store
            mock_cdp.unregister_driver = MagicMock()
            action, _, _ = run_cycle(task, worker_id="w1")

        self.assertEqual(action, "complete")

    def test_abort_cycle_logs_debug(self):
        """Abort inside retry loop emits a debug log."""
        from integration.orchestrator import run_cycle
        task = self._make_task()

        with (
            patch("integration.orchestrator.initialize_cycle"),
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator._get_idempotency_store") as mock_store,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator.cdp") as mock_cdp,
            self.assertLogs("integration.orchestrator", level="DEBUG") as cm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            store = MagicMock()
            store.is_duplicate.return_value = False
            mock_store.return_value = store
            mock_cdp.unregister_driver = MagicMock()
            run_cycle(task, worker_id="w-log", abort_check=lambda: True)

        self.assertTrue(
            any("abort_task=abort_cycle" in line for line in cm.output),
            f"Expected abort_task=abort_cycle in logs: {cm.output}",
        )


# ── Thread-safety: concurrent abort_task calls ───────────────────────────────


class TestAbortTaskThreadSafety(unittest.TestCase):
    """Multiple threads may call abort_task concurrently without errors."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_concurrent_abort_calls_safe(self):
        from integration.worker_task import abort_task, _register_abort, is_task_aborted
        worker_id = "w-concurrent"
        _register_abort(worker_id)
        errors = []

        def _abort():
            try:
                abort_task(worker_id)
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        threads = [threading.Thread(target=_abort) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent abort raised exceptions: {errors}")
        self.assertTrue(is_task_aborted(worker_id))


if __name__ == "__main__":
    unittest.main()
