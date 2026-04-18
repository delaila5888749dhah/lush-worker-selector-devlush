"""Unit tests for integration/worker_task.py and app/__main__.py (PR-04).

Covers F-01 (entrypoint), F-03 (CDP driver registration), and F-04
(BitBrowser lifecycle) across the following scenarios:

  - Normal success path: driver registered, PID registered, profile registered,
    CDP listener probed, unregister_driver called on exit.
  - Exception in body: unregister_driver still called (finally guarantee).
  - SessionFlaggedError path: error propagates, cleanup still runs.
  - BitBrowser client unavailable (None): RuntimeError raised before session opens.
  - Browser PID available: _register_pid called with correct value.
  - Browser PID unavailable: _register_pid not called.
  - BitBrowser lifecycle order: create → launch (open) → close → delete.
  - app/__main__.py stub path (flag off): runtime.start receives no-op callable.
  - app/__main__.py production path (flag on): runtime.start receives make_task_fn result.
"""

import unittest
from unittest.mock import MagicMock, call, patch

from modules.common.exceptions import SessionFlaggedError


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_selenium_driver(pid=None):
    """Return a mock selenium driver with optional browser_pid attribute."""
    drv = MagicMock()
    drv.add_cdp_listener = MagicMock()  # expose callable so probe passes
    if pid is not None:
        drv.browser_pid = pid
    else:
        # Ensure getattr returns None for browser_pid (not a Mock)
        drv.configure_mock(browser_pid=None)
        drv.service = None
    return drv


def _make_bitbrowser_client(profile_id="profile-abc", webdriver_url="ws://127.0.0.1:9222/x"):
    """Return a mock BitBrowserClient that supports create/launch/close/delete."""
    client = MagicMock()
    client.create_profile.return_value = profile_id
    client.launch_profile.return_value = {"webdriver": webdriver_url}
    return client


# ── task_fn lifecycle tests ────────────────────────────────────────────────────


class TestMakeTaskFnSuccess(unittest.TestCase):
    """task_fn registers driver, profile, PID and probes CDP on success."""

    def setUp(self):
        self.profile_id = "profile-xyz"
        self.webdriver_url = "ws://127.0.0.1:9222/profile-xyz"
        self.selenium_drv = _make_selenium_driver(pid=12345)
        self.bb_client = _make_bitbrowser_client(
            profile_id=self.profile_id,
            webdriver_url=self.webdriver_url,
        )
        self.givex_drv = MagicMock()

    def _run(self, worker_id="worker-1"):
        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=self.bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=self.selenium_drv,
            ),
            patch(
                "modules.cdp.driver.GivexDriver",
                return_value=self.givex_drv,
            ) as mock_givex_cls,
            patch("integration.worker_task.cdp") as mock_cdp,
            patch(
                "integration.runtime.probe_cdp_listener_support"
            ) as mock_probe,
        ):
            from integration.worker_task import make_task_fn
            task_fn = make_task_fn()
            task_fn(worker_id)
            return mock_cdp, mock_probe, mock_givex_cls

    def test_register_driver_called(self):
        mock_cdp, _, mock_givex_cls = self._run()
        mock_cdp.register_driver.assert_called_once()
        args = mock_cdp.register_driver.call_args
        self.assertEqual(args[0][0], "worker-1")

    def test_register_browser_profile_called_with_profile_id(self):
        mock_cdp, _, _ = self._run()
        mock_cdp.register_browser_profile.assert_called_once_with(
            "worker-1", self.profile_id
        )

    def test_register_pid_called_when_pid_available(self):
        mock_cdp, _, _ = self._run()
        mock_cdp._register_pid.assert_called_once_with("worker-1", 12345)

    def test_probe_cdp_listener_called(self):
        _, mock_probe, _ = self._run()
        mock_probe.assert_called_once_with(self.selenium_drv)

    def test_unregister_driver_called_on_success(self):
        mock_cdp, _, _ = self._run()
        mock_cdp.unregister_driver.assert_called_once_with("worker-1")

    def test_register_before_unregister(self):
        """register_driver must happen before unregister_driver."""
        call_order = []
        bb_client = self.bb_client
        selenium_drv = self.selenium_drv
        givex_drv = self.givex_drv

        class _TrackingCdp:
            def register_driver(self, w, d):
                call_order.append("register")

            def unregister_driver(self, w):
                call_order.append("unregister")

            def _register_pid(self, w, p):
                call_order.append("register_pid")

            def register_browser_profile(self, w, p):
                call_order.append("register_profile")

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp", _TrackingCdp()),
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            from integration.worker_task import make_task_fn
            make_task_fn()("worker-1")

        self.assertIn("register", call_order)
        self.assertIn("unregister", call_order)
        self.assertLess(call_order.index("register"), call_order.index("unregister"))


