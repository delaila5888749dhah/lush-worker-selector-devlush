import unittest

from ci.check_spec_consistency import (
    _extract_functions,
    check_consistency,
    ROOT_DIR,
)


class ExtractFunctionsTests(unittest.TestCase):
    def test_core_has_fsm_functions(self):
        path = ROOT_DIR / "spec" / "core" / "interface.md"
        funcs = _extract_functions(path)
        self.assertIn("add_new_state", funcs)
        self.assertIn("get_current_state", funcs)
        self.assertIn("transition_to", funcs)
        self.assertIn("reset_states", funcs)

    def test_integration_has_watchdog_billing_cdp(self):
        path = ROOT_DIR / "spec" / "integration" / "interface.md"
        funcs = _extract_functions(path)
        self.assertIn("enable_network_monitor", funcs)
        self.assertIn("wait_for_total", funcs)
        self.assertIn("select_profile", funcs)
        self.assertIn("detect_page_state", funcs)

    def test_aggregated_has_all_functions(self):
        path = ROOT_DIR / "spec" / "interface.md"
        funcs = _extract_functions(path)
        self.assertIn("add_new_state", funcs)
        self.assertIn("enable_network_monitor", funcs)
        self.assertIn("select_profile", funcs)

    def test_nonexistent_file_returns_empty(self):
        path = ROOT_DIR / "spec" / "nonexistent.md"
        funcs = _extract_functions(path)
        self.assertEqual(funcs, {})


class ConsistencyTests(unittest.TestCase):
    def test_current_specs_are_consistent(self):
        """The current spec files should pass consistency checks."""
        errors = check_consistency()
        self.assertEqual(errors, [], f"Spec drift detected: {errors}")


if __name__ == "__main__":
    unittest.main()
