import unittest

from modules.fsm.main import add_new_state, reset_states


class AddNewStateTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_add_valid_state(self):
        self.assertTrue(add_new_state("valid_state"))

    def test_add_duplicate_state(self):
        self.assertTrue(add_new_state("duplicate"))
        self.assertFalse(add_new_state("duplicate"))

    def test_add_empty_state(self):
        self.assertFalse(add_new_state(""))

    def test_add_state_with_special_characters(self):
        self.assertFalse(add_new_state("state!"))

    def test_add_initial_state_case_insensitive(self):
        self.assertFalse(add_new_state("INITIAL"))
        self.assertFalse(add_new_state("Initial"))

    def test_add_final_state(self):
        self.assertFalse(add_new_state("final"))

    def test_add_error_state(self):
        self.assertFalse(add_new_state("error"))

    def test_add_state_after_other_state(self):
        self.assertTrue(add_new_state("first_state"))
        self.assertTrue(add_new_state("second_state"))
        self.assertFalse(add_new_state("first_state"))


if __name__ == "__main__":
    unittest.main()