class TestMakeTaskFnNoPid(unittest.TestCase):
    """_register_pid must NOT be called when browser PID is unavailable."""

    def test_register_pid_not_called_when_pid_unavailable(self):
        selenium_drv = _make_selenium_driver(pid=None)
        bb_client = _make_bitbrowser_client()
        givex_drv = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            from integration.worker_task import make_task_fn
            make_task_fn()("worker-1")
        mock_cdp._register_pid.assert_not_called()


class TestMakeTaskFnExceptionPath(unittest.TestCase):
    """unregister_driver must be called even when an exception is raised."""

    def _run_with_exception(self, exc):
        selenium_drv = _make_selenium_driver(pid=None)
        bb_client = _make_bitbrowser_client()
        givex_drv = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch(
                "integration.runtime.probe_cdp_listener_support",
                side_effect=exc,
            ),
        ):
            from integration.worker_task import make_task_fn
            with self.assertRaises(type(exc)):
                make_task_fn()("worker-1")
        return mock_cdp

    def test_unregister_called_on_runtime_error(self):
        mock_cdp = self._run_with_exception(RuntimeError("cdp probe failed"))
        mock_cdp.unregister_driver.assert_called_once_with("worker-1")

    def test_unregister_called_on_session_flagged_error(self):
        mock_cdp = self._run_with_exception(SessionFlaggedError("flagged"))
        mock_cdp.unregister_driver.assert_called_once_with("worker-1")

    def test_unregister_called_on_value_error(self):
        mock_cdp = self._run_with_exception(ValueError("bad value"))
        mock_cdp.unregister_driver.assert_called_once_with("worker-1")


class TestMakeTaskFnBitBrowserUnavailable(unittest.TestCase):
    """RuntimeError is raised (before session opens) when BitBrowser is None."""

    def test_raises_when_bitbrowser_client_is_none(self):
        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=None,
            ),
            patch("integration.worker_task.cdp") as mock_cdp,
        ):
            from integration.worker_task import make_task_fn
            task_fn = make_task_fn()
            with self.assertRaises(RuntimeError) as ctx:
                task_fn("worker-1")
        self.assertIn("BitBrowser client unavailable", str(ctx.exception))
        mock_cdp.register_driver.assert_not_called()
        mock_cdp.unregister_driver.assert_not_called()


class TestMakeTaskFnBitBrowserLifecycle(unittest.TestCase):
    """BitBrowserSession lifecycle: create → launch → close → delete in order."""

    def test_bitbrowser_client_called_in_lifecycle_order(self):
        bb_client = MagicMock()
        bb_client.create_profile.return_value = "p1"
        bb_client.launch_profile.return_value = {
            "webdriver": "ws://127.0.0.1:9222/p1"
        }
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            from integration.worker_task import make_task_fn
            make_task_fn()("worker-1")

        create_idx = None
        launch_idx = None
        close_idx = None
        delete_idx = None
        for i, c in enumerate(bb_client.method_calls):
            if c[0] == "create_profile":
                create_idx = i
            elif c[0] == "launch_profile":
                launch_idx = i
            elif c[0] == "close_profile":
                close_idx = i
            elif c[0] == "delete_profile":
                delete_idx = i

        self.assertIsNotNone(create_idx, "create_profile not called")
        self.assertIsNotNone(launch_idx, "launch_profile not called")
        self.assertIsNotNone(close_idx, "close_profile not called")
        self.assertIsNotNone(delete_idx, "delete_profile not called")
        self.assertLess(create_idx, launch_idx)
        self.assertLess(launch_idx, close_idx)
        self.assertLess(close_idx, delete_idx)

    def test_bitbrowser_close_called_even_on_exception(self):
        """BitBrowserSession.__exit__ (close + delete) called on exception."""
        bb_client = MagicMock()
        bb_client.create_profile.return_value = "p1"
        bb_client.launch_profile.return_value = {
            "webdriver": "ws://127.0.0.1:9222/p1"
        }
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch(
                "integration.runtime.probe_cdp_listener_support",
                side_effect=RuntimeError("probe failed"),
            ),
        ):
            from integration.worker_task import make_task_fn
            with self.assertRaises(RuntimeError):
                make_task_fn()("worker-1")

        bb_client.close_profile.assert_called_once_with("p1")
        bb_client.delete_profile.assert_called_once_with("p1")


# ── _get_browser_pid tests ─────────────────────────────────────────────────────


