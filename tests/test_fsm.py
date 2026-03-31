import unittest

from modules.fsm.main import add_new_state, _states


class AddNewStateTests(unittest.TestCase):
    def setUp(self):
        _states.clear()

    def test_add_valid_state(self):
        self.assertTrue(add_new_state("valid_state"))

    def test_add_duplicate_state(self):
        self.assertTrue(add_new_state("duplicate"))
        self.assertFalse(add_new_state("duplicate"))

    def test_add_empty_string(self):
        self.assertFalse(add_new_state(""))

    def test_add_none(self):
        self.assertFalse(add_new_state(None))

    def test_add_invalid_characters(self):
        self.assertFalse(add_new_state("state!"))

    def test_add_initial_case_insensitive(self):
        self.assertFalse(add_new_state("initial"))
        self.assertFalse(add_new_state("INITIAL"))
        self.assertFalse(add_new_state("Initial"))

    def test_add_final(self):
        self.assertFalse(add_new_state("final"))

    def test_add_error(self):
        self.assertFalse(add_new_state("error"))

    def test_add_after_another_state(self):
        self.assertTrue(add_new_state("first_state"))
        self.assertTrue(add_new_state("second_state"))
        self.assertFalse(add_new_state("first_state"))


if __name__ == "__main__":
    unittest.main()
# trigger CI