"""Unit tests for the batched-pytest helpers in ``check_blueprint_contracts``.

These exercise the pure-Python mapping functions (no pytest invocation). The
end-to-end batched path is exercised by running the gate itself on the real
contracts — see ``docs/blueprint_contracts_analysis.md`` §5.
"""
from __future__ import annotations

import unittest

from ci.check_blueprint_contracts import _junit_classname_to_file


class JunitClassnameResolutionTests(unittest.TestCase):
    """``_junit_classname_to_file`` must reverse pytest's dotted classname
    back to (file_relpath, class_segments) so we can correlate junit
    ``<testcase>`` entries with contract ``enforced_by`` nodeids."""

    def test_file_level_test_resolves_to_file(self):
        # tests/blueprint/test_meta_change_policy.py exists at repo root.
        file_rel, classes = _junit_classname_to_file(
            "tests.blueprint.test_meta_change_policy"
        )
        self.assertEqual(file_rel, "tests/blueprint/test_meta_change_policy.py")
        self.assertEqual(classes, [])

    def test_class_level_test_resolves_to_file_plus_class(self):
        file_rel, classes = _junit_classname_to_file(
            "tests.blueprint.test_meta_change_policy.CheckChangePolicyTests"
        )
        self.assertEqual(file_rel, "tests/blueprint/test_meta_change_policy.py")
        self.assertEqual(classes, ["CheckChangePolicyTests"])

    def test_unknown_file_falls_back_to_pascalcase_heuristic(self):
        # No such file — the resolver should still produce a best-effort
        # split so we don't crash on a stray junit entry.
        file_rel, classes = _junit_classname_to_file(
            "tests.nonexistent_module.SomeClass"
        )
        self.assertTrue(file_rel.endswith(".py"))
        self.assertEqual(classes, ["SomeClass"])


if __name__ == "__main__":
    unittest.main()
