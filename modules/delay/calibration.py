"""Calibration loop for delay optimization metrics.

Provides a minimal simulation harness that runs ``num_cycles`` of delay
injection and collects success/timeout/delay metrics, enabling systematic
tuning of timing configuration.
"""

from modules.delay.config import MAX_STEP_DELAY
from modules.delay.engine import DelayEngine
from modules.delay.persona import PersonaProfile
from modules.delay.state import BehaviorStateMachine
from modules.delay.temporal import TemporalModel
from modules.delay.wrapper import inject_step_delay


class CalibrationReport:
    """Collected metrics from a single calibration loop run."""

    def __init__(
        self,
        persona_type: str,
        seed: int,
        success_count: int,
        timeout_count: int,
        total_cycles: int,
        delay_samples: list,
        watchdog_trigger_count: int,
    ) -> None:
        self.persona_type = persona_type
        self.seed = seed
        self.success_count = success_count
        self.timeout_count = timeout_count
        self.total_cycles = total_cycles
        self.delay_samples = list(delay_samples)
        self.watchdog_trigger_count = watchdog_trigger_count

    @property
    def success_rate(self) -> float:
        """Fraction of cycles that completed without a stop-event timeout."""
        if self.total_cycles == 0:
            return 1.0
        return self.success_count / self.total_cycles

    @property
    def timeout_rate(self) -> float:
        """Fraction of cycles interrupted by a stop-event."""
        if self.total_cycles == 0:
            return 0.0
        return self.timeout_count / self.total_cycles

    @property
    def avg_cycle_delay(self) -> float:
        """Mean total delay (typing + thinking) per cycle in seconds."""
        if not self.delay_samples:
            return 0.0
        return sum(self.delay_samples) / len(self.delay_samples)

    def to_dict(self) -> dict:
        """Return a serialisable summary of this report."""
        return {
            "persona_type": self.persona_type,
            "seed": self.seed,
            "success_rate": self.success_rate,
            "timeout_rate": self.timeout_rate,
            "avg_cycle_delay": self.avg_cycle_delay,
            "watchdog_trigger_count": self.watchdog_trigger_count,
        }


def run_calibration_loop(
    persona: PersonaProfile,
    num_cycles: int = 100,
    stop_event=None,
) -> "CalibrationReport":
    """Simulate *num_cycles* of delay injection and collect metrics.

    Each cycle consists of a typing delay followed by a thinking delay,
    mirroring the ``wrap()`` injection pattern.  If *stop_event* is
    provided and set, cycles are counted as timeouts (the event causes
    ``inject_step_delay`` to return immediately via ``stop_event.wait``).

    Parameters
    ----------
    persona:
        The :class:`PersonaProfile` whose RNG and timing attributes are used.
    num_cycles:
        Number of simulation cycles to run.
    stop_event:
        Optional :class:`threading.Event`.  When set before or during a
        cycle, that cycle is recorded as a timeout.

    Returns
    -------
    CalibrationReport
        Aggregated metrics for the run.
    """
    sm = BehaviorStateMachine()
    engine = DelayEngine(persona, sm)
    temporal = TemporalModel(persona)

    success_count = 0
    timeout_count = 0
    delay_samples: list[float] = []
    watchdog_trigger_count = 0

    for _ in range(num_cycles):
        sm.reset()
        sm.transition("FILLING_FORM")
        engine.reset_step_accumulator()

        typing_delay = inject_step_delay(engine, temporal, "typing", stop_event=stop_event)
        thinking_delay = inject_step_delay(engine, temporal, "thinking", stop_event=stop_event)

        cycle_delay = typing_delay + thinking_delay
        delay_samples.append(cycle_delay)

        # Detect timeout: stop_event was set (either pre-set or triggered mid-cycle)
        if stop_event is not None and stop_event.is_set():
            timeout_count += 1
        else:
            success_count += 1

        # Watchdog trigger: step accumulator reached the ceiling
        if engine.get_step_accumulated_delay() >= MAX_STEP_DELAY:
            watchdog_trigger_count += 1

    return CalibrationReport(
        persona_type=persona.persona_type,
        seed=persona._seed,
        success_count=success_count,
        timeout_count=timeout_count,
        total_cycles=num_cycles,
        delay_samples=delay_samples,
        watchdog_trigger_count=watchdog_trigger_count,
    )


def adjust_for_high_timeout_rate(report: "CalibrationReport") -> dict:
    """Return suggested config adjustments when *timeout_rate* exceeds 20%.

    If the timeout rate is above the 20% threshold, this function returns a
    dict of suggested constant values to reduce overall step delay.  Callers
    are responsible for applying these suggestions.

    Returns an empty dict when no adjustment is needed.
    """
    if report.timeout_rate <= 0.20:
        return {}
    # Scale down MAX_STEP_DELAY proportionally to the excess timeout rate,
    # clamped to at least 70% of the current value.
    factor = max(0.70, 1.0 - (report.timeout_rate - 0.20))
    return {"MAX_STEP_DELAY": MAX_STEP_DELAY * factor}
