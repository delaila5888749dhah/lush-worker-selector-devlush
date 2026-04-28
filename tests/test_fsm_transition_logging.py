"""Unit tests for structured logging inside ``transition_for_worker``.

Ensures that every successful FSM transition emits a grep-able
``FSM_TRANSITION`` INFO record, and that out-of-band transitions
rejected by ``_VALID_PAYMENT_TRANSITIONS`` (or by terminal-state
guard) emit a ``FSM_TRANSITION_REJECTED`` WARN record.
"""

import logging
import unittest

from modules.fsm.main import (
    cleanup_worker,
    initialize_for_worker,
    reset_registry,
    transition_for_worker,
)

_WID = "worker-fsm-log-test"


class FSMTransitionLoggingTests(unittest.TestCase):
    def setUp(self):
        reset_registry()
        initialize_for_worker(_WID)

    def tearDown(self):
        cleanup_worker(_WID)

    def test_successful_transition_emits_structured_info(self):
        with self.assertLogs("modules.fsm.main", level="INFO") as cm:
            transition_for_worker(_WID, "ui_lock", trace_id="abc-123")
        # Find the FSM_TRANSITION record
        matches = [r for r in cm.records if "FSM_TRANSITION " in r.getMessage()
                   and "REJECTED" not in r.getMessage()]
        self.assertEqual(len(matches), 1)
        msg = matches[0].getMessage()
        self.assertEqual(matches[0].levelno, logging.INFO)
        self.assertIn(f"worker_id={_WID}", msg)
        self.assertIn("from=-", msg)  # no prior state
        self.assertIn("to=ui_lock", msg)
        self.assertIn("trace_id=abc-123", msg)

    def test_successful_transition_includes_prev_state(self):
        transition_for_worker(_WID, "ui_lock", trace_id="t1")
        with self.assertLogs("modules.fsm.main", level="INFO") as cm:
            transition_for_worker(_WID, "success", trace_id="t1")
        msg = next(r.getMessage() for r in cm.records
                   if "FSM_TRANSITION " in r.getMessage() and "REJECTED" not in r.getMessage())
        self.assertIn("from=ui_lock", msg)
        self.assertIn("to=success", msg)
        self.assertIn("trace_id=t1", msg)

    def test_trace_id_defaults_to_dash_when_omitted(self):
        with self.assertLogs("modules.fsm.main", level="INFO") as cm:
            transition_for_worker(_WID, "ui_lock")
        msg = next(r.getMessage() for r in cm.records
                   if "FSM_TRANSITION " in r.getMessage() and "REJECTED" not in r.getMessage())
        self.assertIn("trace_id=-", msg)

    def test_out_of_band_transition_emits_warn(self):
        transition_for_worker(_WID, "ui_lock", trace_id="t2")
        # ui_lock -> vbv_cancelled is not in _VALID_PAYMENT_TRANSITIONS[ui_lock]
        with self.assertLogs("modules.fsm.main", level="WARNING") as cm:
            with self.assertRaises(ValueError):
                transition_for_worker(_WID, "vbv_cancelled", trace_id="t2")
        warns = [r for r in cm.records if r.levelno == logging.WARNING
                 and "FSM_TRANSITION_REJECTED" in r.getMessage()]
        self.assertEqual(len(warns), 1)
        msg = warns[0].getMessage()
        self.assertIn(f"worker_id={_WID}", msg)
        self.assertIn("from=ui_lock", msg)
        self.assertIn("to=vbv_cancelled", msg)
        self.assertIn("reason=out_of_band", msg)
        self.assertIn("trace_id=t2", msg)

    def test_terminal_state_rejection_emits_warn(self):
        transition_for_worker(_WID, "ui_lock")
        transition_for_worker(_WID, "success")
        with self.assertLogs("modules.fsm.main", level="WARNING") as cm:
            with self.assertRaises(ValueError):
                transition_for_worker(_WID, "declined", trace_id="term-1")
        warns = [r for r in cm.records if r.levelno == logging.WARNING
                 and "FSM_TRANSITION_REJECTED" in r.getMessage()]
        self.assertEqual(len(warns), 1)
        msg = warns[0].getMessage()
        self.assertIn("from=success", msg)
        self.assertIn("to=declined", msg)
        self.assertIn("reason=terminal", msg)
        self.assertIn("trace_id=term-1", msg)


if __name__ == "__main__":
    unittest.main()
