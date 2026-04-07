import os
import unittest
from unittest.mock import patch, MagicMock
import subprocess

import tempfile

from ci.check_pr_scope import (
    _normalize,
    _is_excluded,
    _parse_labels,
    _resolve_change_class,
    _check_authorization,
    _export_to_github_env,
    module_from_path,
    get_numstat,
    check,
    main,
    EXCLUDED_PREFIXES,
    MAX_CHANGED_LINES,
    MODULE_MINOR_THRESHOLD,
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

    def test_spec_excluded(self):
        self.assertTrue(_is_excluded("spec/schema.py"))

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


class ParseLabelsTests(unittest.TestCase):
    """Test _parse_labels — security-critical exact match parsing."""

    def test_single_label(self):
        self.assertEqual(_parse_labels("approved-override"), {"approved-override"})

    def test_multiple_labels(self):
        result = _parse_labels("approved-override,bug,critical")
        self.assertEqual(result, {"approved-override", "bug", "critical"})

    def test_whitespace_stripped(self):
        result = _parse_labels("  approved-override , bug ")
        self.assertEqual(result, {"approved-override", "bug"})

    def test_case_normalized(self):
        result = _parse_labels("Approved-Override,BUG")
        self.assertEqual(result, {"approved-override", "bug"})

    def test_empty_string(self):
        self.assertEqual(_parse_labels(""), set())

    def test_empty_entries_ignored(self):
        result = _parse_labels("approved-override,,,,bug")
        self.assertEqual(result, {"approved-override", "bug"})

    def test_exact_match_security(self):
        """Substring 'approved-override' inside longer label must NOT match."""
        labels = _parse_labels("not-approved-override")
        self.assertNotIn("approved-override", labels)
        self.assertIn("not-approved-override", labels)

    def test_suffix_attack(self):
        """'approved-override-requested' must NOT grant access."""
        labels = _parse_labels("approved-override-requested")
        self.assertNotIn("approved-override", labels)


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
            (20, 10, "modules/fsm/main.py"),
            (20, 10, "modules/auth/main.py"),
        ]
        self.assertEqual(check("fake...range"), 1)

    @patch("ci.check_pr_scope.get_numstat")
    def test_minor_module_excluded_from_count(self, mock_numstat):
        """A module with ≤ MODULE_MINOR_THRESHOLD lines is incidental."""
        mock_numstat.return_value = [
            (50, 50, "modules/watchdog/main.py"),  # 100 lines — primary
            (5, 3, "modules/delay/wrapper.py"),     # 8 lines — minor
        ]
        self.assertEqual(check("fake...range"), 0)

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



class ResolveChangeClassTests(unittest.TestCase):
    """Test _resolve_change_class — explicit env, title auto-detect, default."""

    @patch.dict("os.environ", {"CHANGE_CLASS": "spec_sync"}, clear=True)
    def test_explicit_env_takes_priority(self):
        self.assertEqual(_resolve_change_class(), "spec_sync")

    @patch.dict("os.environ", {"CHANGE_CLASS": "spec_sync", "PR_TITLE": "[emergency] hotfix"}, clear=True)
    def test_explicit_env_overrides_title(self):
        """Explicit CHANGE_CLASS env var always wins over title pattern."""
        self.assertEqual(_resolve_change_class(), "spec_sync")

    @patch.dict("os.environ", {"CHANGE_CLASS": "", "PR_TITLE": "[spec-sync] update interfaces"}, clear=True)
    def test_title_spec_sync_detected(self):
        self.assertEqual(_resolve_change_class(), "spec_sync")

    @patch.dict("os.environ", {"CHANGE_CLASS": "", "PR_TITLE": "[emergency] hotfix"}, clear=True)
    def test_title_emergency_detected(self):
        self.assertEqual(_resolve_change_class(), "emergency_override")

    @patch.dict("os.environ", {"CHANGE_CLASS": "", "PR_TITLE": "[infra] update CI"}, clear=True)
    def test_title_infra_detected(self):
        self.assertEqual(_resolve_change_class(), "infra_change")

    @patch.dict("os.environ", {"CHANGE_CLASS": "", "PR_TITLE": "[SPEC-SYNC] case insensitive"}, clear=True)
    def test_title_detection_case_insensitive(self):
        self.assertEqual(_resolve_change_class(), "spec_sync")

    @patch.dict("os.environ", {"CHANGE_CLASS": "", "PR_TITLE": "simple change"}, clear=True)
    def test_no_pattern_defaults_to_normal(self):
        self.assertEqual(_resolve_change_class(), "normal")

    @patch.dict("os.environ", {"CHANGE_CLASS": ""}, clear=True)
    def test_empty_change_class_defaults_to_normal(self):
        self.assertEqual(_resolve_change_class(), "normal")

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_change_class_defaults_to_normal(self):
        self.assertEqual(_resolve_change_class(), "normal")


