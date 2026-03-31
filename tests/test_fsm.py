import unittest
import threading

from spec.schema import State
from modules.fsm.main import (
    add_new_state,
    get_current_state,
    transition_to,
    reset_states,
    ALLOWED_STATES,
)


class AddNewStateTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_add_valid_state_returns_state(self):
        result = add_new_state("ui_lock")
        self.assertIsInstance(result, State)
        self.assertEqual(result.name, "ui_lock")

    def test_add_state_not_in_allowed_raises(self):
        with self.assertRaises(ValueError):
            add_new_state("invalid_state")

    def test_add_duplicate_state_raises(self):
        add_new_state("ui_lock")
        with self.assertRaises(ValueError):
            add_new_state("ui_lock")

    def test_state_is_frozen(self):
        state = add_new_state("success")
        with self.assertRaises(AttributeError):
            state.name = "changed"

    def test_all_allowed_states(self):
        for name in sorted(ALLOWED_STATES):
            result = add_new_state(name)
            self.assertEqual(result.name, name)

    def test_thread_safety(self):
        errors = []

        def worker(name):
            try:
                add_new_state(name)
            except ValueError:
                errors.append(name)

        threads = [threading.Thread(target=worker, args=(s,)) for s in ALLOWED_STATES]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 0)


class GetCurrentStateTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_initial_current_state_is_none(self):
        self.assertIsNone(get_current_state())

    def test_current_state_after_add(self):
        add_new_state("declined")
        result = get_current_state()
        self.assertIsInstance(result, State)
        self.assertEqual(result.name, "declined")


class TransitionToTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_transition_to_existing_state(self):
        add_new_state("ui_lock")
        add_new_state("success")
        result = transition_to("ui_lock")
        self.assertIsInstance(result, State)
        self.assertEqual(result.name, "ui_lock")
        self.assertEqual(get_current_state().name, "ui_lock")

    def test_transition_to_not_allowed_raises(self):
        with self.assertRaises(ValueError):
            transition_to("nonexistent")

    def test_transition_to_unregistered_raises(self):
        with self.assertRaises(ValueError):
            transition_to("ui_lock")


class ResetStatesTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_reset_clears_registry(self):
        add_new_state("ui_lock")
        reset_states()
        self.assertIsNone(get_current_state())
        state = add_new_state("ui_lock")
        self.assertEqual(state.name, "ui_lock")


if __name__ == "__main__":
    unittest.main()