"""PR-10 hardening tests — F-11 (FSM_ALLOW_LEGACY) and F-12 (/tmp billing guard)."""

import os
import unittest
from unittest.mock import patch

# Tests legitimately access module internals (_pool_dir) and use hardcoded
# /tmp test paths to validate the production-mode guard. They are not
# performing any real filesystem operation under /tmp.
# pylint: disable=protected-access

from modules.billing import main as billing
from modules.fsm.main import (
    add_new_state,
    get_current_state,
    reset_states,
    transition_to,
)


# ---------------------------------------------------------------------------
# F-11 — FSM_ALLOW_LEGACY gate
# ---------------------------------------------------------------------------


class TestFsmLegacyGateDisabled(unittest.TestCase):
    """Legacy FSM calls raise RuntimeError when FSM_ALLOW_LEGACY is not enabled."""

    @staticmethod
    def _clear_legacy_flag():
        """Return an env dict without FSM_ALLOW_LEGACY set."""
        return {k: v for k, v in os.environ.items() if k != "FSM_ALLOW_LEGACY"}

    def test_reset_states_raises_when_flag_off(self):
        """reset_states() must raise RuntimeError when FSM_ALLOW_LEGACY is unset."""
        with patch.dict(os.environ, self._clear_legacy_flag(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                reset_states()
        self.assertIn("FSM_ALLOW_LEGACY", str(ctx.exception))

    def test_add_new_state_raises_when_flag_off(self):
        """add_new_state() must raise RuntimeError when FSM_ALLOW_LEGACY is unset."""
        with patch.dict(os.environ, self._clear_legacy_flag(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                add_new_state("success")
        self.assertIn("FSM_ALLOW_LEGACY", str(ctx.exception))

    def test_transition_to_raises_when_flag_off(self):
        """transition_to() must raise RuntimeError when FSM_ALLOW_LEGACY is unset."""
        with patch.dict(os.environ, self._clear_legacy_flag(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                transition_to("success")
        self.assertIn("FSM_ALLOW_LEGACY", str(ctx.exception))

    def test_get_current_state_raises_when_flag_off(self):
        """get_current_state() must raise RuntimeError when FSM_ALLOW_LEGACY is unset."""
        with patch.dict(os.environ, self._clear_legacy_flag(), clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                get_current_state()
        self.assertIn("FSM_ALLOW_LEGACY", str(ctx.exception))

    def test_flag_explicitly_set_to_zero_raises(self):
        """FSM_ALLOW_LEGACY=0 must still disable the legacy API."""
        with patch.dict(os.environ, {"FSM_ALLOW_LEGACY": "0"}):
            with self.assertRaises(RuntimeError):
                reset_states()

    def test_flag_explicitly_set_to_false_raises(self):
        """FSM_ALLOW_LEGACY=false must still disable the legacy API."""
        with patch.dict(os.environ, {"FSM_ALLOW_LEGACY": "false"}):
            with self.assertRaises(RuntimeError):
                reset_states()


class TestFsmLegacyGateEnabled(unittest.TestCase):
    """Legacy FSM calls succeed (with warnings) when FSM_ALLOW_LEGACY=1."""

    def setUp(self):
        self._patch = patch.dict(os.environ, {"FSM_ALLOW_LEGACY": "1"})
        self._patch.start()
        with self.assertLogs("modules.fsm.main", level="WARNING"):
            reset_states()

    def tearDown(self):
        self._patch.stop()

    def test_legacy_api_works_when_flag_on(self):
        """Legacy API must succeed when FSM_ALLOW_LEGACY=1, emitting a warning."""
        with self.assertLogs("modules.fsm.main", level="WARNING") as log_ctx:
            add_new_state("success")
        self.assertTrue(any("add_new_state" in m for m in log_ctx.output))

    def test_flag_true_enables_legacy(self):
        """FSM_ALLOW_LEGACY=true must enable the legacy API."""
        with patch.dict(os.environ, {"FSM_ALLOW_LEGACY": "true"}):
            with self.assertLogs("modules.fsm.main", level="WARNING"):
                reset_states()

    def test_flag_yes_enables_legacy(self):
        """FSM_ALLOW_LEGACY=yes must enable the legacy API."""
        with patch.dict(os.environ, {"FSM_ALLOW_LEGACY": "yes"}):
            with self.assertLogs("modules.fsm.main", level="WARNING"):
                reset_states()


# ---------------------------------------------------------------------------
# F-12 — /tmp billing pool guard
# ---------------------------------------------------------------------------


class TestBillingTmpGuardProduction(unittest.TestCase):
    """/tmp BILLING_POOL_DIR paths are rejected when ENABLE_PRODUCTION_TASK_FN is on."""

    def setUp(self):
        self._prod_patch = patch.dict(os.environ, {"ENABLE_PRODUCTION_TASK_FN": "1"})
        self._prod_patch.start()

    def tearDown(self):
        self._prod_patch.stop()

    def test_tmp_path_rejected_in_production(self):
        """/tmp/... BILLING_POOL_DIR falls back to default billing_pool in production."""
        with patch.dict(os.environ, {"BILLING_POOL_DIR": "/tmp/billing"}):  # nosec B108
            with self.assertLogs("modules.billing.main", level="WARNING") as log_ctx:
                result = billing._pool_dir()
        self.assertTrue(str(result).endswith("billing_pool"))
        self.assertFalse(str(result).startswith("/tmp"))
        self.assertTrue(any("production mode" in m for m in log_ctx.output))

    def test_tmp_root_itself_rejected_in_production(self):
        """Bare /tmp as BILLING_POOL_DIR is also rejected in production."""
        with patch.dict(os.environ, {"BILLING_POOL_DIR": "/tmp"}):  # nosec B108
            with self.assertLogs("modules.billing.main", level="WARNING") as log_ctx:
                result = billing._pool_dir()
        self.assertTrue(str(result).endswith("billing_pool"))
        self.assertFalse(str(result).startswith("/tmp"))
        self.assertTrue(any("production mode" in m for m in log_ctx.output))

    def test_data_path_still_allowed_in_production(self):
        """/data/... BILLING_POOL_DIR is still allowed in production (not /tmp)."""
        with patch.dict(os.environ, {"BILLING_POOL_DIR": "/data/billing"}):
            result = billing._pool_dir()
        self.assertTrue(str(result).startswith("/data"))

    def test_tmp_flag_true_rejected(self):
        """ENABLE_PRODUCTION_TASK_FN=true also rejects /tmp."""
        with patch.dict(
            os.environ,
            {"ENABLE_PRODUCTION_TASK_FN": "true", "BILLING_POOL_DIR": "/tmp/x"},  # nosec B108
        ):
            with self.assertLogs("modules.billing.main", level="WARNING") as log_ctx:
                result = billing._pool_dir()
        self.assertTrue(str(result).endswith("billing_pool"))
        self.assertFalse(str(result).startswith("/tmp"))
        self.assertTrue(any("production mode" in m for m in log_ctx.output))


class TestBillingTmpGuardNonProduction(unittest.TestCase):
    """/tmp BILLING_POOL_DIR remains usable outside production mode (dev/test)."""

    @staticmethod
    def _clear_prod_flag():
        return {k: v for k, v in os.environ.items() if k != "ENABLE_PRODUCTION_TASK_FN"}

    def test_tmp_path_allowed_in_dev_mode(self):
        """/tmp/... BILLING_POOL_DIR is accepted when ENABLE_PRODUCTION_TASK_FN is off."""
        with patch.dict(os.environ, self._clear_prod_flag(), clear=True):
            with patch.dict(os.environ, {"BILLING_POOL_DIR": "/tmp/billing_test"}):  # nosec B108
                result = billing._pool_dir()
        self.assertEqual(str(result), "/tmp/billing_test")

    def test_tmp_path_allowed_when_prod_flag_is_off(self):
        """/tmp/... is accepted when ENABLE_PRODUCTION_TASK_FN=0."""
        with patch.dict(
            os.environ,
            {"ENABLE_PRODUCTION_TASK_FN": "0", "BILLING_POOL_DIR": "/tmp/billing_dev"},  # nosec B108
        ):
            result = billing._pool_dir()
        self.assertEqual(str(result), "/tmp/billing_dev")


if __name__ == "__main__":
    unittest.main()
