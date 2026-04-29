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
from modules.cdp.fingerprint import BitBrowserLaunchEndpoint


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


class TestEndOfCycleHardReset(unittest.TestCase):
    """Blueprint §7 / INV-SESSION-04: end-of-cycle hard-reset.

    ``GivexDriver._clear_browser_state()`` must be invoked in the cycle's
    finally block — after run_cycle finishes (success / abort / exception)
    and BEFORE BitBrowserSession.__exit__ closes the profile — in addition
    to the two start-of-cycle wipes inside ``navigate_to_egift``.
    """

    def _run(self, probe_side_effect=None):
        bb_client = MagicMock()
        bb_client.create_profile.return_value = "p1"
        bb_client.launch_profile.return_value = {
            "webdriver": "ws://127.0.0.1:9222/p1"
        }
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()

        # Track ordering of end-of-cycle cleanup vs. BitBrowser close.
        call_log = []
        givex_drv._clear_browser_state.side_effect = (
            lambda: call_log.append("clear_browser_state")
        )
        bb_client.close_profile.side_effect = (
            lambda pid: call_log.append("close_profile")
        )
        bb_client.delete_profile.side_effect = (
            lambda pid: call_log.append("delete_profile")
        )

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
                side_effect=probe_side_effect,
            ),
        ):
            from integration.worker_task import make_task_fn
            try:
                make_task_fn()("worker-1")
            except Exception:  # pylint: disable=broad-except
                pass
        return givex_drv, call_log

    def test_clear_browser_state_called_at_end_of_cycle(self):
        """``_clear_browser_state`` must be invoked once in cycle finally."""
        givex_drv, _ = self._run()
        givex_drv._clear_browser_state.assert_called_once()

    def test_clear_browser_state_runs_before_browser_close(self):
        """End-of-cycle wipe must precede ``/browser/close`` (Blueprint §7)."""
        _, call_log = self._run()
        self.assertIn("clear_browser_state", call_log)
        self.assertIn("close_profile", call_log)
        self.assertLess(
            call_log.index("clear_browser_state"),
            call_log.index("close_profile"),
            "_clear_browser_state must run before close_profile",
        )

    def test_clear_browser_state_called_even_on_exception(self):
        """End-of-cycle wipe runs even when the cycle aborts via exception."""
        givex_drv, call_log = self._run(
            probe_side_effect=RuntimeError("probe failed")
        )
        givex_drv._clear_browser_state.assert_called_once()
        self.assertIn("close_profile", call_log)
        self.assertLess(
            call_log.index("clear_browser_state"),
            call_log.index("close_profile"),
        )

    def test_clear_browser_state_failure_does_not_propagate(self):
        """A raise from ``_clear_browser_state`` must not break cleanup."""
        bb_client = MagicMock()
        bb_client.create_profile.return_value = "p1"
        bb_client.launch_profile.return_value = {
            "webdriver": "ws://127.0.0.1:9222/p1"
        }
        selenium_drv = _make_selenium_driver()
        givex_drv = MagicMock()
        givex_drv._clear_browser_state.side_effect = RuntimeError(
            "selenium session gone"
        )

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
            # Must NOT raise — cleanup is best-effort.
            make_task_fn()("worker-1")

        givex_drv._clear_browser_state.assert_called_once()
        # Driver still unregistered, profile still released.
        mock_cdp.unregister_driver.assert_called_once_with("worker-1")
        bb_client.close_profile.assert_called_once_with("p1")

    def test_no_clear_when_givex_driver_construction_fails(self):
        """If ``GivexDriver(...)`` raises, no ``_clear_browser_state`` call.

        ``givex_driver`` is None when construction fails, so the cleanup
        guard must skip the call (no ``NameError``, no spurious call).
        """
        bb_client = MagicMock()
        bb_client.create_profile.return_value = "p1"
        bb_client.launch_profile.return_value = {
            "webdriver": "ws://127.0.0.1:9222/p1"
        }
        selenium_drv = _make_selenium_driver()

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch(
                "modules.cdp.driver.GivexDriver",
                side_effect=RuntimeError("driver init failed"),
            ),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            from integration.worker_task import make_task_fn
            with self.assertRaises(RuntimeError):
                make_task_fn()("worker-1")

        # Cleanup path still ran.
        mock_cdp.unregister_driver.assert_called_once_with("worker-1")
        bb_client.close_profile.assert_called_once_with("p1")


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
    """Tests for ``_build_remote_driver``.

    Verifies forward-compat with Selenium >= 4.10 (``options=`` kwarg) and
    a graceful fallback to ``desired_capabilities=`` for legacy clients.
    """

    def test_raises_runtime_error_when_selenium_missing(self):
        def _block_selenium(name):
            if name == "selenium.webdriver":
                raise ImportError("no module named selenium")
            raise AssertionError(f"unexpected import: {name}")

        with patch(
            "integration.worker_task.importlib.import_module",
            side_effect=_block_selenium,
        ):
            from integration.worker_task import _build_remote_driver
            with self.assertRaises(RuntimeError) as ctx:
                _build_remote_driver("ws://127.0.0.1:9222/x")
        self.assertIn("selenium is not installed", str(ctx.exception))

    def test_uses_options_kwarg_for_selenium_4_10_plus(self):
        """Modern Selenium accepts ``options=ChromeOptions()``."""
        sentinel_driver = object()
        options_instance = MagicMock(name="ChromeOptionsInstance")
        ChromeOptions = MagicMock(return_value=options_instance)
        Remote = MagicMock(return_value=sentinel_driver)
        fake_webdriver = MagicMock(Remote=Remote, ChromeOptions=ChromeOptions)

        def _import(name):
            if name == "selenium.webdriver":
                return fake_webdriver
            raise AssertionError(f"unexpected import: {name}")

        with patch(
            "integration.worker_task.importlib.import_module",
            side_effect=_import,
        ):
            from integration.worker_task import _build_remote_driver
            result = _build_remote_driver("ws://127.0.0.1:9222/x")

        self.assertIs(result, sentinel_driver)
        Remote.assert_called_once_with(
            command_executor="ws://127.0.0.1:9222/x",
            options=options_instance,
        )
        # Must not pass deprecated kwarg.
        _, kwargs = Remote.call_args
        self.assertNotIn("desired_capabilities", kwargs)

    def test_legacy_launch_endpoint_uses_remote_driver(self):
        """Legacy BitBrowser webdriver metadata still uses Selenium Remote."""
        sentinel_driver = object()
        options_instance = MagicMock(name="ChromeOptionsInstance")
        ChromeOptions = MagicMock(return_value=options_instance)
        Remote = MagicMock(return_value=sentinel_driver)
        Chrome = MagicMock()
        fake_webdriver = MagicMock(
            Remote=Remote,
            Chrome=Chrome,
            ChromeOptions=ChromeOptions,
        )

        def _import(name):
            if name == "selenium.webdriver":
                return fake_webdriver
            raise AssertionError(f"unexpected import: {name}")

        with patch(
            "integration.worker_task.importlib.import_module",
            side_effect=_import,
        ):
            from integration.worker_task import _build_remote_driver
            result = _build_remote_driver("http://127.0.0.1:9999")

        self.assertIs(result, sentinel_driver)
        Remote.assert_called_once_with(
            command_executor="http://127.0.0.1:9999",
            options=options_instance,
        )
        Chrome.assert_not_called()

    def test_modern_launch_endpoint_uses_chromedriver_attach_mode(self):
        """BitBrowser v144/v146 metadata uses local chromedriver attach mode."""
        sentinel_driver = object()
        options_instance = MagicMock(name="ChromeOptionsInstance")
        service_instance = MagicMock(name="ServiceInstance")
        ChromeOptions = MagicMock(return_value=options_instance)
        Chrome = MagicMock(return_value=sentinel_driver)
        Remote = MagicMock()
        fake_webdriver = MagicMock(
            Remote=Remote,
            Chrome=Chrome,
            ChromeOptions=ChromeOptions,
        )
        Service = MagicMock(return_value=service_instance)
        fake_service_module = MagicMock(Service=Service)

        def _import(name):
            if name == "selenium.webdriver":
                return fake_webdriver
            if name == "selenium.webdriver.chrome.service":
                return fake_service_module
            raise AssertionError(f"unexpected import: {name}")

        endpoint = BitBrowserLaunchEndpoint(
            debugger_address="127.0.0.1:64663",
            driver_path=r"C:\chromedriver\144\chromedriver.exe",
        )
        with patch(
            "integration.worker_task.importlib.import_module",
            side_effect=_import,
        ):
            from integration.worker_task import _build_remote_driver
            result = _build_remote_driver(endpoint)

        self.assertIs(result, sentinel_driver)
        self.assertEqual(options_instance.debugger_address, "127.0.0.1:64663")
        Service.assert_called_once_with(
            executable_path=r"C:\chromedriver\144\chromedriver.exe"
        )
        Chrome.assert_called_once_with(service=service_instance, options=options_instance)
        Remote.assert_not_called()

    def test_incomplete_launch_endpoint_error_lists_required_fields(self):
        from integration.worker_task import _build_remote_driver

        with self.assertRaises(RuntimeError) as ctx:
            _build_remote_driver(object())
        msg = str(ctx.exception)
        self.assertIn("webdriver_url", msg)
        self.assertIn("debugger_address", msg)
        self.assertIn("driver_path", msg)

    def test_bitbrowser_launch_endpoint_rejects_missing_fields(self):
        with self.assertRaises(ValueError) as ctx:
            BitBrowserLaunchEndpoint(debugger_address="127.0.0.1:64663")
        msg = str(ctx.exception)
        self.assertIn("webdriver_url", msg)
        self.assertIn("debugger_address", msg)
        self.assertIn("driver_path", msg)

    def test_falls_back_to_desired_capabilities_on_typeerror(self):
        """Legacy Selenium that rejects ``options=`` falls back gracefully."""
        sentinel_driver = object()
        options_instance = MagicMock(name="ChromeOptionsInstance")
        ChromeOptions = MagicMock(return_value=options_instance)

        def _remote(*_args, **kwargs):
            if "options" in kwargs:
                raise TypeError("unexpected keyword argument 'options'")
            return sentinel_driver

        Remote = MagicMock(side_effect=_remote)
        fake_webdriver = MagicMock(Remote=Remote, ChromeOptions=ChromeOptions)

        fake_caps = MagicMock()
        fake_caps.DesiredCapabilities.CHROME = {"browserName": "chrome"}

        def _import(name):
            if name == "selenium.webdriver":
                return fake_webdriver
            if name == "selenium.webdriver.common.desired_capabilities":
                return fake_caps
            raise AssertionError(f"unexpected import: {name}")

        with patch(
            "integration.worker_task.importlib.import_module",
            side_effect=_import,
        ):
            from integration.worker_task import _build_remote_driver
            result = _build_remote_driver("ws://127.0.0.1:9222/x")

        self.assertIs(result, sentinel_driver)
        # Two calls: first with options= (raises TypeError), then with
        # desired_capabilities= (succeeds).
        self.assertEqual(Remote.call_count, 2)
        first_kwargs = Remote.call_args_list[0].kwargs
        second_kwargs = Remote.call_args_list[1].kwargs
        self.assertIn("options", first_kwargs)
        self.assertIn("desired_capabilities", second_kwargs)
        self.assertEqual(
            second_kwargs["desired_capabilities"], {"browserName": "chrome"}
        )


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
            patch.dict("os.environ", {"MIN_BILLING_PROFILES": "1"}, clear=False),
            # Patch at the implementation level so the patches survive importlib.reload.
            # _startup_check_geoip calls os.path.exists for the MMDB file;
            # make it look like the file exists, and no-op the actual reader init.
            patch("os.path.exists", return_value=True),
            patch("modules.cdp.driver.init_maxmind_reader"),
            # _startup_load_billing_pool calls billing.load_billing_pool; return a
            # value that satisfies the production min-profile threshold (default 1).
            patch("modules.billing.main.load_billing_pool", return_value=1),
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
        mock_offset=-8.0,
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
            patch(
                "integration.worker_task._lookup_maxmind_utc_offset",
                return_value=mock_offset,
            ),
            patch("integration.worker_task.set_utc_offset") as mock_set_utc_offset,
            patch("integration.orchestrator.run_cycle", return_value=("complete", None, None)) as mock_run_cycle,
        ):
            from integration.worker_task import make_task_fn
            make_task_fn(task_source=mock_task_source)(worker_id)
        return mock_run_cycle, mock_task_source, mock_cdp, mock_set_utc_offset

    def test_run_cycle_called_with_resolved_zip(self):
        """run_cycle receives the zip resolved by MaxMind."""
        mock_run_cycle, _, _, _ = self._run(mock_zip="10001")
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertEqual(kwargs.get("zip_code"), "10001")

    def test_run_cycle_called_with_none_zip_when_maxmind_unavailable(self):
        """run_cycle receives zip_code=None when MaxMind cannot resolve a zip."""
        mock_run_cycle, _, _, _ = self._run(mock_zip=None)
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertIsNone(kwargs.get("zip_code"))

    def test_run_cycle_called_with_none_zip_when_ip_unavailable(self):
        """run_cycle receives zip_code=None when the public IP cannot be detected."""
        mock_run_cycle, _, _, _ = self._run(mock_ip=None, mock_zip=None)
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertIsNone(kwargs.get("zip_code"))

    def test_run_cycle_receives_correct_worker_id(self):
        """run_cycle is called with the same worker_id as task_fn."""
        mock_run_cycle, _, _, _ = self._run(worker_id="worker-42", mock_zip="90210")
        mock_run_cycle.assert_called_once()
        _, kwargs = mock_run_cycle.call_args
        self.assertEqual(kwargs.get("worker_id"), "worker-42")

    def test_run_cycle_ctx_receives_fractional_utc_offset(self):
        """CycleContext carries the exact MaxMind offset through to run_cycle."""
        mock_run_cycle, _, _, _ = self._run(mock_offset=5.5)
        _, kwargs = mock_run_cycle.call_args
        self.assertEqual(kwargs["ctx"].utc_offset_hours, 5.5)

    def test_set_utc_offset_called_with_fractional_value(self):
        """Temporal ContextVar receives the exact MaxMind offset."""
        mock_run_cycle, _, _, mock_set_utc_offset = self._run(mock_offset=-3.5)
        mock_set_utc_offset.assert_called_once_with(-3.5)
        mock_run_cycle.assert_called_once()

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
        _, mock_task_source, _, _ = self._run(worker_id="worker-99")
        mock_task_source.assert_called_once_with("worker-99")

    def test_maxmind_lookup_zip_receives_detected_ip_address(self):
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
            patch("integration.orchestrator.run_cycle", return_value=("complete", None, None)),
        ):
            from integration.worker_task import make_task_fn
            make_task_fn(task_source=MagicMock(return_value=task))("w")
        mock_zip_lookup.assert_called_once_with("203.0.113.5")


