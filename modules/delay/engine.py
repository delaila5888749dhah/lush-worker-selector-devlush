"""DelayEngine — Action-Aware Bounded Delay Calculator (Task 10.3).

Calculates delays based on action type (typing/click/thinking),
BehaviorState context, and PersonaProfile.  All delays are clamped
by hard constraints before being applied.

Thread-safe via threading.Lock.  Imports are limited to modules
within ``modules.delay``; no imports from outside that package.
Deterministic via random.Random instance from PersonaProfile.
"""

import threading

from modules.delay.config import (
    MAX_TYPING_DELAY, MIN_TYPING_DELAY, MIN_THINKING_DELAY,
    MAX_HESITATION_DELAY, MAX_STEP_DELAY, WATCHDOG_HEADROOM,
)
from modules.delay.persona import PersonaProfile
from modules.delay.state import BehaviorStateMachine

_MIN_THINKING_DELAY: float = MIN_THINKING_DELAY


class DelayEngine:
    """Calculate bounded delays for worker actions.

    Parameters
    ----------
    persona : PersonaProfile
        Seed-deterministic persona providing timing attributes.
    state_machine : BehaviorStateMachine
        FSM that tracks the current behavioral context.
    """

    def __init__(
        self, persona: PersonaProfile, state_machine: BehaviorStateMachine
    ) -> None:
        self._persona = persona
        self._state_machine = state_machine
        self._step_accumulated: float = 0.0
        self._lock = threading.Lock()

    # ── action-specific calculators ──────────────────────────────

    def calculate_typing_delay(self, group_index: int) -> float:
        """Return typing delay for a 4-digit group (0.6–1.8 s, clamped).

        Returns 0.0 when delay is not permitted (critical context or
        accumulator exhausted).
        """
        if not self.is_delay_permitted():
            return 0.0
        raw = self._persona.get_typing_delay(group_index)
        clamped = max(MIN_TYPING_DELAY, min(raw, MAX_TYPING_DELAY))
        return self._accumulate(clamped)

    def calculate_click_delay(self) -> float:
        """Return click reaction delay (0.05–0.25 s). NOT accumulated."""
        return self._persona.get_click_delay()

    def calculate_thinking_delay(self) -> float:
        """Return thinking/hesitation delay (3.0–5.0 s, clamped).

        The raw value comes from PersonaProfile.get_hesitation_delay()
        and is clamped to the safe 3.0–5.0 s range before accumulation.
        Returns 0.0 when delay is not permitted.
        """
        if not self.is_delay_permitted():
            return 0.0
        raw = self._persona.get_hesitation_delay()
        clamped = max(_MIN_THINKING_DELAY, min(raw, MAX_HESITATION_DELAY))
        return self._accumulate(clamped)

    # ── dispatcher ───────────────────────────────────────────────

    def calculate_delay(self, action_type: str) -> float:
        """Dispatch to the appropriate calculator by *action_type*.

        Supported types: ``"typing"``, ``"click"``, ``"thinking"``.
        Unknown types return 0.0.
        """
        if action_type == "typing":
            return self.calculate_typing_delay(0)
        if action_type == "click":
            return self.calculate_click_delay()
        if action_type == "thinking":
            return self.calculate_thinking_delay()
        return 0.0

    def get_base_delay(self, action_type: str) -> float:
        """Return a clamped base delay without recording it in the accumulator."""
        if action_type == "typing":
            raw = self._persona.get_typing_delay(0)
            return max(MIN_TYPING_DELAY, min(raw, MAX_TYPING_DELAY))
        if action_type == "thinking":
            raw = self._persona.get_hesitation_delay()
            return max(_MIN_THINKING_DELAY, min(raw, MAX_HESITATION_DELAY))
        if action_type == "click":
            return self.calculate_click_delay()
        return 0.0

    def accumulate_delay(self, delay: float) -> float:
        """Record a caller-provided delay against the step accumulator."""
        return self._accumulate(delay)

    # ── accumulator ──────────────────────────────────────────────

    def get_step_accumulated_delay(self) -> float:
        """Return the total delay accumulated in the current step."""
        with self._lock:
            return self._step_accumulated

    def reset_step_accumulator(self) -> None:
        """Reset the step accumulator to zero."""
        with self._lock:
            self._step_accumulated = 0.0

    # ── guards ───────────────────────────────────────────────────

    def is_delay_permitted(self) -> bool:
        """Return *True* when delay injection is safe.

        Delay is **not** permitted when the state machine is in a critical
        context (VBV, POST_ACTION, or Phase-9 CRITICAL_SECTION) or when
        the step accumulator has reached the effective ceiling
        ``MAX_STEP_DELAY - WATCHDOG_HEADROOM``.
        """
        if not self._state_machine.is_safe_for_delay():
            return False
        with self._lock:
            return self._step_accumulated < (MAX_STEP_DELAY - WATCHDOG_HEADROOM)

    # ── internal ─────────────────────────────────────────────────

    def _accumulate(self, delay: float) -> float:
        """Clamp *delay* against remaining step headroom and record it.

        The effective ceiling is ``MAX_STEP_DELAY - WATCHDOG_HEADROOM`` so
        that at least *WATCHDOG_HEADROOM* seconds of buffer are always
        reserved for the watchdog timer.
        """
        with self._lock:
            headroom = (MAX_STEP_DELAY - WATCHDOG_HEADROOM) - self._step_accumulated
            if headroom <= 0:
                return 0.0
            actual = min(delay, headroom)
            self._step_accumulated += actual
            return actual
