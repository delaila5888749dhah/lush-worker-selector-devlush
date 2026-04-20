"""C5 — Payment-step watchdog uses Blueprint §5's 10s timeout."""
import os
import unittest
from unittest import mock

from integration import orchestrator
from modules.common.exceptions import SessionFlaggedError
from modules.watchdog import main as watchdog


class TestPaymentWatchdogTimeout(unittest.TestCase):
    """Tests deliberately avoid ``importlib.reload(orchestrator)`` — reloading
    replaces module-level globals (e.g. ``_idempotency_lock``, ``_idempotency_store``)
    that other modules/tests have already imported by reference, which corrupts
    state in subsequent tests (e.g. ``test_runtime.TestStartupConfigValidation``).

    Instead, we call the pure helper ``_load_payment_watchdog_timeout()`` directly
    to validate env-var parsing without mutating the orchestrator module.
    """

    def setUp(self):
        # Snapshot env var so tearDown can restore it exactly.
        self._prev_env = os.environ.get("PAYMENT_WATCHDOG_TIMEOUT_S")
        if self._prev_env is not None:
            os.environ.pop("PAYMENT_WATCHDOG_TIMEOUT_S", None)

    def tearDown(self):
        # Always restore the env var to its pre-test value.
        if self._prev_env is None:
            os.environ.pop("PAYMENT_WATCHDOG_TIMEOUT_S", None)
        else:
            os.environ["PAYMENT_WATCHDOG_TIMEOUT_S"] = self._prev_env

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
        """PAYMENT_WATCHDOG_TIMEOUT_S overrides the default (pure helper)."""
        with mock.patch.dict(os.environ, {"PAYMENT_WATCHDOG_TIMEOUT_S": "7.5"}):
            self.assertEqual(orchestrator._load_payment_watchdog_timeout(), 7.5)

    def test_env_override_empty_returns_default(self):
        """Missing or empty override returns the 10s default."""
        os.environ.pop("PAYMENT_WATCHDOG_TIMEOUT_S", None)
        self.assertEqual(orchestrator._load_payment_watchdog_timeout(), 10.0)
        with mock.patch.dict(os.environ, {"PAYMENT_WATCHDOG_TIMEOUT_S": "   "}):
            self.assertEqual(orchestrator._load_payment_watchdog_timeout(), 10.0)

    def test_env_override_invalid_falls_back_to_default(self):
        """Non-numeric or non-positive overrides keep the 10s default."""
        for bad in ("abc", "0", "-5"):
            with mock.patch.dict(os.environ, {"PAYMENT_WATCHDOG_TIMEOUT_S": bad}):
                self.assertEqual(orchestrator._load_payment_watchdog_timeout(), 10.0)

    def test_timeout_raises_SessionFlaggedError(self):
        """wait_for_total raises SessionFlaggedError on timeout."""
        wid = "test-payment-timeout-worker"
        watchdog.enable_network_monitor(wid)
        with self.assertRaises(SessionFlaggedError):
            watchdog.wait_for_total(wid, timeout=0.05)


if __name__ == "__main__":
    unittest.main()