class TestGetBrowserPid(unittest.TestCase):
    """Unit tests for the _get_browser_pid helper."""

    def test_reads_browser_pid_attribute(self):
        from integration.worker_task import _get_browser_pid
        drv = MagicMock()
        drv.browser_pid = 9999
        self.assertEqual(_get_browser_pid(drv), 9999)

    def test_reads_service_process_pid(self):
        from integration.worker_task import _get_browser_pid
        drv = MagicMock()
        drv.configure_mock(browser_pid=None)
        drv.service.process.pid = 5555
        self.assertEqual(_get_browser_pid(drv), 5555)

    def test_returns_none_when_no_pid(self):
        from integration.worker_task import _get_browser_pid
        drv = MagicMock()
        drv.configure_mock(browser_pid=None)
        drv.service = None
        self.assertIsNone(_get_browser_pid(drv))

    def test_returns_none_on_attribute_error(self):
        from integration.worker_task import _get_browser_pid

        class Broken:
            @property
            def browser_pid(self):
                raise AttributeError("no pid")

        self.assertIsNone(_get_browser_pid(Broken()))


# ── _build_remote_driver tests ────────────────────────────────────────────────


class TestBuildRemoteDriver(unittest.TestCase):
    """_build_remote_driver raises RuntimeError when selenium is absent."""

    def test_raises_runtime_error_when_selenium_missing(self):
        import builtins
        real_import = builtins.__import__

        def _block_selenium(name, *args, **kwargs):
            if name == "selenium.webdriver":
                raise ImportError("no module named selenium")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_block_selenium):
            from integration.worker_task import _build_remote_driver
            with self.assertRaises(RuntimeError) as ctx:
                _build_remote_driver("ws://127.0.0.1:9222/x")
        self.assertIn("selenium is not installed", str(ctx.exception))


# ── app/__main__.py tests ─────────────────────────────────────────────────────


class TestAppMainStubPath(unittest.TestCase):
    """When ENABLE_PRODUCTION_TASK_FN is off, runtime.start receives stub."""

    def test_stub_task_fn_used_when_flag_off(self):
        with (
            patch.dict("os.environ", {"ENABLE_PRODUCTION_TASK_FN": "false"}),
            patch("integration.runtime.start") as mock_start,
            patch("integration.runtime.is_production_task_fn_enabled", return_value=False),
        ):
            import app.__main__ as app_main
            # Re-import to pick up patched env (reload handles module cache)
            import importlib
            importlib.reload(app_main)
            app_main.main()
        mock_start.assert_called_once()
        task_fn = mock_start.call_args[0][0]
        # Stub must be callable and accept worker_id without error
        task_fn("worker-test")

    def test_stub_task_fn_is_callable(self):
        import app.__main__ as app_main
        import importlib
        importlib.reload(app_main)
        stub = app_main._make_stub_task_fn()
        self.assertTrue(callable(stub))
        stub("worker-test")  # must not raise


class TestAppMainProductionPath(unittest.TestCase):
    """When ENABLE_PRODUCTION_TASK_FN is on, runtime.start receives make_task_fn result."""

    def test_production_task_fn_used_when_flag_on(self):
        fake_task_fn = MagicMock()
        with (
            patch("integration.runtime.is_production_task_fn_enabled", return_value=True),
            patch("integration.runtime.start") as mock_start,
            patch(
                "integration.worker_task.make_task_fn",
                return_value=fake_task_fn,
            ),
        ):
            import app.__main__ as app_main
            import importlib
            importlib.reload(app_main)
            app_main.main()
        mock_start.assert_called_once_with(fake_task_fn)


# ── runtime.is_production_task_fn_enabled tests ───────────────────────────────


class TestIsProductionTaskFnEnabled(unittest.TestCase):
    """is_production_task_fn_enabled() reflects ENABLE_PRODUCTION_TASK_FN env var."""

    def _check(self, val, expected):
        from integration import runtime
        with patch.dict("os.environ", {"ENABLE_PRODUCTION_TASK_FN": val}):
            self.assertEqual(runtime.is_production_task_fn_enabled(), expected)

    def test_true_for_1(self):
        self._check("1", True)

    def test_true_for_true(self):
        self._check("true", True)

    def test_true_for_TRUE(self):
        self._check("TRUE", True)

    def test_true_for_yes(self):
        self._check("yes", True)

    def test_false_for_false(self):
        self._check("false", False)

    def test_false_for_0(self):
        self._check("0", False)

    def test_false_for_empty(self):
        self._check("", False)

    def test_false_when_env_not_set(self):
        import os
        from integration import runtime
        env = {k: v for k, v in os.environ.items()
               if k != "ENABLE_PRODUCTION_TASK_FN"}
        with patch.dict("os.environ", env, clear=True):
            self.assertFalse(runtime.is_production_task_fn_enabled())


# ── task_fn zip wiring tests (F-07) ──────────────────────────────────────────


