import threading
import unittest

from spec.schema import State
from modules.fsm.main import (
    add_new_state,
    get_current_state,
    reset_states,
    transition_to,
    _states,
    ALLOWED_STATES,
)


class AddNewStateTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_add_valid_state_returns_state(self):
        result = add_new_state("ui_lock")
        self.assertIsInstance(result, State)
        self.assertEqual(result.name, "ui_lock")

    def test_add_duplicate_state_raises(self):
        add_new_state("success")
        with self.assertRaises(ValueError):
            add_new_state("success")

    def test_add_invalid_state_raises(self):
        with self.assertRaises(ValueError):
            add_new_state("not_a_real_state")

    def test_state_is_frozen(self):
        state = add_new_state("declined")
        with self.assertRaises(AttributeError):
            state.name = "other"

    def test_all_allowed_states(self):
        for name in ALLOWED_STATES:
            state = add_new_state(name)
            self.assertEqual(state.name, name)

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
        self.assertEqual(len(_states), len(ALLOWED_STATES))


class GetCurrentStateTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_initial_state_is_none(self):
        self.assertIsNone(get_current_state())

    def test_after_transition(self):
        add_new_state("ui_lock")
        transition_to("ui_lock")
        self.assertEqual(get_current_state().name, "ui_lock")


class TransitionToTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_transition_returns_state(self):
        add_new_state("success")
        result = transition_to("success")
        self.assertIsInstance(result, State)
        self.assertEqual(result.name, "success")

    def test_transition_invalid_state_raises(self):
        with self.assertRaises(ValueError):
            transition_to("bogus")

    def test_transition_unregistered_state_raises(self):
        with self.assertRaises(ValueError):
            transition_to("ui_lock")


class ResetStatesTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_reset_clears_registry(self):
        add_new_state("ui_lock")
        reset_states()
        self.assertEqual(len(_states), 0)

    def test_reset_clears_current_state(self):
        add_new_state("ui_lock")
        transition_to("ui_lock")
        reset_states()
        self.assertIsNone(get_current_state())


if __name__ == "__main__":
    unittest.main()