class AuthorizationTests(unittest.TestCase):
    """Test _check_authorization — override requires approval signal."""

    def test_normal_needs_no_authorization(self):
        self.assertEqual(_check_authorization("normal"), [])

    @patch.dict("os.environ", {"PR_LABELS": "", "CHANGE_CLASS_APPROVED": "", "PR_REVIEW_STATE": "", "ALLOW_SPEC_MODIFICATION": ""}, clear=True)
    def test_spec_sync_without_approval_fails(self):
        """spec_sync requires authorization per AI_CONTEXT.md §6."""
        errors = _check_authorization("spec_sync")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires explicit authorization", errors[0])

    @patch.dict("os.environ", {"PR_LABELS": "approved-override", "PR_REVIEW_STATE": ""}, clear=True)
    def test_spec_sync_with_label_passes(self):
        self.assertEqual(_check_authorization("spec_sync"), [])

    @patch.dict("os.environ", {"PR_LABELS": "", "CHANGE_CLASS_APPROVED": "", "ALLOW_SPEC_MODIFICATION": "true"}, clear=True)
    def test_spec_sync_with_allow_spec_modification_passes(self):
        """ALLOW_SPEC_MODIFICATION=true authorizes spec_sync (consistent with check_spec_lock/meta_audit)."""
        self.assertEqual(_check_authorization("spec_sync"), [])

    @patch.dict("os.environ", {"PR_LABELS": "", "CHANGE_CLASS_APPROVED": "", "ALLOW_SPEC_MODIFICATION": "true"}, clear=True)
    def test_allow_spec_modification_does_not_authorize_other_classes(self):
        """ALLOW_SPEC_MODIFICATION only applies to spec_sync, not other change classes."""
        errors = _check_authorization("infra_change")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires explicit authorization", errors[0])

    @patch.dict("os.environ", {"PR_LABELS": "", "CHANGE_CLASS_APPROVED": "", "PR_REVIEW_STATE": ""}, clear=True)
    def test_override_without_approval_fails(self):
        errors = _check_authorization("infra_change")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires explicit authorization", errors[0])

    @patch.dict("os.environ", {"PR_LABELS": "approved-override,bug", "PR_REVIEW_STATE": ""}, clear=True)
    def test_pr_label_grants_authorization(self):
        self.assertEqual(_check_authorization("infra_change"), [])

    @patch.dict("os.environ", {"CHANGE_CLASS_APPROVED": "true", "PR_LABELS": "", "PR_REVIEW_STATE": ""}, clear=True)
    def test_admin_approved_grants_authorization(self):
        self.assertEqual(_check_authorization("infra_change"), [])

    @patch.dict("os.environ", {"PR_LABELS": "Approved-Override", "PR_REVIEW_STATE": ""}, clear=True)
    def test_label_check_is_case_insensitive(self):
        self.assertEqual(_check_authorization("infra_change"), [])

    @patch.dict("os.environ", {"PR_LABELS": "approved-override", "PR_REVIEW_STATE": ""}, clear=True)
    def test_emergency_without_review_fails(self):
        errors = _check_authorization("emergency_override")
        self.assertEqual(len(errors), 1)
        self.assertIn("APPROVED review", errors[0])

    @patch.dict("os.environ", {"PR_LABELS": "approved-override", "PR_REVIEW_STATE": "APPROVED"}, clear=True)
    def test_emergency_with_review_passes(self):
        self.assertEqual(_check_authorization("emergency_override"), [])

    @patch.dict("os.environ", {"PR_LABELS": "", "CHANGE_CLASS_APPROVED": "", "PR_REVIEW_STATE": "APPROVED"}, clear=True)
    def test_emergency_without_label_or_admin_fails(self):
        """Even with review, still needs label or admin approval."""
        errors = _check_authorization("emergency_override")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires explicit authorization", errors[0])

    @patch.dict("os.environ", {"PR_LABELS": "not-approved-override", "PR_REVIEW_STATE": ""}, clear=True)
    def test_substring_attack_rejected(self):
        """Label 'not-approved-override' must NOT grant access (exact match)."""
        errors = _check_authorization("infra_change")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires explicit authorization", errors[0])

    @patch.dict("os.environ", {"PR_LABELS": "approved-override-requested", "PR_REVIEW_STATE": ""}, clear=True)
    def test_suffix_attack_rejected(self):
        """Label 'approved-override-requested' must NOT grant access."""
        errors = _check_authorization("infra_change")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires explicit authorization", errors[0])


