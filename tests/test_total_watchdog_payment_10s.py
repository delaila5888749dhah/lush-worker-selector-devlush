"""C5 — Payment-step watchdog uses Blueprint §5's 10s timeout."""
import importlib
import os
import unittest
from unittest import mock

from integration import orchestrator
from modules.common.exceptions import SessionFlaggedError
from modules.watchdog import main as watchdog


class TestPaymentWatchdogTimeout(unittest.TestCase):
    def test_module_constants_have_distinct_defaults(self):
        """10s for payment, 30s for default caller."""
        self.assertEqual(orchestrator._WATCHDOG_TIMEOUT_PAYMENT, 10.0)
        self.assertEqual(orchestrator._WATCHDOG_TIMEOUT_DEFAULT, 30)
        self.assertEqual(orchestrator._WATCHDOG_TIMEOUT, 30)

    def test_payment_step_uses_10s_timeout(self):
        """run_payment_step calls wait_for_total with _WATCHDOG_TIMEOUT_PAYMENT."""
        import inspect
        src = inspect.getsource(orchestrator.run_payment_step)
        self.assertIn("_WATCHDOG_TIMEOUT_PAYMENT", src)
        self.assertNotIn(
            "wait_for_total(worker_id, timeout=_WATCHDOG_TIMEOUT)",
            src,
        )

    def test_default_callers_still_use_30s(self):
        """The module contract default remains 30s for non-payment callers."""
        self.assertEqual(orchestrator._WATCHDOG_TIMEOUT_DEFAULT, 30)

    def test_env_override(self):
        """PAYMENT_WATCHDOG_TIMEOUT_S overrides the default."""
        with mock.patch.dict(os.environ, {"PAYMENT_WATCHDOG_TIMEOUT_S": "7.5"}):
            reloaded = importlib.reload(orchestrator)
            try:
                self.assertEqual(reloaded._WATCHDOG_TIMEOUT_PAYMENT, 7.5)
            finally:
                # Restore default environment and reload.
                pass
        # Reload without the override so other tests see the 10s default.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAYMENT_WATCHDOG_TIMEOUT_S", None)
            importlib.reload(orchestrator)

    def test_env_override_invalid_falls_back_to_default(self):
        """Non-numeric or non-positive overrides keep the 10s default."""
        for bad in ("abc", "0", "-5"):
            with mock.patch.dict(os.environ, {"PAYMENT_WATCHDOG_TIMEOUT_S": bad}):
                reloaded = importlib.reload(orchestrator)
                self.assertEqual(reloaded._WATCHDOG_TIMEOUT_PAYMENT, 10.0)
        os.environ.pop("PAYMENT_WATCHDOG_TIMEOUT_S", None)
        importlib.reload(orchestrator)

    def test_timeout_raises_SessionFlaggedError(self):
        """wait_for_total raises SessionFlaggedError on timeout."""
        wid = "test-payment-timeout-worker"
        watchdog.enable_network_monitor(wid)
        with self.assertRaises(SessionFlaggedError):
            watchdog.wait_for_total(wid, timeout=0.05)


if __name__ == "__main__":
    unittest.main()
