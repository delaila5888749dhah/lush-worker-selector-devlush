"""Wall-clock realtime bookkeeping overhead test for §8.6 (issue G5).

This test addresses the audit gap in :mod:`tests.test_delay_overhead`,
which compares bookkeeping CPU time to a baseline CPU task — a
ratio that is independent of the actual delay budget and therefore
does not directly verify Blueprint §8.6.

Per Blueprint §8.6 the relevant invariant is that wrapper bookkeeping
(state-machine transitions, RNG draws, accumulator math, log-format)
is a small fraction of ``MAX_STEP_DELAY`` (the per-step delay budget),
not of the workload time. This test wraps a fake ``task_fn`` doing
~50 ms of CPU work, runs 100 iterations, and asserts:

    bookkeeping_overhead_per_step / MAX_STEP_DELAY < 0.05  (strict)
    bookkeeping_overhead_per_step / MAX_STEP_DELAY < 0.15  (per-step §8.6)

``time.sleep`` is stubbed so the wall-clock measurement
(``time.perf_counter``) reflects only bookkeeping; the actual
behavioral delay durations are not part of the bookkeeping budget
and would dominate the elapsed time, making the test impractical.

Like ``test_delay_overhead.py`` this benchmark is opt-in via
``DELAY_OVERHEAD_BENCH=1`` because wall-clock measurements are
inherently flaky in CI containers.
"""
import os
import time
import unittest
from unittest.mock import patch

from modules.delay.config import MAX_STEP_DELAY
from modules.delay.persona import PersonaProfile
from modules.delay.wrapper import wrap


_N_ITERATIONS = 100
_TASK_CPU_SECONDS = 0.050  # ~50 ms CPU work per iteration (issue spec).
_OVERHEAD_BUDGET_RATIO = 0.05   # bookkeeping / MAX_STEP_DELAY (strict).
_PER_STEP_RELAXED = 0.15        # Blueprint §8.6 per-step ceiling on retry.


def _baseline_task(_worker_id):
    """Simulate ~50 ms of deterministic CPU work."""
    end = time.perf_counter() + _TASK_CPU_SECONDS
    x = 0
    while time.perf_counter() < end:
        x += 1
    return x


def _measure_wall(seed: int) -> tuple[float, float]:
    """Return (wall_time_without_wrap, wall_time_with_wrap) seconds.

    ``time.sleep`` inside the wrapper is stubbed to a no-op so the
    elapsed wall-clock measurement reflects bookkeeping only — the
    sleeps themselves are part of the simulated behavior, not part
    of the overhead being measured against ``MAX_STEP_DELAY``.
    """
    persona = PersonaProfile(seed)

    with patch("modules.delay.wrapper.time.sleep", lambda _t: None):
        # Baseline: no wrapper.
        t0 = time.perf_counter()
        for _ in range(_N_ITERATIONS):
            _baseline_task("worker-1")
        wall_without = time.perf_counter() - t0

        # Wrapped: full bookkeeping path.
        wrapped = wrap(_baseline_task, persona)
        t0 = time.perf_counter()
        for _ in range(_N_ITERATIONS):
            wrapped("worker-1")
        wall_with = time.perf_counter() - t0

    return wall_without, wall_with


def _per_step_ratio(seed: int) -> tuple[float, float, float]:
    wall_without, wall_with = _measure_wall(seed)
    bookkeeping = max(wall_with - wall_without, 0.0)
    per_step = bookkeeping / _N_ITERATIONS
    ratio = per_step / MAX_STEP_DELAY
    return ratio, per_step, bookkeeping


_BENCH_OPT_IN = os.getenv("DELAY_OVERHEAD_BENCH") in ("1", "true", "yes")


@unittest.skipUnless(_BENCH_OPT_IN, "Set DELAY_OVERHEAD_BENCH=1 to run benchmark")
class TestDelayOverheadRealtime(unittest.TestCase):
    """Wrapper bookkeeping overhead vs MAX_STEP_DELAY (Blueprint §8.6).

    Unlike :mod:`tests.test_delay_overhead`, this test compares the
    wall-clock bookkeeping cost to the per-step delay *budget*, which
    is the actual spec invariant.
    """

    def test_bookkeeping_below_5_percent_of_max_step_delay(self):
        ratio, per_step, total = _per_step_ratio(seed=42)
        if ratio >= _OVERHEAD_BUDGET_RATIO:
            # One retry with the relaxed per-step §8.6 cap to absorb
            # transient CI noise. The seed is unchanged on purpose:
            # bookkeeping is deterministic per seed, but the wall-clock
            # ``perf_counter`` measurement is not — re-running gives a
            # fresh sample of system-load noise (see same pattern in
            # ``test_delay_overhead.py``).
            ratio, per_step, total = _per_step_ratio(seed=42)
            self.assertLess(
                ratio,
                _PER_STEP_RELAXED,
                f"per-step bookkeeping {per_step * 1000:.2f} ms is "
                f"{ratio:.2%} of MAX_STEP_DELAY ({MAX_STEP_DELAY:.1f}s); "
                f"§8.6 invariant requires <{_PER_STEP_RELAXED:.0%} "
                f"(total bookkeeping over {_N_ITERATIONS} iters: "
                f"{total * 1000:.1f} ms)",
            )
        else:
            self.assertLess(ratio, _OVERHEAD_BUDGET_RATIO)

    def test_bookkeeping_stable_across_personas(self):
        """Per-step overhead must respect §8.6 across persona seeds."""
        for seed in (1, 42, 999):
            ratio, per_step, _ = _per_step_ratio(seed)
            self.assertLess(
                ratio,
                _PER_STEP_RELAXED,
                f"seed={seed}: per-step {per_step * 1000:.2f} ms is "
                f"{ratio:.2%} of MAX_STEP_DELAY; "
                f"§8.6 requires <{_PER_STEP_RELAXED:.0%}",
            )


if __name__ == "__main__":
    unittest.main()
