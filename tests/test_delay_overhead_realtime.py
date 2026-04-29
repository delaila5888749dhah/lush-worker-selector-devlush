"""Wall-clock realtime overhead test for §8.6 (issue G5).

This test addresses the audit gap in :mod:`tests.test_delay_overhead`,
which compares bookkeeping CPU time to a baseline CPU task with
``time.sleep`` stubbed — a ratio that is independent of the actual
delay budget and therefore does not directly verify Blueprint §8.6.

Per Blueprint §8.6 the relevant invariant is that wrapper bookkeeping
(state-machine transitions, RNG draws, accumulator math, log-format)
adds only a small fraction of ``MAX_STEP_DELAY`` *on top of the real
behavioral sleep*. To verify this the test:

1. Calls :func:`inject_step_delay` with the **real** ``time.sleep``
   path — no monkey-patching of ``time.sleep`` — so the elapsed
   wall-clock includes both the actual behavioral sleep and the
   bookkeeping work around it.
2. Captures the requested delay via the function's return value.
3. Computes ``overhead = elapsed_wall - requested_delay`` per call,
   which isolates wrapper bookkeeping from the requested sleep itself.
4. Asserts the median and p95 overhead are a small fraction of
   ``MAX_STEP_DELAY``, the per-step Blueprint §8.6 budget.

Like ``test_delay_overhead.py`` this benchmark is opt-in via
``DELAY_OVERHEAD_BENCH=1`` because wall-clock measurements are
inherently flaky in CI containers and each iteration sleeps for a
real ``MIN_TYPING_DELAY .. MAX_TYPING_DELAY`` interval.
"""
import os
import statistics
import time
import unittest

from modules.delay.config import MAX_STEP_DELAY
from modules.delay.engine import DelayEngine
from modules.delay.persona import PersonaProfile
from modules.delay.state import BehaviorStateMachine
from modules.delay.temporal import TemporalModel
from modules.delay.wrapper import inject_step_delay


_N_ITERATIONS = 8
_WARMUP = 2
# Bookkeeping overhead caps as a fraction of MAX_STEP_DELAY (Blueprint §8.6).
_MEDIAN_RATIO_CAP = 0.05  # strict: median < 5% of MAX_STEP_DELAY
_P95_RATIO_CAP = 0.10     # relaxed for OS jitter: p95 < 10% of MAX_STEP_DELAY
_UNDERSHOOT_TOLERANCE = 0.010  # allow 10 ms for timer granularity / clock jitter

_BENCH_OPT_IN = os.getenv("DELAY_OVERHEAD_BENCH") in ("1", "true", "yes")


def _measure_overhead_samples(seed: int, action: str = "typing") -> list[float]:
    """Run real-sleep ``inject_step_delay`` calls and return overhead samples.

    Each sample is ``elapsed_wall - requested_delay`` (seconds), where
    ``elapsed_wall`` is measured with :func:`time.perf_counter` around
    the call and ``requested_delay`` is the value returned by
    :func:`inject_step_delay` (the actual time it asked ``time.sleep``
    to sleep, after temporal modifiers and accumulator clamping).

    Warm-up iterations are discarded to remove first-call import / lazy
    init noise. The accumulator is reset between iterations so each
    call exercises a full bookkeeping path.
    """
    persona = PersonaProfile(seed)
    sm = BehaviorStateMachine()
    engine = DelayEngine(persona, sm)
    temporal = TemporalModel(persona)

    samples: list[float] = []
    for i in range(_WARMUP + _N_ITERATIONS):
        sm.reset()
        sm.transition("FILLING_FORM")
        engine.reset_step_accumulator()
        temporal.reset_drift()

        t0 = time.perf_counter()
        delay = inject_step_delay(engine, temporal, action)
        elapsed = time.perf_counter() - t0

        if i < _WARMUP:
            continue
        if delay <= 0:
            # Skipped delay (critical context or accumulator empty); the
            # call is essentially pure bookkeeping with no sleep, so it
            # is not a meaningful overhead-vs-real-sleep sample.
            continue
        samples.append(elapsed - delay)
    return samples


def _percentile(samples: list[float], pct: float) -> float:
    """Return ``pct``-th percentile (0..1) of ``samples`` (small-N safe)."""
    if not samples:
        raise ValueError("empty samples")
    ordered = sorted(samples)
    idx = max(0, min(len(ordered) - 1, int(round(pct * len(ordered))) - 1))
    return ordered[idx]


@unittest.skipUnless(_BENCH_OPT_IN, "Set DELAY_OVERHEAD_BENCH=1 to run benchmark")
class TestDelayOverheadRealtime(unittest.TestCase):
    """Real wall-clock bookkeeping overhead vs MAX_STEP_DELAY (Blueprint §8.6).

    Unlike :mod:`tests.test_delay_overhead`, this test does NOT stub
    ``time.sleep``; it measures the full real-time call (sleep included)
    and subtracts the engine-reported sleep duration to isolate the
    bookkeeping component, then ratios it against ``MAX_STEP_DELAY``.
    """

    def _assert_under_budget(self, samples: list[float], label: str) -> None:
        self.assertGreater(
            len(samples), 0,
            f"{label}: no overhead samples collected (delays were all 0?)",
        )
        min_overhead = min(samples)
        self.assertGreaterEqual(
            min_overhead,
            -_UNDERSHOOT_TOLERANCE,
            f"{label}: wall-clock elapsed time undershot the requested delay by "
            f"{(-min_overhead) * 1000:.2f} ms; benchmark must record actual - "
            f"expected without masking real sleep undershoot",
        )
        median = statistics.median(samples)
        p95 = _percentile(samples, 0.95)
        median_ratio = median / MAX_STEP_DELAY
        p95_ratio = p95 / MAX_STEP_DELAY
        self.assertLess(
            median_ratio, _MEDIAN_RATIO_CAP,
            f"{label}: median bookkeeping overhead {median * 1000:.2f} ms "
            f"is {median_ratio:.2%} of MAX_STEP_DELAY ({MAX_STEP_DELAY:.1f}s); "
            f"§8.6 requires <{_MEDIAN_RATIO_CAP:.0%} (n={len(samples)})",
        )
        self.assertLess(
            p95_ratio, _P95_RATIO_CAP,
            f"{label}: p95 bookkeeping overhead {p95 * 1000:.2f} ms "
            f"is {p95_ratio:.2%} of MAX_STEP_DELAY; "
            f"§8.6 (relaxed for OS jitter) requires <{_P95_RATIO_CAP:.0%} "
            f"(n={len(samples)})",
        )

    def test_typing_overhead_below_5_percent_of_max_step_delay(self):
        samples = _measure_overhead_samples(seed=42, action="typing")
        self._assert_under_budget(samples, "typing seed=42")

    def test_thinking_overhead_below_5_percent_of_max_step_delay(self):
        # Thinking delays are MIN_THINKING_DELAY..MAX_HESITATION_DELAY (3–5 s)
        # which dominates wall time; bookkeeping must remain a small fraction
        # of MAX_STEP_DELAY regardless.
        samples = _measure_overhead_samples(seed=42, action="thinking")
        self._assert_under_budget(samples, "thinking seed=42")

    def test_overhead_stable_across_personas(self):
        """Bookkeeping must respect §8.6 across persona seeds."""
        for seed in (1, 42, 999):
            samples = _measure_overhead_samples(seed=seed, action="typing")
            self._assert_under_budget(samples, f"typing seed={seed}")


if __name__ == "__main__":
    unittest.main()
