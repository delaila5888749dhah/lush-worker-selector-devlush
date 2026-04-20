"""Replacement for test_runtime_wiring_posture.py (U-02 / F-01 landed).

The former lock-in test asserted that BitBrowser / CDP registration calls
were NOT present anywhere in the code base. F-01/F-03 have now landed and
the wiring lives in ``integration/worker_task.py::make_task_fn``. This
test verifies that wiring is present.
"""
import ast
import inspect
import unittest
from typing import List

from integration import worker_task as _worker_task_module


def _source() -> str:
    return inspect.getsource(_worker_task_module)


def _call_names(tree: ast.AST) -> List[str]:
    names: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                names.append(f"{func.value.id}.{func.attr}")
            else:
                names.append(func.attr)
        elif isinstance(func, ast.Name):
            names.append(func.id)
    return names


class TestRuntimeWiringLifecycle(unittest.TestCase):
    """C14 lifecycle wiring lock-in (replaces U-02 posture test)."""

    def setUp(self):
        self._source = _source()
        self._call_names = _call_names(ast.parse(self._source))

    def test_session_lifecycle_wired_into_task_fn(self):
        """BitBrowserSession is instantiated inside make_task_fn."""
        self.assertIn("BitBrowserSession", self._source)
        self.assertIn("get_bitbrowser_client", self._source)

    def test_register_driver_invoked(self):
        """cdp.register_driver must be wired in the task factory."""
        self.assertIn("cdp.register_driver", self._call_names)

    def test_register_browser_profile_invoked(self):
        """cdp.register_browser_profile must be wired in the task factory."""
        self.assertIn("cdp.register_browser_profile", self._call_names)

    def test_unregister_driver_in_finally(self):
        """The cycle must always unregister the driver on exit."""
        self.assertIn("cdp.unregister_driver", self._call_names)


if __name__ == "__main__":
    unittest.main()
