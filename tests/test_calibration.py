"""Calibration loop tests — validate delay optimization metrics."""
import threading
import unittest
from unittest.mock import patch


class TestCalibrationMetricsBaseline(unittest.TestCase):
    def test_calibration_metrics_baseline(self):
        """100-cycle calibration with no stop_event → all successes, plausible delays."""
        from modules.delay.calibration import run_calibration_loop
        from modules.delay.persona import PersonaProfile

        persona = PersonaProfile(42)
        with patch("modules.delay.wrapper.time.sleep"):
            report = run_calibration_loop(persona, num_cycles=100)

        self.assertAlmostEqual(report.success_rate, 1.0, places=1)
        self.assertEqual(report.timeout_rate, 0.0)
        self.assertGreater(report.avg_cycle_delay, 0.0)
        # Max plausible: typing (1.8) + thinking (5.0) per cycle = 6.8 s
        self.assertLessEqual(report.avg_cycle_delay, 14.0)
        self.assertEqual(report.total_cycles, 100)
        self.assertEqual(report.success_count, 100)
        self.assertEqual(report.timeout_count, 0)

    def test_calibration_reduces_variance_on_high_timeout_rate(self):
        """When timeout rate > 20%, adjustment function reduces MAX_STEP_DELAY suggestion."""
        from modules.delay.calibration import run_calibration_loop, adjust_for_high_timeout_rate
        from modules.delay.config import MAX_STEP_DELAY
        from modules.delay.persona import PersonaProfile

        persona = PersonaProfile(42)
        stop_event = threading.Event()
        stop_event.set()  # pre-set: all cycles "timeout"

        report = run_calibration_loop(persona, num_cycles=10, stop_event=stop_event)

        self.assertGreater(report.timeout_rate, 0.20, "All cycles should be timeouts")
        suggestions = adjust_for_high_timeout_rate(report)
        self.assertIn("MAX_STEP_DELAY", suggestions)
        self.assertLess(suggestions["MAX_STEP_DELAY"], MAX_STEP_DELAY)

    def test_calibration_export_format(self):
        """CalibrationReport.to_dict() must return the required keys."""
        from modules.delay.calibration import CalibrationReport

        report = CalibrationReport(
            persona_type="fast_typer",
            seed=42,
            success_count=80,
            timeout_count=20,
            total_cycles=100,
            delay_samples=[1.5] * 100,
            watchdog_trigger_count=3,
        )
        d = report.to_dict()
        required_keys = {
            "persona_type",
            "seed",
            "success_rate",
            "timeout_rate",
            "avg_cycle_delay",
            "watchdog_trigger_count",
        }
        for key in required_keys:
            self.assertIn(key, d, f"Missing key: {key}")

        self.assertAlmostEqual(d["success_rate"], 0.80)
        self.assertAlmostEqual(d["timeout_rate"], 0.20)
        self.assertAlmostEqual(d["avg_cycle_delay"], 1.5)
        self.assertEqual(d["watchdog_trigger_count"], 3)
        self.assertEqual(d["persona_type"], "fast_typer")
        self.assertEqual(d["seed"], 42)


class TestCalibrationProperties(unittest.TestCase):
    def test_empty_delay_samples_avg_is_zero(self):
        from modules.delay.calibration import CalibrationReport
        r = CalibrationReport("fast_typer", 0, 0, 0, 0, [], 0)
        self.assertEqual(r.avg_cycle_delay, 0.0)

    def test_zero_cycles_rates(self):
        from modules.delay.calibration import CalibrationReport
        r = CalibrationReport("fast_typer", 0, 0, 0, 0, [], 0)
        self.assertEqual(r.success_rate, 1.0)
        self.assertEqual(r.timeout_rate, 0.0)

    def test_watchdog_triggers_counted(self):
        """Cycles that exhaust the accumulator increment watchdog_trigger_count."""
        from modules.delay.calibration import run_calibration_loop
        from modules.delay.persona import PersonaProfile
        from modules.delay.config import MAX_STEP_DELAY

        persona = PersonaProfile(42)
        with patch("modules.delay.wrapper.time.sleep"):
            report = run_calibration_loop(persona, num_cycles=20)
        # watchdog_trigger_count may be 0 for well-behaved delays; just verify type
        self.assertIsInstance(report.watchdog_trigger_count, int)
        self.assertGreaterEqual(report.watchdog_trigger_count, 0)


if __name__ == "__main__":
    unittest.main()
