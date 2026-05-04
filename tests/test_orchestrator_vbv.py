import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import integration.orchestrator as orch
from modules.common.exceptions import SessionLostError
from modules.common.types import State


class OrchestratorVbvSessionLossTests(unittest.TestCase):
    def test_vbv_handler_skipped_on_dead_session(self):
        raw = MagicMock()
        raw.execute_cdp_cmd.side_effect = RuntimeError("detached")
        driver = SimpleNamespace(
            _driver=raw,
            handle_vbv_challenge=MagicMock(return_value="cancelled"),
        )

        with patch.object(orch.cdp, "_get_driver", return_value=driver), \
             self.assertLogs("integration.orchestrator", level="ERROR") as logs:
            with self.assertRaises(SessionLostError) as cm:
                orch.handle_outcome(State("vbv_3ds"), (), worker_id="w-dead")

        self.assertEqual(cm.exception.reason, "session_probe_failed_pre_vbv")
        driver.handle_vbv_challenge.assert_not_called()
        self.assertEqual(
            sum("SESSION_LOST reason=session_probe_failed_pre_vbv" in msg for msg in logs.output),
            1,
        )

    def test_vbv_handler_no_retry_on_invalid_session_id(self):
        raw = MagicMock()
        driver = SimpleNamespace(
            _driver=raw,
            _last_cdp_error="invalid session id",
            handle_vbv_challenge=MagicMock(return_value="cdp_fail"),
        )

        with patch.object(orch.cdp, "_get_driver", return_value=driver):
            with self.assertRaises(SessionLostError) as cm:
                orch.handle_outcome(State("vbv_3ds"), (), worker_id="w-invalid")

        self.assertEqual(cm.exception.reason, "invalid_session_id")
        driver.handle_vbv_challenge.assert_called_once()


if __name__ == "__main__":
    unittest.main()
