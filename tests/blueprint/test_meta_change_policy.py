"""Tests for INV-META-01 — change-policy enforcement.

These tests exercise ``ci.check_blueprint_contracts.check_change_policy`` and
the parsing of the audit-lock CHANGE POLICY file list.
"""
from __future__ import annotations

import unittest

from ci.check_blueprint_contracts import (
    AUDIT_LOCK_RELATIVE,
    PROTECTED_FILES,
    _FALLBACK_PROTECTED_FILES,
    _parse_protected_files,
    check_change_policy,
)
from ci.check_blueprint_contracts import AUDIT_LOCK_PATH


class ProtectedFilesParsingTests(unittest.TestCase):
    """The hard-coded fallback list MUST stay in sync with audit-lock.md."""

    def test_protected_files_parsed_from_audit_lock(self):
        parsed = _parse_protected_files(AUDIT_LOCK_PATH)
        # Parsing must succeed (non-empty) and match the runtime constant.
        self.assertTrue(parsed)
        self.assertEqual(parsed, PROTECTED_FILES)

    def test_fallback_matches_audit_lock(self):
        parsed = _parse_protected_files(AUDIT_LOCK_PATH)
        self.assertEqual(
            sorted(parsed),
            sorted(_FALLBACK_PROTECTED_FILES),
            msg=(
                "Hard-coded _FALLBACK_PROTECTED_FILES drifted from "
                "spec/audit-lock.md#change-policy-post-audit. "
                "Update the fallback constant to match."
            ),
        )

    def test_fallback_used_when_audit_lock_missing(self, tmp_path=None):
        from pathlib import Path
        result = _parse_protected_files(Path("/nonexistent/audit-lock.md"))
        self.assertEqual(result, _FALLBACK_PROTECTED_FILES)


class CheckChangePolicyTests(unittest.TestCase):
    def test_pass_when_no_protected_files_changed(self):
        code, msg = check_change_policy(changed_files=[
            "README.md",
            "docs/blueprint_coverage.md",
        ])
        self.assertEqual(code, 0, msg)
        self.assertIn("PASS", msg)

    def test_fail_when_protected_file_changed_without_audit_lock(self):
        code, msg = check_change_policy(changed_files=[
            "modules/fsm/main.py",
            "tests/test_fsm.py",
        ])
        self.assertEqual(code, 1, msg)
        self.assertIn("FAIL", msg)
        self.assertIn("INV-META-01", msg)
        self.assertIn("modules/fsm/main.py", msg)
        self.assertIn(AUDIT_LOCK_RELATIVE, msg)

    def test_pass_when_protected_file_and_audit_lock_changed(self):
        code, msg = check_change_policy(changed_files=[
            "modules/delay/engine.py",
            AUDIT_LOCK_RELATIVE,
        ])
        self.assertEqual(code, 0, msg)
        self.assertIn("PASS", msg)

    def test_lists_all_violating_protected_files(self):
        code, msg = check_change_policy(changed_files=[
            "integration/orchestrator.py",
            "modules/cdp/main.py",
            "modules/watchdog/main.py",
        ])
        self.assertEqual(code, 1, msg)
        for path in (
            "integration/orchestrator.py",
            "modules/cdp/main.py",
            "modules/watchdog/main.py",
        ):
            self.assertIn(path, msg)


if __name__ == "__main__":
    unittest.main()
