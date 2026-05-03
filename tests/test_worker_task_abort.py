"""Core tests for P1-5: worker_task abort."""

import unittest
from unittest.mock import MagicMock, patch


def _make_selenium_driver():
    drv = MagicMock()
    drv.add_cdp_listener = MagicMock()
    drv.configure_mock(browser_pid=None)
    drv.service = None
    return drv


def _make_bitbrowser_client():
    client = MagicMock()
    client.create_profile.return_value = "profile-abc"
    client.launch_profile.return_value = {"webdriver": "ws://127.0.0.1:9222/x"}
    return client


def _reset_abort_registry():
    from integration import worker_task as wt
    with wt._abort_lock:
        wt._abort_flags.clear()


class TestAbortTaskApi(unittest.TestCase):
    """abort_task / is_task_aborted registry helpers."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_is_task_aborted_false_when_not_registered(self):
        from integration.worker_task import is_task_aborted
        self.assertFalse(is_task_aborted("unknown"))

    def test_abort_task_sets_flag(self):
        from integration.worker_task import abort_task, is_task_aborted
        abort_task("w1")
        self.assertTrue(is_task_aborted("w1"))

    def test_abort_task_without_prior_register_is_safe(self):
        from integration.worker_task import abort_task, is_task_aborted
        abort_task("never-registered")
        self.assertTrue(is_task_aborted("never-registered"))


class TestPreCycleAbort(unittest.TestCase):
    """task_fn returns early when abort is set before cycle starts."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_bitbrowser_not_opened_on_pre_cycle_abort(self):
        from integration.worker_task import abort_task, make_task_fn
        bb_client = _make_bitbrowser_client()
        abort_task("w-abort")

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=_make_selenium_driver()),
            patch("modules.cdp.driver.GivexDriver", return_value=MagicMock()),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            make_task_fn()("w-abort")

        bb_client.create_profile.assert_not_called()


class TestAbortCheckWiredIntoRunCycle(unittest.TestCase):
    """task_fn passes abort_check=lambda: is_task_aborted(worker_id) to run_cycle."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_abort_check_kwarg_passed_to_run_cycle(self):
        from integration.worker_task import make_task_fn
        task = MagicMock()

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=_make_bitbrowser_client()),
            patch("integration.worker_task._build_remote_driver", return_value=_make_selenium_driver()),
            patch("modules.cdp.driver.GivexDriver", return_value=MagicMock()),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            patch("integration.worker_task._get_current_ip_best_effort", return_value=None),
            patch("integration.worker_task.maxmind_lookup_zip", return_value=None),
            patch("integration.orchestrator.run_cycle", return_value=("complete", None, None)) as mock_run_cycle,
        ):
            make_task_fn(task_source=MagicMock(return_value=task))("w1")

        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertTrue(callable(kwargs.get("abort_check")))


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
        from integration.orchestrator import run_cycle
        task = self._make_task()

        with patch.dict("os.environ", {"ENABLE_RETRY_LOOP": "0"}):
            with patch("integration.orchestrator.initialize_cycle") as mock_init:
                with patch("integration.orchestrator.billing") as mock_billing:
                    mock_billing.select_profile.return_value = MagicMock()
                    action, _, _ = run_cycle(task, worker_id="w1", abort_check=lambda: True)

        self.assertEqual(action, "abort_cycle")
        mock_init.assert_not_called()

    def test_abort_check_true_in_retry_loop_iter0(self):
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
            action, _, _ = run_cycle(task, worker_id="w1", abort_check=lambda: True)

        self.assertEqual(action, "abort_cycle")
        mock_init.assert_not_called()


class TestAbortCycleReturn(unittest.TestCase):
    """task_fn captures run_cycle return; on 'abort_cycle' returns early."""

    def setUp(self):
        _reset_abort_registry()

    def tearDown(self):
        _reset_abort_registry()

    def test_abort_cycle_return_releases_profile_and_no_retry(self):
        from integration.worker_task import make_task_fn
        from modules.common.exceptions import CycleDidNotCompleteError
        task = MagicMock()
        bb_client = _make_bitbrowser_client()

        with (
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=_make_selenium_driver()),
            patch("modules.cdp.driver.GivexDriver", return_value=MagicMock()),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch("integration.runtime.probe_cdp_listener_support"),
            patch("integration.worker_task._get_current_ip_best_effort", return_value=None),
            patch("integration.worker_task.maxmind_lookup_zip", return_value=None),
            patch(
                "integration.orchestrator.run_cycle",
                return_value=("abort_cycle", None, None),
            ) as mock_run_cycle,
        ):
            # P0: abort_cycle must raise CycleDidNotCompleteError so the
            # runtime accounts the cycle as an error (not a success).  The
            # BitBrowserSession context manager still releases the profile
            # and the finally block still unregisters the driver.
            with self.assertRaises(CycleDidNotCompleteError) as cm:
                make_task_fn(task_source=MagicMock(return_value=task))("w-abort")

        self.assertEqual(cm.exception.action, "abort_cycle")
        mock_run_cycle.assert_called_once()
        # Profile released via BitBrowserSession context manager exit.
        bb_client.close_profile.assert_called_once()
        # Driver unregistered even when the cycle exits via exception.
        mock_cdp.unregister_driver.assert_called_once_with("w-abort")


if __name__ == "__main__":
    unittest.main()
