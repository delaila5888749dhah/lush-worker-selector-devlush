"""PR-4 L9 — Graceful SIGTERM drain test."""
import signal
import unittest
from unittest.mock import patch

from integration import runtime


class SigtermDrainTests(unittest.TestCase):
    def test_sigterm_handler_invokes_stop_with_timeout(self):
        """_handle_shutdown calls runtime.stop(timeout=_WORKER_TIMEOUT)."""
        with patch.object(runtime, "stop") as stop_mock:
            runtime._handle_shutdown(signal.SIGTERM, None)
        stop_mock.assert_called_once()
        kwargs = stop_mock.call_args.kwargs
        # stop may be called positionally in some branches; check either.
        timeout = kwargs.get("timeout") or (
            stop_mock.call_args.args[0] if stop_mock.call_args.args else None
        )
        self.assertEqual(timeout, runtime._WORKER_TIMEOUT)

    def test_register_signal_handlers_binds_sigterm_and_sigint(self):
        """register_signal_handlers installs handlers for SIGTERM + SIGINT."""
        with patch("signal.signal") as sigmock, \
             patch("atexit.register"):
            runtime.register_signal_handlers()
        # Called at least for SIGTERM and SIGINT.
        signums = [call.args[0] for call in sigmock.call_args_list]
        self.assertIn(signal.SIGTERM, signums)
        self.assertIn(signal.SIGINT, signums)


if __name__ == "__main__":
    unittest.main()
