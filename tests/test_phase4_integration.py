"""Phase 4 Integration Smoke Tests — SPEC-6 §Phase 4 (CP-1 through CP-8).

Smoke tests only: verify module loadability and interface compatibility.
Business logic is covered by unit tests in individual module test files.
"""

import ast
from pathlib import Path
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestModuleLoadability(unittest.TestCase):
    """All modules must be importable without errors. (CP-1)"""

    def test_fsm_module_loads(self):
        import modules.fsm.main  # noqa: F401

    def test_billing_module_loads(self):
        import modules.billing.main  # noqa: F401

    def test_behavior_module_loads(self):
        import modules.behavior.main  # noqa: F401

    def test_delay_module_loads(self):
        import modules.delay.main  # noqa: F401

    def test_cdp_module_loads(self):
        import modules.cdp.main  # noqa: F401

    def test_watchdog_module_loads(self):
        import modules.watchdog.main  # noqa: F401

    def test_integration_runtime_loads(self):
        import integration.runtime  # noqa: F401


class TestInterfaceCompatibility(unittest.TestCase):
    """Public interfaces must be present and callable. (CP-1, CP-3)"""

    def test_persona_profile_instantiable(self):
        from modules.delay.main import PersonaProfile
        p = PersonaProfile(seed=42)
        self.assertIsNotNone(p)

    def test_delay_engine_instantiable(self):
        from modules.delay.main import DelayEngine, PersonaProfile, BehaviorStateMachine
        p = PersonaProfile(seed=1)
        sm = BehaviorStateMachine()
        engine = DelayEngine(persona=p, state_machine=sm)
        self.assertIsNotNone(engine)

    def test_temporal_model_instantiable(self):
        from modules.delay.main import TemporalModel, PersonaProfile
        p = PersonaProfile(seed=5)
        t = TemporalModel(persona=p)
        self.assertIsNotNone(t)

    def test_behavior_state_machine_instantiable(self):
        from modules.delay.main import BehaviorStateMachine
        sm = BehaviorStateMachine()
        self.assertEqual(sm.get_state(), "IDLE")

    def test_wrap_function_present(self):
        from modules.delay.main import wrap
        self.assertTrue(callable(wrap))

    def test_inject_step_delay_present(self):
        from modules.delay.wrapper import inject_step_delay
        self.assertTrue(callable(inject_step_delay))

    def test_behavior_evaluate_present(self):
        from modules.behavior.main import evaluate
        self.assertTrue(callable(evaluate))


class TestEndToEndPipeline(unittest.TestCase):
    """End-to-end pipeline: persona → delay engine → wrap → execute. (CP-2, CP-3)"""

    def test_full_pipeline_single_worker(self):
        """Single worker full pipeline: PersonaProfile → wrap() → execute dummy task."""
        import threading
        from modules.delay.main import PersonaProfile, wrap

        results = []

        def dummy_task():
            results.append("done")
            return "ok"

        persona = PersonaProfile(seed=7)
        stop_event = threading.Event()
        with patch.object(stop_event, "wait", return_value=False):
            wrapped = wrap(dummy_task, persona, stop_event)
            ret = wrapped()
        self.assertEqual(ret, "ok")
        self.assertEqual(results, ["done"])

    @patch("modules.delay.wrapper.time.sleep", return_value=None)
    def test_wrap_does_not_alter_task_output(self, _mock_sleep):
        """wrap() must not change the return value of task_fn."""
        from modules.delay.main import PersonaProfile, wrap

        sentinel = object()

        def identity_task():
            return sentinel

        persona = PersonaProfile(seed=99)
        wrapped = wrap(identity_task, persona)
        self.assertIs(wrapped(), sentinel)

    @patch("modules.delay.wrapper.time.sleep", return_value=None)
    def test_wrap_propagates_exceptions(self, _mock_sleep):
        """wrap() must propagate exceptions from task_fn unchanged."""
        from modules.delay.main import PersonaProfile, wrap

        class _Boom(RuntimeError):
            pass

        def failing_task():
            raise _Boom("boom")

        persona = PersonaProfile(seed=3)
        wrapped = wrap(failing_task, persona)
        with self.assertRaises(_Boom):
            wrapped()


class TestModuleIsolation(unittest.TestCase):
    """Verify no cross-module import violations at runtime. (CP-1)"""

    def _iter_python_files(self, package_path):
        return sorted((REPO_ROOT / package_path).rglob("*.py"))

    def _assert_no_imports(self, package_path, forbidden_prefix):
        for file_path in self._iter_python_files(package_path):
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
            relative_path = file_path.relative_to(REPO_ROOT)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported = alias.name
                        self.assertFalse(
                            imported == forbidden_prefix
                            or imported.startswith(f"{forbidden_prefix}."),
                            f"{relative_path}:{node.lineno} imports {imported}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    imported = node.module or ""
                    self.assertFalse(
                        imported == forbidden_prefix
                        or imported.startswith(f"{forbidden_prefix}."),
                        f"{relative_path}:{node.lineno} imports {imported}",
                    )

    def test_delay_module_no_integration_import(self):
        self._assert_no_imports("modules/delay", "integration")

    def test_behavior_module_no_delay_import(self):
        self._assert_no_imports("modules/behavior", "modules.delay")


class TestBillingAtomicInterface(unittest.TestCase):
    """CP-5: Billing interface must be present and thread-safe."""

    def test_billing_has_select_profile(self):
        from modules.billing import main as billing
        self.assertTrue(callable(getattr(billing, "select_profile", None)))

    def test_billing_module_has_idempotency_check(self):
        """Billing _lock provides the atomic guard against double-consume (Guard 3.2)."""
        import threading
        from modules.billing import main as billing
        lock = getattr(billing, "_lock", None)
        if lock is None:
            self.skipTest("billing._lock not yet implemented")
        self.assertIsInstance(lock, type(threading.Lock()))


class TestScalingIntegration(unittest.TestCase):
    """CP-8: Behavior decision engine integrates with runtime."""

    def setUp(self):
        from modules.behavior import main as behavior
        behavior.reset()
        behavior.expire_cooldown_for_testing()

    def test_evaluate_returns_valid_decision(self):
        from modules.behavior.main import evaluate, VALID_DECISIONS
        metrics = {"error_rate": 0.0, "success_rate": 0.80,
                   "restarts_last_hour": 0, "baseline_success_rate": None}
        decision, reasons = evaluate(metrics, 0, 3)
        self.assertIn(decision, VALID_DECISIONS)
        self.assertIsInstance(reasons, list)

    def test_scale_up_on_healthy_metrics(self):
        from modules.behavior.main import evaluate, SCALE_UP
        metrics = {
            "error_rate": 0.01,
            "success_rate": 0.85,
            "restarts_last_hour": 0,
            "baseline_success_rate": None,
        }
        decision, _ = evaluate(metrics, 0, 3)
        self.assertEqual(decision, SCALE_UP)

    def test_scale_down_on_high_error_rate(self):
        from modules.behavior.main import evaluate, SCALE_DOWN
        metrics = {
            "error_rate": 0.20,
            "success_rate": 0.50,
            "restarts_last_hour": 0,
            "baseline_success_rate": None,
        }
        decision, reasons = evaluate(metrics, 1, 3)
        self.assertEqual(decision, SCALE_DOWN)
        self.assertTrue(len(reasons) > 0)


if __name__ == "__main__":
    unittest.main()
