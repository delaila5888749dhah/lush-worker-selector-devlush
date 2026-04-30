import unittest
from unittest.mock import patch


class AppMainLifecycleTests(unittest.TestCase):
    """Bug 8.1: main thread must NOT return immediately after runtime.start()."""

    def test_main_blocks_while_runtime_is_running(self):
        """app.main() must wait for runtime so main thread does not exit."""
        from app import __main__ as app_main

        with patch.object(app_main, "runtime") as mock_runtime, \
             patch.object(app_main, "_startup_check_geoip"), \
             patch.object(app_main, "_startup_load_billing_pool"):
            mock_runtime.is_production_task_fn_enabled.return_value = False
            mock_runtime.start.return_value = True
            app_main.main()

        mock_runtime.start.assert_called_once()
        self.assertTrue(
            mock_runtime.wait.called or mock_runtime.is_running.called,
            "main() must wait for runtime — it must not return immediately "
            "after runtime.start() (Bug 8.1)",
        )

    def test_keyboard_interrupt_calls_runtime_stop(self):
        """KeyboardInterrupt during runtime wait must call runtime.stop()."""
        from app import __main__ as app_main

        with patch.object(app_main, "runtime") as mock_runtime, \
             patch.object(app_main, "_startup_check_geoip"), \
             patch.object(app_main, "_startup_load_billing_pool"):
            mock_runtime.is_production_task_fn_enabled.return_value = False
            mock_runtime.start.return_value = True
            mock_runtime.wait.side_effect = [KeyboardInterrupt, True]
            app_main.main()

        mock_runtime.stop.assert_called()

    def test_main_returns_when_runtime_failed_to_start(self):
        """If runtime.start() returns False, main() must NOT block forever."""
        from app import __main__ as app_main

        with patch.object(app_main, "runtime") as mock_runtime, \
             patch.object(app_main, "_startup_check_geoip"), \
             patch.object(app_main, "_startup_load_billing_pool"):
            mock_runtime.is_production_task_fn_enabled.return_value = False
            mock_runtime.start.return_value = False
            app_main.main()

        mock_runtime.wait.assert_not_called()
