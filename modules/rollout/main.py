"""Rollout system for progressive worker scaling with automatic rollback."""

import logging
import threading

_logger = logging.getLogger(__name__)

_lock = threading.Lock()

# Scaling steps
SCALE_STEPS = (1, 3, 5, 10)

# Current step index into SCALE_STEPS
_current_step_index = 0

# Callbacks injected by the orchestration layer
_check_rollback_fn = None
_save_baseline_fn = None

# History of rollback events
_rollback_history = []
_ROLLBACK_HISTORY_LIMIT = 200


def configure(check_rollback_fn=None, save_baseline_fn=None):
    """Inject monitor callbacks.  Called by the orchestration layer at init.

    Args:
        check_rollback_fn: Callable returning a list of rollback reason
            strings.  Empty list means healthy.
        save_baseline_fn: Callable that snapshots the current success rate
            as baseline for the next scaling step.
    """
    global _check_rollback_fn, _save_baseline_fn
    with _lock:
        _check_rollback_fn = check_rollback_fn
        _save_baseline_fn = save_baseline_fn


def is_configured() -> bool:
    """Return True if monitor callbacks have been injected."""
    with _lock:
        return _check_rollback_fn is not None and _save_baseline_fn is not None


def get_current_workers():
    """Return the current target worker count."""
    with _lock:
        return SCALE_STEPS[_current_step_index]


def get_current_step_index():
    """Return the zero-based index of the current scaling step."""
    with _lock:
        return _current_step_index


def can_scale_up():
    """Return True if a higher scaling step is available."""
    with _lock:
        return _current_step_index < len(SCALE_STEPS) - 1


def try_scale_up():
    """Attempt to advance to the next scaling step.

    Before scaling up, the injected *check_rollback_fn* is called.  If it
    returns any reasons, a rollback is performed instead.

    Returns:
        ``(worker_count, action, reasons)`` where *action* is one of
        ``"scaled_up"``, ``"rollback"``, or ``"at_max"``, and *reasons*
        is the list of triggered rollback conditions (empty when healthy).
    """
    global _current_step_index

    with _lock:
        if _current_step_index >= len(SCALE_STEPS) - 1:
            return SCALE_STEPS[_current_step_index], "at_max", []

        check_fn = _check_rollback_fn
        save_fn = _save_baseline_fn

    # Call callbacks outside the lock to avoid holding it during I/O
    reasons = check_fn() if check_fn is not None else []

    with _lock:
        if reasons:
            old_index = _current_step_index
            if _current_step_index > 0:
                _current_step_index -= 1
            _rollback_history.append({
                "from_step": old_index,
                "to_step": _current_step_index,
                "reasons": list(reasons),
            })
            if len(_rollback_history) > _ROLLBACK_HISTORY_LIMIT:
                _rollback_history[:] = _rollback_history[-_ROLLBACK_HISTORY_LIMIT:]
            _logger.warning(
                "Rollback %d → %d workers: %s",
                SCALE_STEPS[old_index],
                SCALE_STEPS[_current_step_index],
                "; ".join(reasons),
            )
            return SCALE_STEPS[_current_step_index], "rollback", reasons

        if _current_step_index >= len(SCALE_STEPS) - 1:
            return SCALE_STEPS[_current_step_index], "at_max", []

        _current_step_index += 1
        new_count = SCALE_STEPS[_current_step_index]
        new_step = _current_step_index

    # Save baseline outside the lock
    if save_fn is not None:
        save_fn()

    _logger.info(
        "Scaled up to %d workers (step %d/%d)",
        new_count,
        new_step + 1,
        len(SCALE_STEPS),
    )
    return new_count, "scaled_up", []


def check_health():
    """Evaluate current health without changing the scaling step.

    Returns:
        A list of triggered rollback condition descriptions.
        Empty means healthy.
    """
    with _lock:
        check_fn = _check_rollback_fn
    if check_fn is not None:
        return check_fn()
    return []


def force_rollback(reason="manual"):
    """Force an immediate rollback by one step.

    Args:
        reason: Description of why the rollback was forced.

    Returns:
        The new target worker count after rollback.
    """
    global _current_step_index
    with _lock:
        old_index = _current_step_index
        if _current_step_index > 0:
            _current_step_index -= 1
        _rollback_history.append({
            "from_step": old_index,
            "to_step": _current_step_index,
            "reasons": [reason],
        })
        if len(_rollback_history) > _ROLLBACK_HISTORY_LIMIT:
            _rollback_history[:] = _rollback_history[-_ROLLBACK_HISTORY_LIMIT:]
        _logger.warning(
            "Forced rollback %d → %d workers: %s",
            SCALE_STEPS[old_index],
            SCALE_STEPS[_current_step_index],
            reason,
        )
        return SCALE_STEPS[_current_step_index]


def get_rollback_history():
    """Return a copy of the rollback event history."""
    with _lock:
        return [
            {
                "from_step": entry["from_step"],
                "to_step": entry["to_step"],
                "reasons": list(entry["reasons"]),
            }
            for entry in _rollback_history
        ]


def get_status():
    """Return a snapshot of the current rollout status as a dict."""
    with _lock:
        return {
            "current_workers": SCALE_STEPS[_current_step_index],
            "step_index": _current_step_index,
            "max_step_index": len(SCALE_STEPS) - 1,
            "can_scale_up": _current_step_index < len(SCALE_STEPS) - 1,
            "rollback_count": len(_rollback_history),
        }


def reset():
    """Reset all rollout state.  Intended for testing."""
    global _current_step_index, _check_rollback_fn, _save_baseline_fn
    global _rollback_history
    with _lock:
        _current_step_index = 0
        _check_rollback_fn = None
        _save_baseline_fn = None
        _rollback_history = []