class TestMakeTaskFnPersonaInjection(unittest.TestCase):
    """GivexDriver must receive a PersonaProfile derived from worker_id (Layer 2).

    When ``persona is None`` in GivexDriver, 4x4 card pattern, ghost-cursor,
    temporal night-factor, and biometric profile are all bypassed.  The
    production task_fn must therefore always pass a persona so Layer 2
    anti-detection stays active in production.
    """

    def _run(self, worker_id="worker-1"):
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
            patch(
                "modules.cdp.driver.GivexDriver",
                return_value=givex_drv,
            ) as mock_givex_cls,
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            from integration.worker_task import make_task_fn
            make_task_fn()(worker_id)
        return mock_givex_cls, selenium_drv

    def test_givex_driver_called_with_persona(self):
        """GivexDriver must be constructed with a non-None persona kwarg."""
        mock_givex_cls, selenium_drv = self._run()
        mock_givex_cls.assert_called_once()
        args, kwargs = mock_givex_cls.call_args
        self.assertEqual(args[0], selenium_drv)
        persona = kwargs.get("persona")
        self.assertIsNotNone(persona, "GivexDriver must receive a persona (Layer 2)")
        from modules.delay.persona import PersonaProfile
        self.assertIsInstance(persona, PersonaProfile)

    def test_persona_seed_is_deterministic_from_worker_id(self):
        """Same worker_id → same persona seed (matches runtime.start_worker)."""
        import zlib
        mock_givex_cls, _ = self._run(worker_id="worker-7")
        _, kwargs = mock_givex_cls.call_args
        persona = kwargs["persona"]
        expected_seed = zlib.crc32(b"worker-7") & 0xFFFFFFFF
        self.assertEqual(persona._seed, expected_seed)

    def test_real_givex_driver_has_layer2_components_active(self):
        """Production path must build a real GivexDriver with Layer 2 active.

        Regression guard: patching ``GivexDriver`` only proves a ``persona``
        kwarg is passed. This test runs without patching the constructor and
        asserts the persona-backed components (``_bio``, ``_engine``,
        ``_temporal``, ``_cursor``) are non-None, which is exactly what
        bypasses 4x4 card pattern, ghost-cursor, temporal night-factor and
        biometric profile when persona is ``None``.
        """
        selenium_drv = _make_selenium_driver(pid=None)
        bb_client = _make_bitbrowser_client()
        captured: list = []

        def _capture_register(_wid, drv):
            captured.append(drv)

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=bb_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("integration.worker_task.cdp") as mock_cdp,
            patch("integration.runtime.probe_cdp_listener_support"),
            # Geo-check is now invoked from worker_task right after the CDP
            # listener probe (Blueprint §2).  This regression guard cares
            # only about Layer 2 wiring, so stub the geo-check on the real
            # GivexDriver class to avoid touching the mock selenium driver.
            patch.object(
                __import__("modules.cdp.driver", fromlist=["GivexDriver"]).GivexDriver,
                "preflight_geo_check",
                return_value="US",
            ),
        ):
            mock_cdp.register_driver.side_effect = _capture_register
            from integration.worker_task import make_task_fn
            make_task_fn()("worker-layer2")

        self.assertEqual(len(captured), 1, "register_driver must be called once")
        real_driver = captured[0]
        from modules.cdp.driver import GivexDriver
        self.assertIsInstance(real_driver, GivexDriver)
        # Layer 2 components must all be active (non-None) in production.
        self.assertIsNotNone(real_driver._engine, "DelayEngine must be active")
        self.assertIsNotNone(real_driver._temporal, "TemporalModel must be active")
        self.assertIsNotNone(real_driver._bio, "BiometricProfile must be active")
        self.assertIsNotNone(real_driver._cursor, "GhostCursor must be active")