class ChangeClassIntegrationTests(unittest.TestCase):
    """End-to-end tests for CHANGE_CLASS through main()."""

    @patch("ci.check_pr_scope._get_changed_files", return_value=["modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "",
        "PR_TITLE": "simple change",
    }, clear=False)
    def test_auto_detect_normal_enforces_limits(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (150, 60, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "normal",
    }, clear=False)
    def test_explicit_normal_enforces_both_limits(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (150, 60, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "normal",
    }, clear=False)
    def test_normal_passes_under_limit(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (50, 10, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "emergency_override",
        "PR_TITLE": "[emergency] hotfix",
        "PR_LABELS": "approved-override",
        "PR_REVIEW_STATE": "APPROVED",
    }, clear=False)
    def test_emergency_override_full_bypass(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (200, 100, "modules/fsm/main.py"),
            (200, 100, "modules/watchdog/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "emergency_override",
        "PR_LABELS": "",
        "CHANGE_CLASS_APPROVED": "",
        "PR_REVIEW_STATE": "APPROVED",
    }, clear=False)
    def test_emergency_without_label_fails(self, mock_resolve):
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "emergency_override",
        "PR_LABELS": "approved-override",
        "PR_REVIEW_STATE": "PENDING",
    }, clear=False)
    def test_emergency_without_review_fails(self, mock_resolve):
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["spec/fsm.md", "modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "spec_sync",
        "PR_LABELS": "approved-override",
    }, clear=False)
    def test_spec_sync_bypasses_limits(self, mock_resolve, mock_numstat, mock_files):
        """spec_sync skips line limit AND module limit."""
        mock_numstat.return_value = [
            (200, 100, "spec/fsm.md"),
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/watchdog/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["spec/fsm.md", "modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "spec_sync",
        "PR_TITLE": "update interfaces",
        "PR_LABELS": "approved-override",
    }, clear=False)
    def test_spec_sync_bypasses_both_limits(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (200, 100, "spec/fsm.md"),
            (10, 5, "modules/fsm/main.py"),
            (10, 5, "modules/watchdog/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["spec/fsm.md", "modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "spec_sync",
        "PR_TITLE": "[spec-sync] update interfaces",
        "PR_LABELS": "",
        "CHANGE_CLASS_APPROVED": "",
        "ALLOW_SPEC_MODIFICATION": "true",
    }, clear=False)
    def test_spec_sync_with_allow_spec_modification_passes(self, mock_resolve, mock_numstat, mock_files):
        """ALLOW_SPEC_MODIFICATION=true authorizes spec_sync in integration."""
        mock_numstat.return_value = [
            (200, 100, "spec/fsm.md"),
            (10, 5, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["ci/check_pr_scope.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "infra_change",
        "PR_TITLE": "update CI scripts",
        "PR_LABELS": "approved-override",
    }, clear=False)
    def test_infra_bypasses_line_limit_only(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (300, 200, "ci/check_pr_scope.py"),
        ]
        self.assertEqual(main(), 0)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["ci/check_pr_scope.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "infra_change",
        "PR_TITLE": "update CI",
        "PR_LABELS": "approved-override",
    }, clear=False)
    def test_infra_still_enforces_module_limit(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (10, 5, "ci/check_pr_scope.py"),
            (20, 10, "modules/fsm/main.py"),
            (20, 10, "modules/watchdog/main.py"),
        ]
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["modules/fsm/main.py"])
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {"CHANGE_CLASS": "invalid_class"}, clear=False)
    def test_invalid_change_class_fails(self, mock_resolve, mock_files):
        self.assertEqual(main(), 1)

    @patch("ci.check_pr_scope._get_changed_files", return_value=["modules/fsm/main.py"])
    @patch("ci.check_pr_scope.get_numstat")
    @patch("ci.check_pr_scope.resolve_diff_range", return_value="fake...range")
    @patch.dict("os.environ", {
        "CHANGE_CLASS": "emergency_override",
        "PR_TITLE": "[emergency] hotfix",
        "CHANGE_CLASS_APPROVED": "true",
        "PR_REVIEW_STATE": "APPROVED",
    }, clear=False)
    def test_admin_approved_grants_access(self, mock_resolve, mock_numstat, mock_files):
        mock_numstat.return_value = [
            (200, 100, "modules/fsm/main.py"),
        ]
        self.assertEqual(main(), 0)


class ConstantsTests(unittest.TestCase):
    def test_max_lines(self):
        self.assertEqual(MAX_CHANGED_LINES, 200)

    def test_module_minor_threshold(self):
        self.assertEqual(MODULE_MINOR_THRESHOLD, 20)

    def test_excluded_prefixes(self):
        self.assertIn("tests/", EXCLUDED_PREFIXES)
        self.assertIn("ci/", EXCLUDED_PREFIXES)
        self.assertIn("spec/", EXCLUDED_PREFIXES)

    def test_valid_change_classes_includes_normal(self):
        self.assertIn("normal", VALID_CHANGE_CLASSES)
        self.assertIn("emergency_override", VALID_CHANGE_CLASSES)
        self.assertIn("spec_sync", VALID_CHANGE_CLASSES)
        self.assertIn("infra_change", VALID_CHANGE_CLASSES)


class ExportToGithubEnvTests(unittest.TestCase):
    """Tests for _export_to_github_env()."""

    def test_writes_to_github_env_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env",
                                         delete=False) as f:
            env_file = f.name
        with patch.dict("os.environ", {"GITHUB_ENV": env_file}):
            _export_to_github_env("CHANGE_CLASS", "spec_sync")
        with open(env_file) as f:
            content = f.read()
        self.assertIn("CHANGE_CLASS=spec_sync\n", content)
        os.unlink(env_file)

    def test_no_op_when_github_env_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            # Should not raise
            _export_to_github_env("CHANGE_CLASS", "normal")


if __name__ == "__main__":
    unittest.main()