class TestMakeTaskFnZipWiring(unittest.TestCase):
    """task_fn resolves zip via MaxMind and forwards it to run_cycle (F-07)."""

    def _run(
        self,
        worker_id="worker-1",
        mock_ip="1.2.3.4",
        mock_zip="10001",
        task=None,
    ):
        """Run task_fn with a mock task_source and return the mock run_cycle."""
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        if task is None:
            task = MagicMock()
        mock_task_source = MagicMock(return_value=task)

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch("integration.runtime.probe_cdp_listener_support"),
            patch(
                "integration.worker_task._get_current_ip_best_effort",
                return_value=mock_ip,
            ),
            patch(
                "integration.worker_task.maxmind_lookup_zip",
                return_value=mock_zip,
            ),
            patch("integration.orchestrator.run_cycle") as mock_run_cycle,
        ):
            from integration.worker_task import make_task_fn
            make_task_fn(task_source=mock_task_source)(worker_id)
        return mock_run_cycle, mock_task_source, mock_cdp

    def test_run_cycle_called_with_resolved_zip(self):
        """run_cycle receives the zip resolved by MaxMind."""
        mock_run_cycle, _, _ = self._run(mock_zip="10001")
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertEqual(kwargs.get("zip_code"), "10001")

    def test_run_cycle_called_with_none_zip_when_maxmind_unavailable(self):
        """run_cycle receives zip_code=None when MaxMind cannot resolve a zip."""
        mock_run_cycle, _, _ = self._run(mock_zip=None)
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertIsNone(kwargs.get("zip_code"))

    def test_run_cycle_called_with_none_zip_when_ip_unavailable(self):
        """run_cycle receives zip_code=None when the public IP cannot be detected."""
        mock_run_cycle, _, _ = self._run(mock_ip=None, mock_zip=None)
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertIsNone(kwargs.get("zip_code"))

    def test_run_cycle_receives_correct_worker_id(self):
        """run_cycle is called with the same worker_id as task_fn."""
        mock_run_cycle, _, _ = self._run(worker_id="worker-42", mock_zip="90210")
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertEqual(kwargs.get("worker_id"), "worker-42")

    def test_run_cycle_not_called_when_task_source_returns_none(self):
        """run_cycle is skipped if task_source returns None (no pending task)."""
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            patch(
                "integration.worker_task._get_current_ip_best_effort",
                return_value="1.2.3.4",
            ),
            patch(
                "integration.worker_task.maxmind_lookup_zip",
                return_value="10001",
            ),
            patch("integration.orchestrator.run_cycle") as mock_run_cycle,
        ):
            from integration.worker_task import make_task_fn
            make_task_fn(task_source=MagicMock(return_value=None))("worker-1")
        mock_run_cycle.assert_not_called()

    def test_run_cycle_not_called_without_task_source(self):
        """run_cycle is not called when make_task_fn() is invoked with no task_source."""
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            patch(
                "integration.worker_task._get_current_ip_best_effort",
                return_value="1.2.3.4",
            ),
            patch(
                "integration.worker_task.maxmind_lookup_zip",
                return_value="10001",
            ),
            patch("integration.orchestrator.run_cycle") as mock_run_cycle,
        ):
            from integration.worker_task import make_task_fn
            make_task_fn()("worker-1")
        mock_run_cycle.assert_not_called()

    def test_unregister_still_called_when_run_cycle_raises(self):
        """cdp.unregister_driver is called even when run_cycle raises."""
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch("integration.runtime.probe_cdp_listener_support"),
            patch(
                "integration.worker_task._get_current_ip_best_effort",
                return_value="1.2.3.4",
            ),
            patch(
                "integration.worker_task.maxmind_lookup_zip",
                return_value="10001",
            ),
            patch(
                "integration.orchestrator.run_cycle",
                side_effect=RuntimeError("cycle failed"),
            ),
        ):
            from integration.worker_task import make_task_fn
            with self.assertRaises(RuntimeError):
                make_task_fn(task_source=MagicMock(return_value=MagicMock()))("w")
        mock_cdp.unregister_driver.assert_called_once_with("w")

    def test_task_source_called_with_worker_id(self):
        """task_source is invoked with the worker_id to retrieve the task."""
        _, mock_task_source, _ = self._run(worker_id="worker-99")
        mock_task_source.assert_called_once_with("worker-99")

    def test_maxmind_lookup_called_with_detected_ip(self):
        """maxmind_lookup_zip is called with the IP returned by _get_current_ip_best_effort."""
        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()
        task = MagicMock()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            patch(
                "integration.worker_task._get_current_ip_best_effort",
                return_value="203.0.113.5",
            ),
            patch(
                "integration.worker_task.maxmind_lookup_zip",
                return_value="90210",
            ) as mock_zip_lookup,
            patch("integration.orchestrator.run_cycle"),
        ):
            from integration.worker_task import make_task_fn
            make_task_fn(task_source=MagicMock(return_value=task))("w")
        mock_zip_lookup.assert_called_once_with("203.0.113.5")


if __name__ == "__main__":
    unittest.main()