# ── Geo-check ordering tests (immediately after BitBrowserSession.__enter__) ──


class TestMakeTaskFnGeoCheckOrdering(unittest.TestCase):
    """preflight_geo_check must run immediately after the CDP listener probe,
    BEFORE MaxMind/zip resolution, persona work, or run_cycle.

    See issue: "Geo-check should run immediately after BitBrowserSession.__enter__
    (not inside run_preflight_and_fill)".
    """

    def _build_call_log(self, geo_side_effect=None, task_source=None):
        """Run task_fn with patches that record the order of relevant calls."""
        call_log: list[str] = []

        bb_client = _make_bitbrowser_client()
        selenium_drv = _make_selenium_driver(pid=12345)
        givex_drv = MagicMock()

        def _record(name):
            def _fn(*_a, **_kw):
                call_log.append(name)
                return None
            return _fn

        givex_drv.preflight_geo_check.side_effect = (
            geo_side_effect
            if geo_side_effect is not None
            else lambda: (call_log.append("preflight_geo_check"), "US")[-1]
        )

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
                side_effect=_record("probe_cdp_listener"),
            ),
            patch(
                "integration.worker_task._get_current_ip_best_effort",
                side_effect=_record("get_current_ip"),
            ),
            patch(
                "integration.worker_task.maxmind_lookup_zip",
                side_effect=_record("maxmind_lookup_zip"),
            ),
            patch(
                "integration.worker_task._lookup_maxmind_utc_offset",
                side_effect=_record("maxmind_utc_offset"),
            ),
        ):
            from integration.worker_task import make_task_fn
            try:
                make_task_fn(task_source=task_source)("worker-geo")
            except RuntimeError:
                pass
        return call_log, bb_client, givex_drv

    def test_geo_check_runs_before_maxmind(self):
        """preflight_geo_check runs immediately after probe, before MaxMind work."""
        call_log, _, _ = self._build_call_log()
        self.assertIn("probe_cdp_listener", call_log)
        self.assertIn("preflight_geo_check", call_log)
        # MaxMind helpers run after geo-check on the success path.
        self.assertIn("get_current_ip", call_log)
        self.assertLess(
            call_log.index("probe_cdp_listener"),
            call_log.index("preflight_geo_check"),
            "geo-check must run after the CDP listener probe",
        )
        self.assertLess(
            call_log.index("preflight_geo_check"),
            call_log.index("get_current_ip"),
            "geo-check must run BEFORE MaxMind IP resolution",
        )

    def test_geo_check_failure_aborts_before_maxmind_and_run_cycle(self):
        """Non-US geo-check raises before MaxMind / run_cycle work runs."""
        task_source = MagicMock()
        # Geo-check raising propagates out of the worker_task body BEFORE
        # MaxMind helpers are invoked and BEFORE run_cycle is dispatched.
        # We therefore don't need to patch ``importlib.import_module`` —
        # that code path is never reached.
        call_log, bb_client, givex_drv = self._build_call_log(
            geo_side_effect=RuntimeError("Geo-check failed: got 'CA'"),
            task_source=task_source,
        )

        # Geo-check was attempted; MaxMind work was NOT.
        givex_drv.preflight_geo_check.assert_called_once()
        self.assertNotIn("get_current_ip", call_log)
        self.assertNotIn("maxmind_lookup_zip", call_log)
        # task_source must not have been touched (run_cycle never reached).
        task_source.assert_not_called()
        # POOL-NO-DELETE in legacy mode means close + delete still run via
        # BitBrowserSession.__exit__; verify close_profile was called so the
        # session was released (not leaked).
        bb_client.close_profile.assert_called_once()


