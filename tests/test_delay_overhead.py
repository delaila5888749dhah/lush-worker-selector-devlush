"""Benchmark for §8.6 overhead invariant (Phase 5B Task 5).

Measures **pure bookkeeping overhead** of the behavior wrapper —
state-machine transitions, RNG draws, accumulator math, log-formatting —
relative to a baseline task. Real ``time.sleep`` calls inside
``inject_step_delay`` are stubbed out so we measure CPU time only.

Per Blueprint §8.6 the bookkeeping must add ≤ 15% on top of the baseline
task. CI runners flake under load, so the test allows one retry with a
relaxed 20% cap before failing (per issue spec).
"""
import os
import time
import unittest
from unittest.mock import patch

import pytest

from modules.delay.persona import PersonaProfile
from modules.delay.wrapper import wrap


_N_ITERATIONS = 100
_OVERHEAD_CAP = 0.15        # Blueprint §8.6
_OVERHEAD_RETRY_CAP = 0.20  # Slightly relaxed cap on the retry attempt


def _baseline_task(_worker_id):
    """Simulate real work: ~50 ms of CPU."""
    # Use a deterministic CPU-bound spin so that timing isn't dominated
    # by OS scheduler noise inherent to time.sleep on some CI runners.
    end = time.perf_counter() + 0.050
    x = 0
    while time.perf_counter() < end:
        x += 1
    return x


def _measure(seed: int) -> tuple[float, float]:
    """Return (baseline_total, wrapped_total) seconds for *seed*."""
    persona = PersonaProfile(seed)

    # Stub time.sleep / Event.wait so we measure pure CPU overhead.
    with patch("modules.delay.wrapper.time.sleep", lambda _t: None):
        # Baseline run.
        t0 = time.perf_counter()
        for _ in range(_N_ITERATIONS):
            _baseline_task("worker-1")
        baseline_total = time.perf_counter() - t0

        # Wrapped run.
        wrapped = wrap(_baseline_task, persona)
        t0 = time.perf_counter()
        for _ in range(_N_ITERATIONS):
            wrapped("worker-1")
        wrapped_total = time.perf_counter() - t0

    return baseline_total, wrapped_total


def _overhead_ratio(seed: int) -> tuple[float, float, float]:
    baseline_total, wrapped_total = _measure(seed)
    ratio = (wrapped_total - baseline_total) / max(baseline_total, 1e-9)
    return ratio, baseline_total, wrapped_total


# Benchmarks default OFF in CI (set DELAY_OVERHEAD_BENCH=1 to opt in)
# because wall-clock measurements are inherently flaky in containers.
_BENCH_OPT_IN = os.getenv("DELAY_OVERHEAD_BENCH") in ("1", "true", "yes")


@unittest.skipUnless(_BENCH_OPT_IN, "Set DELAY_OVERHEAD_BENCH=1 to run benchmark")
class TestDelayOverhead(unittest.TestCase):
    """Behavior wrapper bookkeeping overhead < 15% of baseline (per §8.6)."""

    def setUp(self):
        # Disable drift so RNG draws are minimised.
        self._drift_patch = patch(
            "modules.delay.temporal.ENABLE_GRADUAL_DRIFT", False
        )
        self._drift_patch.start()

    def tearDown(self):
        self._drift_patch.stop()

    def test_delay_engine_overhead_below_15_percent(self):
        ratio, base, wrapped = _overhead_ratio(seed=42)
        if ratio >= _OVERHEAD_CAP:
            # One retry with relaxed cap (per issue spec).
            ratio, base, wrapped = _overhead_ratio(seed=42)
            self.assertLess(
                ratio,
                _OVERHEAD_RETRY_CAP,
                f"Behavior wrapper overhead {ratio:.2%} exceeds "
                f"§8.6 retry cap {_OVERHEAD_RETRY_CAP:.0%} "
                f"(baseline={base:.3f}s, wrapped={wrapped:.3f}s)",
            )
        else:
            self.assertLess(ratio, _OVERHEAD_CAP)

    def test_overhead_stable_across_personas(self):
        """Overhead should not depend strongly on persona seed."""
        for seed in (1, 42, 999):
            ratio, _, _ = _overhead_ratio(seed)
            # Always allow the relaxed cap here — this is a parametrised
            # check, not the headline assertion.
            self.assertLess(
                ratio,
                _OVERHEAD_RETRY_CAP,
                f"seed={seed}: overhead {ratio:.2%} > {_OVERHEAD_RETRY_CAP:.0%}",
            )

    def test_overhead_stable_across_day_night(self):
        """Wrapper bookkeeping must respect §8.6 in both DAY and NIGHT modes."""
        for state in ("DAY", "NIGHT"):
            with patch(
                "modules.delay.temporal.TemporalModel.get_time_state",
                return_value=state,
            ):
                ratio, _, _ = _overhead_ratio(seed=42)
            self.assertLess(
                ratio,
                _OVERHEAD_RETRY_CAP,
                f"state={state}: overhead {ratio:.2%} > {_OVERHEAD_RETRY_CAP:.0%}",
            )


# ---------------------------------------------------------------------------
# Pytest mirror — pytest-only; same opt-in.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _BENCH_OPT_IN,
                    reason="Set DELAY_OVERHEAD_BENCH=1 to run benchmark")
@pytest.mark.parametrize("persona_seed", [1, 42, 999, 2 ** 32 - 1])
def test_overhead_stable_across_personas_pytest(persona_seed):
    with patch("modules.delay.temporal.ENABLE_GRADUAL_DRIFT", False):
        ratio, _, _ = _overhead_ratio(persona_seed)
    assert ratio < _OVERHEAD_RETRY_CAP, (
        f"seed={persona_seed}: overhead {ratio:.2%} > {_OVERHEAD_RETRY_CAP:.0%}"
    )


if __name__ == "__main__":
    unittest.main()

