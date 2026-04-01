import unittest
from unittest.mock import patch, MagicMock
import subprocess

from ci.check_pr_scope import (
    _normalize,
    _is_excluded,
    module_from_path,
    get_numstat,
    check,
    main,
    EXCLUDED_PREFIXES,
    MAX_CHANGED_LINES,
    VALID_CHANGE_CLASSES,
)


class NormalizeTests(unittest.TestCase):
    def test_strip_dot_slash(self):
        self.assertEqual(_normalize("./src/app.py"), "src/app.py")

    def test_backslash(self):
        self.assertEqual(_normalize("ci\\script.py"), "ci/script.py")

    def test_already_normalized(self):
        self.assertEqual(_normalize("modules/fsm/main.py"), "modules/fsm/main.py")


class IsExcludedTests(unittest.TestCase):
    def test_tests_dir(self):
        self.assertTrue(_is_excluded("tests/test_fsm.py"))

    def test_ci_dir(self):
        self.assertTrue(_is_excluded("ci/check_pr_scope.py"))

    def test_modules_not_excluded(self):
        self.assertFalse(_is_excluded("modules/fsm/main.py"))

    def test_spec_not_excluded(self):
        self.assertFalse(_is_excluded("spec/schema.py"))

    def test_root_file(self):
        self.assertFalse(_is_excluded("README.md"))


class ModuleFromPathTests(unittest.TestCase):
    def test_module_path(self):
        self.assertEqual(module_from_path("modules/fsm/main.py"), "fsm")

    def test_non_module_path(self):
        self.assertIsNone(module_from_path("ci/check_pr_scope.py"))

    def test_bare_modules(self):
        self.assertIsNone(module_from_path("modules/"))

    def test_spec(self):
        self.assertIsNone(module_from_path("spec/schema.py"))


class CheckTests(unittest.TestCase):
    """Test the check() function with mocked git output."""

    def _mock_numstat(self, lines: list[str]):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "\n".join(lines) + "\n"
        result.stderr = ""
        return result

    @patch("ci.check_pr_scope.get_numstat")
    def test_pass_under_limit(self, mock_numstat):
        mock_numstat.return_value = [
            (50, 10, "modules/fsm/main.py"),
        ]
        self.assertEqual(check("fake...range"), 0)

    @patch("ci.check_pr_scope.get_numstat")
    def test_fail_over_limit(self, mock_numstat):
        mock_numstat.return_value = [
            (150, 60, "modules/fsm/main.py"),
        ]
        self.assertEqual(check("fake...range"), 1)

    @patch("ci.check_pr_scope.get_numstat")
    def test_excluded_dirs_not_counted(self, mock_numstat):
        mock_numstat.return_value = [
            (50, 10, "modules/fsm/main.py"),
            (300, 100, "tests/test_fsm.py"),
            (200, 50, "ci/check_pr_scope.py"),
        ]
        self.assertEqual(check("fake...range"), 0)

    @patch("ci.check_pr_scope.get_numstat")
    def test_fail_multiple_modules(self, mock_numstat):
        mock_numstat.return_value = [
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/auth/main.py"),
        ]
        self.assertEqual(check("fake...range"), 1)

    @patch("ci.check_pr_scope.get_numstat")
    def test_single_module_pass(self, mock_numstat):
        mock_numstat.return_value = [
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/fsm/helpers.py"),
        ]
        self.assertEqual(check("fake...range"), 0)

    @patch("ci.check_pr_scope.get_numstat")
    def test_exactly_at_limit(self, mock_numstat):
        mock_numstat.return_value = [
            (100, 100, "modules/fsm/main.py"),
        ]
        self.assertEqual(check("fake...range"), 0)

    @patch("ci.check_pr_scope.get_numstat")
    def test_no_changes(self, mock_numstat):
        mock_numstat.return_value = []
        self.assertEqual(check("fake...range"), 0)

    @patch("ci.check_pr_scope.get_numstat")
    def test_only_excluded_changes(self, mock_numstat):
        mock_numstat.return_value = [
            (500, 200, "ci/check_pr_scope.py"),
            (300, 100, "tests/test_check_pr_scope.py"),
        ]
        self.assertEqual(check("fake...range"), 0)


class ConstantsTests(unittest.TestCase):
    def test_max_lines(self):
        self.assertEqual(MAX_CHANGED_LINES, 200)

    def test_excluded_prefixes(self):
        self.assertIn("tests/", EXCLUDED_PREFIXES)
        self.assertIn("ci/", EXCLUDED_PREFIXES)


class AllowMultiModuleTests(unittest.TestCase):
    """Test the ALLOW_MULTI_MODULE env var bypass."""

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"ALLOW_MULTI_MODULE": "true"})
    def test_bypass_allows_multi_module(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/watchdog/main.py"),
            (10, 5, "modules/billing/main.py"),
            (10, 5, "modules/cdp/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"ALLOW_MULTI_MODULE": "true"})
    def test_bypass_still_enforces_line_limit(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (150, 60, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"ALLOW_MULTI_MODULE": "false"})
    def test_no_bypass_when_false(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/watchdog/main.py"),
        ]
        self.assertEqual(main(), 1)


class ChangeClassTests(unittest.TestCase):
    """Test the CHANGE_CLASS exception framework."""

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"CHANGE_CLASS": "emergency_override"}, clear=False)
    def test_emergency_override_bypasses_all(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (200, 100, "modules/fsm/main.py"),
            (200, 100, "modules/watchdog/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"CHANGE_CLASS": "spec_sync"}, clear=False)
    def test_spec_sync_bypasses_module_limit(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/watchdog/main.py"),
            (10, 5, "modules/billing/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"CHANGE_CLASS": "spec_sync"}, clear=False)
    def test_spec_sync_still_enforces_line_limit(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (150, 60, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"CHANGE_CLASS": "infra_change"}, clear=False)
    def test_infra_change_bypasses_line_limit(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (300, 200, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"CHANGE_CLASS": "infra_change"}, clear=False)
    def test_infra_change_still_enforces_module_limit(self, mock_resolve, mock_numstat):
        mock_numstat.return_value = [
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/watchdog/main.py"),
        ]
        self.assertEqual(main(), 1)

    @patch.dict("os.environ", {"CHANGE_CLASS": "invalid_class"}, clear=False)
    def test_invalid_change_class_fails(self):
        self.assertEqual(main(), 1)

    def test_valid_change_classes_constant(self):
        self.assertIn("emergency_override", VALID_CHANGE_CLASSES)
        self.assertIn("spec_sync", VALID_CHANGE_CLASSES)
        self.assertIn("infra_change", VALID_CHANGE_CLASSES)


if __name__ == "__main__":
    unittest.main()