class TestMakeTaskFnGeoCheckPropagatesPoolNoDelete(unittest.TestCase):
    """Geo-check failure must propagate so BitBrowserSession.__exit__ runs.

    In pool mode (POOL-NO-DELETE) ``__exit__`` releases the profile via
    ``release_profile`` (which posts ``/browser/close``) and never calls
    ``delete_profile``.  This test asserts pool-mode release semantics.
    """

    def test_pool_mode_releases_without_delete_on_geo_failure(self):
        from modules.cdp.fingerprint import (
            BitBrowserPoolClient,
            BitBrowserSession,
        )

        # Construct a pool-mode-capable client mock (spec → isinstance check
        # in BitBrowserSession flips ``_pool_mode`` to True).
        pool_client = MagicMock(spec=BitBrowserPoolClient)
        pool_client.acquire_profile.return_value = "pool-profile-1"
        pool_client.launch_profile.return_value = {
            "webdriver": "ws://127.0.0.1:9222/pool"
        }

        selenium_drv = _make_selenium_driver(pid=99)
        givex_drv = MagicMock()
        givex_drv.preflight_geo_check.side_effect = RuntimeError(
            "Geo-check failed: expected 'US', got 'GB'"
        )

        with (
            patch(
                "integration.worker_task.get_bitbrowser_client",
                return_value=pool_client,
            ),
            patch(
                "integration.worker_task._build_remote_driver",
                return_value=selenium_drv,
            ),
            patch("modules.cdp.driver.GivexDriver", return_value=givex_drv),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
        ):
            # Sanity: BitBrowserSession detects pool mode for this client —
            # if pool detection breaks, POOL-NO-DELETE assertions become
            # vacuous, so guard against that here.
            self.assertTrue(
                BitBrowserSession(pool_client)._pool_mode,
                "Test fixture did not flip BitBrowserSession into pool mode",
            )

            from integration.worker_task import make_task_fn
            with self.assertRaises(RuntimeError):
                make_task_fn()("worker-geo-pool")

        # Geo-check was the one attempted (no MaxMind/run_cycle work).
        givex_drv.preflight_geo_check.assert_called_once()
        # POOL-NO-DELETE: profile released via release_profile, never deleted.
        pool_client.release_profile.assert_called_once()
        self.assertFalse(
            pool_client.delete_profile.called,
            "POOL-NO-DELETE violated: delete_profile was called on geo-check failure",
        )


