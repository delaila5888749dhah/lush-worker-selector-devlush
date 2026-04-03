"""Rollout manager — stepped production rollout with automatic rollback.

Coordinates the 1 → 3 → 5 → 10 worker scale-up, checking health metrics
from the monitor module at each step before proceeding.

This module lives in the integration layer because it orchestrates across
the monitor module and the worker pool.
"""

import threading

from modules.monitor import main as monitor

_lock = threading.Lock()

ROLLOUT_STEPS = (1, 3, 5, 10)

# Rollout state
_current_step_index = 0
_active_workers = 0
_rollback_active = False
_rollback_reasons = []


def get_current_step():
    """Return the target worker count for the current rollout step."""
    with _lock:
        return ROLLOUT_STEPS[_current_step_index]


def get_active_workers():
    """Return the number of currently active workers."""
    with _lock:
        return _active_workers


def set_active_workers(count):
    """Update the active worker count (called by the worker pool manager)."""
    global _active_workers
    with _lock:
        _active_workers = count


def is_rollback_active():
    """Return True if a rollback has been triggered."""
    with _lock:
        return _rollback_active


def get_rollback_reasons():
    """Return the list of reasons that triggered the last rollback."""
    with _lock:
        return list(_rollback_reasons)


def can_advance():
    """Check whether the rollout can advance to the next step.

    Returns True only when:
      - Not at the final step.
      - No rollback is active.
      - All monitor health checks pass.
    """
    with _lock:
        if _rollback_active:
            return False
        if _current_step_index >= len(ROLLOUT_STEPS) - 1:
            return False

    reasons = monitor.check_rollback_needed()
    return len(reasons) == 0


def advance():
    """Advance to the next rollout step.

    Saves the current metrics as a new baseline, then increments the step.

    Returns:
        The new target worker count, or None if advancement is not possible.
    """
    global _current_step_index
    with _lock:
        if _rollback_active:
            return None
        if _current_step_index >= len(ROLLOUT_STEPS) - 1:
            return None
        reasons = monitor.check_rollback_needed()
        if reasons:
            return None
        monitor.save_baseline()
        _current_step_index += 1
        return ROLLOUT_STEPS[_current_step_index]


def evaluate():
    """Evaluate current health and trigger rollback if needed.

    Returns:
        A (healthy, reasons) tuple.  ``healthy`` is True when no rollback
        conditions are triggered.  ``reasons`` lists the triggered conditions.
    """
    global _rollback_active, _rollback_reasons
    reasons = monitor.check_rollback_needed()
    if reasons:
        with _lock:
            _rollback_active = True
            _rollback_reasons = list(reasons)
        return False, reasons
    return True, []


def rollback():
    """Roll back to the previous rollout step.

    Returns:
        The new target worker count after rollback, or the current step
        count if already at step 0.
    """
    global _current_step_index, _rollback_active
    with _lock:
        if _current_step_index > 0:
            _current_step_index -= 1
        _rollback_active = False
        _rollback_reasons.clear()
        return ROLLOUT_STEPS[_current_step_index]


def get_status():
    """Return a dict summarizing the current rollout state."""
    with _lock:
        return {
            "step_index": _current_step_index,
            "target_workers": ROLLOUT_STEPS[_current_step_index],
            "active_workers": _active_workers,
            "rollback_active": _rollback_active,
            "rollback_reasons": list(_rollback_reasons),
            "is_final_step": _current_step_index >= len(ROLLOUT_STEPS) - 1,
        }


def reset():
    """Reset rollout state.  Intended for testing."""
    global _current_step_index, _active_workers, _rollback_active, _rollback_reasons
    with _lock:
        _current_step_index = 0
        _active_workers = 0
        _rollback_active = False
        _rollback_reasons = []
