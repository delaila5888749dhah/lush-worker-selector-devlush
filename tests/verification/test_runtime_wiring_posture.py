"""Lock-in test for runtime wiring posture (U-02).

Parses the source of integration.runtime and asserts that none of the CDP
registration or BitBrowser session functions are called anywhere in the module.

This test will BREAK if someone adds registration wiring before the F-01/F-03
PRs are reviewed, ensuring that change is not silent.
"""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import ast
import inspect
import unittest
from typing import List

from integration import runtime as _runtime_module


def _get_runtime_source() -> str:
    return inspect.getsource(_runtime_module)


def _get_runtime_ast() -> ast.Module:
    return ast.parse(_get_runtime_source())


def _call_names_in_ast(tree: ast.Module) -> List[str]:
    """Return all dotted call-expression names found in the AST."""
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            # e.g. cdp.register_driver
            if isinstance(func.value, ast.Name):
                names.append(f"{func.value.id}.{func.attr}")
        elif isinstance(func, ast.Name):
            names.append(func.id)
    return names


class TestRuntimeWiringPosture(unittest.TestCase):
    """U-02 lock-in: assert runtime.py contains NO CDP-registration or
    BitBrowser-session wiring."""

    def setUp(self):
        self._source = _get_runtime_source()
        self._call_names = _call_names_in_ast(_get_runtime_ast())

    def test_no_cdp_register_driver_call(self):
        """cdp.register_driver must not be wired in runtime.py yet (F-01/F-03 scope)."""
        self.assertNotIn("cdp.register_driver", self._call_names,
                         "cdp.register_driver must not be called in runtime.py "
                         "(belongs to F-01/F-03 scope)")

    def test_no_cdp_register_pid_call(self):
        """cdp._register_pid must not appear in runtime.py source."""
        # _register_pid is private; check source text as AST may mangle leading _
        self.assertNotIn("cdp._register_pid", self._source,
                         "cdp._register_pid must not be called in runtime.py")

    def test_no_cdp_register_browser_profile_call(self):
        """cdp.register_browser_profile must not be wired in runtime.py yet (F-03 scope)."""
        self.assertNotIn("cdp.register_browser_profile", self._call_names,
                         "cdp.register_browser_profile must not be called in "
                         "runtime.py (belongs to F-03 scope)")

    def test_no_bitbrowser_session_instantiation(self):
        """BitBrowserSession must not be instantiated in runtime.py."""
        self.assertNotIn("BitBrowserSession", self._source,
                         "BitBrowserSession must not be instantiated in runtime.py")

    def test_no_get_bitbrowser_client_call(self):
        """get_bitbrowser_client must not be called in runtime.py."""
        self.assertNotIn("get_bitbrowser_client", self._source,
                         "get_bitbrowser_client must not be called in runtime.py")


if __name__ == "__main__":
    unittest.main()