# ── De-duplication: orchestrator skips geo-check when worker_task ran it ─────


class TestRunPreflightAndFillGeoDedupe(unittest.TestCase):
    """When ``_geo_checked_this_cycle`` is True, run_preflight_and_fill skips
    the redundant ``preflight_geo_check`` call.
    """

    def test_skips_geo_check_when_flag_true(self):
        import modules.cdp.main as cdp_main

        driver = MagicMock()
        driver._geo_checked_this_cycle = True
        cdp_main.register_driver("dedupe-worker", driver)
        try:
            profile = MagicMock()
            profile.email = "x@example.com"
            task = MagicMock()
            cdp_main.run_preflight_and_fill(task, profile, "dedupe-worker")
            driver.preflight_geo_check.assert_not_called()
            driver.navigate_to_egift.assert_called_once()
        finally:
            cdp_main.unregister_driver("dedupe-worker")

    def test_runs_geo_check_when_flag_absent(self):
        """Stub drivers without the flag fall through to running geo-check."""
        import modules.cdp.main as cdp_main

        # Plain object → ``_geo_checked_this_cycle`` not present.
        class _Stub:
            def __init__(self):
                self.calls = []

            def preflight_geo_check(self):
                self.calls.append("geo")

            def navigate_to_egift(self):
                self.calls.append("nav")

            def fill_egift_form(self, _t, _p):
                self.calls.append("fill")

            def add_to_cart_and_checkout(self):
                self.calls.append("cart")

            def select_guest_checkout(self, _e):
                self.calls.append("guest")

            def fill_payment_and_billing(self, _c, _p):
                self.calls.append("pay")

        stub = _Stub()
        cdp_main.register_driver("dedupe-worker-2", stub)
        try:
            profile = MagicMock()
            profile.email = "x@example.com"
            task = MagicMock()
            cdp_main.run_preflight_and_fill(task, profile, "dedupe-worker-2")
            self.assertEqual(stub.calls[0], "geo")
        finally:
            cdp_main.unregister_driver("dedupe-worker-2")


if __name__ == "__main__":
    unittest.main()
