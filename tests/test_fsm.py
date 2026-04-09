import unittest

from modules.fsm.main import (
    add_new_state,
    get_current_state,
    initialize_for_worker,
    cleanup_worker,
    transition_for_worker,
    reset_states,
    transition_to,
)
from modules.common.exceptions import InvalidStateError, InvalidTransitionError
from modules.common.types import State

_WID = "worker-fsm-test"


class FSMTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_add_new_state_returns_state(self):
        result = add_new_state("ui_lock")
        self.assertIsInstance(result, State)
        self.assertEqual(result.name, "ui_lock")

    def test_add_new_state_duplicate_raises_value_error(self):
        add_new_state("success")
        with self.assertRaises(ValueError):
            add_new_state("success")

    def test_add_new_state_invalid_raises_invalid_state_error(self):
        with self.assertRaises(InvalidStateError):
            add_new_state("not_a_real_state")

    def test_transition_to_valid(self):
        add_new_state("success")
        result = transition_to("success")
        self.assertIsInstance(result, State)
        self.assertEqual(result.name, "success")
        self.assertEqual(get_current_state().name, "success")

    def test_transition_to_invalid_raises_invalid_transition_error(self):
        with self.assertRaises(InvalidTransitionError):
            transition_to("ui_lock")

    def test_transition_to_bogus_state_raises_invalid_state_error(self):
        with self.assertRaises(InvalidStateError):
            transition_to("bogus")

    def test_reset_states_clears_all(self):
        add_new_state("ui_lock")
        transition_to("ui_lock")
        reset_states()
        self.assertIsNone(get_current_state())
        with self.assertRaises(InvalidTransitionError):
            transition_to("ui_lock")


class FSMTransitionGraphTests(unittest.TestCase):
    """Verify that transition_for_worker enforces _VALID_PAYMENT_TRANSITIONS."""

    def setUp(self):
        cleanup_worker(_WID)
        initialize_for_worker(_WID)

    def tearDown(self):
        cleanup_worker(_WID)

    def test_valid_flow_ui_lock_to_vbv_3ds_to_success(self):
        """None -> ui_lock -> vbv_3ds -> success must succeed."""
        s = transition_for_worker(_WID, "ui_lock")
        self.assertEqual(s.name, "ui_lock")
        s = transition_for_worker(_WID, "vbv_3ds")
        self.assertEqual(s.name, "vbv_3ds")
        s = transition_for_worker(_WID, "success")
        self.assertEqual(s.name, "success")

    def test_valid_flow_ui_lock_to_success(self):
        """None -> ui_lock -> success must succeed."""
        transition_for_worker(_WID, "ui_lock")
        s = transition_for_worker(_WID, "success")
        self.assertEqual(s.name, "success")

    def test_valid_flow_ui_lock_to_declined(self):
        """None -> ui_lock -> declined must succeed."""
        transition_for_worker(_WID, "ui_lock")
        s = transition_for_worker(_WID, "declined")
        self.assertEqual(s.name, "declined")

    def test_valid_flow_vbv_3ds_to_declined(self):
        """None -> vbv_3ds -> declined must succeed."""
        transition_for_worker(_WID, "vbv_3ds")
        s = transition_for_worker(_WID, "declined")
        self.assertEqual(s.name, "declined")

    def test_none_to_any_allowed_state(self):
        """First transition from None accepts any ALLOWED_STATE."""
        for state_name in ("ui_lock", "success", "vbv_3ds", "declined"):
            cleanup_worker(_WID)
            initialize_for_worker(_WID)
            s = transition_for_worker(_WID, state_name)
            self.assertEqual(s.name, state_name)

    def test_invalid_ui_lock_to_ui_lock(self):
        """ui_lock -> ui_lock is not in the transition graph."""
        transition_for_worker(_WID, "ui_lock")
        with self.assertRaises(ValueError) as ctx:
            transition_for_worker(_WID, "ui_lock")
        self.assertIn("Invalid transition from ui_lock to ui_lock", str(ctx.exception))

    def test_invalid_declined_to_success(self):
        """declined is a terminal state; no outgoing transitions."""
        transition_for_worker(_WID, "declined")
        with self.assertRaises(ValueError) as ctx:
            transition_for_worker(_WID, "success")
        self.assertIn("Invalid transition from declined to success", str(ctx.exception))

    def test_invalid_success_to_vbv_3ds(self):
        """success is a terminal state; no outgoing transitions."""
        transition_for_worker(_WID, "success")
        with self.assertRaises(ValueError) as ctx:
            transition_for_worker(_WID, "vbv_3ds")
        self.assertIn("Invalid transition from success to vbv_3ds", str(ctx.exception))

    def test_invalid_success_to_declined(self):
        """success -> declined is not valid."""
        transition_for_worker(_WID, "success")
        with self.assertRaises(ValueError):
            transition_for_worker(_WID, "declined")

    def test_invalid_declined_to_ui_lock(self):
        """declined -> ui_lock is not valid."""
        transition_for_worker(_WID, "declined")
        with self.assertRaises(ValueError):
            transition_for_worker(_WID, "ui_lock")


if __name__ == "__main__":
    unittest.main()