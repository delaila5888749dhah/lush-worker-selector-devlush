import unittest

from modules.fsm.main import (
    add_new_state,
    get_current_state,
    reset_states,
    transition_to,
)
from spec.schema import InvalidStateError, InvalidTransitionError, State


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


if __name__ == "__main__":
    unittest.main()