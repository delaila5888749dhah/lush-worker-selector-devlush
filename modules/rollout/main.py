"""Rollout system for progressive worker scaling with automatic rollback."""

import logging
import os
import threading

_logger = logging.getLogger(__name__)

_lock = threading.Lock()

# Default scaling steps (cap at 10 workers).  The tuple is rebuilt at import
# time — and whenever :func:`reset` is called — based on the ``MAX_WORKER_COUNT``
# environment variable so operators can configure the exact worker cap.
_DEFAULT_SCALE_STEPS = (1, 3, 5, 10)
_DEFAULT_MAX_WORKER_COUNT = 10
# Canonical progression used to pick intermediate steps between 10 and the cap.
_DECADE_MULTIPLIERS = (2, 5, 10)


def _read_max_worker_count():
    """Return the target maximum worker count from the environment.

    Falls back to :data:`_DEFAULT_MAX_WORKER_COUNT` (10) when the env var is
    unset, empty, non-numeric, or less than 1.  A warning is logged on invalid
    input so the misconfiguration is observable.
    """
    raw = os.environ.get("MAX_WORKER_COUNT", "").strip()
    if not raw:
        return _DEFAULT_MAX_WORKER_COUNT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _logger.warning(
            "Invalid MAX_WORKER_COUNT=%r; falling back to default %d",
            raw, _DEFAULT_MAX_WORKER_COUNT,
        )
        return _DEFAULT_MAX_WORKER_COUNT
    if value < 1:
        _logger.warning(
            "MAX_WORKER_COUNT=%d is below 1; falling back to default %d",
            value, _DEFAULT_MAX_WORKER_COUNT,
        )
        return _DEFAULT_MAX_WORKER_COUNT
    return value


def _build_scale_steps(max_count):
    """Build the scaling-step tuple for a given maximum worker count.

    ``max_count`` is the operator-configured true upper bound.  The returned
    tuple always progresses strictly upward and always ends with exactly
    ``max_count``.  For ``max_count`` in ``1..10`` the canonical default
    steps ``(1, 3, 5, 10)`` are filtered to values strictly below
    ``max_count`` before the cap is appended — so ``1 → (1,)``,
    ``2 → (1, 2)``, ``4 → (1, 3, 4)``, ``7 → (1, 3, 5, 7)``, and
    ``10 → (1, 3, 5, 10)``.  For ``max_count > 10`` the canonical
    ``(1, 3, 5, 10)`` prefix is kept and extended with a 2/5/10 decade
    progression (``20, 50, 100, 200, 500, …``) up to — but not including —
    ``max_count``, which is then appended as the final step.
    """
    if max_count <= 1:
        return (1,)
    steps = [value for value in _DEFAULT_SCALE_STEPS if value < max_count]
    if max_count > _DEFAULT_MAX_WORKER_COUNT:
        decade = 10
        # Guard against pathological inputs: the progression grows by 10× each
        # outer iteration, so 20 iterations already reaches 10**21.
        for _ in range(20):
            appended_any = False
            reached_cap = False
            for multiplier in _DECADE_MULTIPLIERS:
                value = decade * multiplier
                if value >= max_count:
                    reached_cap = True
                    break
                steps.append(value)
                appended_any = True
            if reached_cap or not appended_any:
                break
            decade *= 10
    steps.append(max_count)
    return tuple(steps)


# Scaling steps — evaluated at import time from the current environment.
SCALE_STEPS = _build_scale_steps(_read_max_worker_count())

# Runtime configuration overrides.  When either is non-``None``, :func:`reset`
# rebuilds :data:`SCALE_STEPS` from the override instead of the environment so
# that values installed via :func:`configure_max_workers` / :func:`set_scale_steps`
# survive subsequent ``reset()`` calls (e.g. from ``integration/runtime.py``).
# ``_runtime_scale_steps`` takes precedence over ``_runtime_max_worker_count``.
# These are mutable runtime state, not constants — lowercase naming is
# intentional; suppress Pylint's constant-naming heuristic.
_runtime_max_worker_count = None  # pylint: disable=invalid-name
_runtime_scale_steps = None  # pylint: disable=invalid-name

# Current step index into SCALE_STEPS
_current_step_index = 0

# Callbacks injected by the orchestration layer
_check_rollback_fn = None
_save_baseline_fn = None

# History of rollback events
_ROLLBACK_HISTORY = []
_ROLLBACK_HISTORY_LIMIT = 200

# Guard: at most one forced rollback per scale-up window.
# Reset to False at the start of every try_scale_up() call.
_ROLLBACK_APPLIED = False


def configure(check_rollback_fn=None, save_baseline_fn=None):  # pylint: disable=global-statement
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


def try_scale_up():  # pylint: disable=global-statement
    """Attempt to advance to the next scaling step.

    Before scaling up, the injected *check_rollback_fn* is called.  If it
    returns any reasons, a rollback is performed instead.

    The entire check-then-act sequence is performed under ``_lock`` to
    prevent TOCTOU races where two concurrent callers could both pass the
    health check and both scale up (or one scales up while the other
    rolls back).

    If ``save_fn()`` raises, ``_current_step_index`` is restored to its
    pre-increment value so in-memory state stays consistent with persisted
    state.

    Returns:
        ``(worker_count, action, reasons)`` where *action* is one of
        ``"scaled_up"``, ``"rollback"``, or ``"at_max"``, and *reasons*
        is the list of triggered rollback conditions (empty when healthy).
    """
    global _current_step_index, _ROLLBACK_APPLIED

    with _lock:
        # Re-arm the rollback guard at the start of every scale-up attempt so
        # that a persistent save_fn failure can never permanently block
        # force_rollback() (Bug fix: _rollback_applied permanent block).
        _ROLLBACK_APPLIED = False

        if _current_step_index >= len(SCALE_STEPS) - 1:
            return SCALE_STEPS[_current_step_index], "at_max", []

        check_fn = _check_rollback_fn
        save_fn = _save_baseline_fn

        # Call health check under lock to prevent TOCTOU race.
        # The check_fn (monitor.check_rollback_needed) is lightweight —
        # it only reads in-memory counters, no I/O.
        reasons = check_fn() if check_fn is not None else []

        if reasons:
            old_index = _current_step_index
            if _current_step_index > 0:
                _current_step_index -= 1
            _ROLLBACK_HISTORY.append({
                "from_step": old_index,
                "to_step": _current_step_index,
                "reasons": list(reasons),
            })
            if len(_ROLLBACK_HISTORY) > _ROLLBACK_HISTORY_LIMIT:
                _ROLLBACK_HISTORY[:] = _ROLLBACK_HISTORY[-_ROLLBACK_HISTORY_LIMIT:]
            _logger.warning(
                "Rollback %d → %d workers: %s",
                SCALE_STEPS[old_index],
                SCALE_STEPS[_current_step_index],
                "; ".join(reasons),
            )
            return SCALE_STEPS[_current_step_index], "rollback", reasons

        prev_step = _current_step_index
        _current_step_index += 1
        # Capture intended_step inside the lock before releasing it for
        # save_fn().  The except block uses this to detect whether a concurrent
        # force_rollback() changed _current_step_index while save_fn() ran
        # (TOCTOU guard: only revert if _current_step_index == intended_step).
        intended_step = _current_step_index
        new_count = SCALE_STEPS[_current_step_index]

    # save_fn() is intentionally called outside the lock to avoid blocking
    # other callers during I/O.  If it raises, revert _current_step_index —
    # but only when no concurrent force_rollback() has already changed it
    # (TOCTOU guard: compare against intended_step before reverting).
    try:
        if save_fn is not None:
            save_fn()
    except Exception:
        with _lock:
            if _current_step_index == intended_step:
                # No concurrent change: revert our own increment.
                # _ROLLBACK_APPLIED is already False (reset at call start) so
                # force_rollback() remains armed for the restored window.
                _current_step_index = prev_step
            elif _current_step_index == prev_step:
                # A concurrent force_rollback() has already decremented back to
                # prev_step while save_fn() was running.  Re-arm the rollback
                # guard for the restored window so it retains its rollback budget.
                _ROLLBACK_APPLIED = False
        _logger.error(
            "save_fn failed in try_scale_up (prev_step=%d, intended_step=%d); step reverted.",
            prev_step,
            intended_step,
            exc_info=True,
        )
        raise

    _logger.info(
        "Scaled up to %d workers (step %d/%d)",
        new_count,
        intended_step + 1,
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


def force_rollback(reason="manual"):  # pylint: disable=global-statement
    """Force an immediate rollback by one step.

    At most one forced rollback is applied per scale-up window.  If another
    caller already applied a forced rollback in the current window,
    subsequent calls are treated as idempotent and return the current worker
    count without decrementing ``_current_step_index`` again.  The window
    resets at the start of every ``try_scale_up()`` call.

    Args:
        reason: Description of why the rollback was forced.

    Returns:
        The new target worker count after rollback.
    """
    global _current_step_index, _ROLLBACK_APPLIED
    with _lock:
        if _ROLLBACK_APPLIED:
            _logger.debug(
                "force_rollback skipped: rollback already applied in current window (step %d)",
                _current_step_index,
            )
            return SCALE_STEPS[_current_step_index]
        old_index = _current_step_index
        if _current_step_index > 0:
            _current_step_index -= 1
        _ROLLBACK_APPLIED = True
        _ROLLBACK_HISTORY.append({
            "from_step": old_index,
            "to_step": _current_step_index,
            "reasons": [reason],
        })
        if len(_ROLLBACK_HISTORY) > _ROLLBACK_HISTORY_LIMIT:
            _ROLLBACK_HISTORY[:] = _ROLLBACK_HISTORY[-_ROLLBACK_HISTORY_LIMIT:]
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
            for entry in _ROLLBACK_HISTORY
        ]


def get_status():
    """Return a snapshot of the current rollout status as a dict."""
    with _lock:
        return {
            "current_workers": SCALE_STEPS[_current_step_index],
            "step_index": _current_step_index,
            "max_step_index": len(SCALE_STEPS) - 1,
            "can_scale_up": _current_step_index < len(SCALE_STEPS) - 1,
            "rollback_count": len(_ROLLBACK_HISTORY),
        }


def reset():  # pylint: disable=global-statement
    """Reset all rollout state.  Intended for testing.

    Rebuilds :data:`SCALE_STEPS` from the most recently installed runtime
    configuration if any (:func:`configure_max_workers` or
    :func:`set_scale_steps`), otherwise from the ``MAX_WORKER_COUNT``
    environment variable.  Runtime overrides are preserved across
    ``reset()`` so integration layers may safely reset rollout state
    without dropping an operator-configured cap.
    """
    global _current_step_index, _check_rollback_fn, _save_baseline_fn
    global _ROLLBACK_HISTORY, _ROLLBACK_APPLIED, SCALE_STEPS
    with _lock:
        if _runtime_scale_steps is not None:
            SCALE_STEPS = _runtime_scale_steps
        elif _runtime_max_worker_count is not None:
            SCALE_STEPS = _build_scale_steps(_runtime_max_worker_count)
        else:
            SCALE_STEPS = _build_scale_steps(_read_max_worker_count())
        _current_step_index = 0
        _check_rollback_fn = None
        _save_baseline_fn = None
        _ROLLBACK_HISTORY = []
        _ROLLBACK_APPLIED = False


# Supported range for the operator-configurable worker cap.
_MIN_MAX_WORKER_COUNT = 1
_MAX_MAX_WORKER_COUNT = 50


def configure_max_workers(count: int) -> None:
    """Set the worker cap and re-derive :data:`SCALE_STEPS` at runtime.

    Must be called before the rollout scheduler loop is started.  Validates
    ``count`` is an ``int`` in ``[1, 50]``, installs it as the runtime cap
    override (so subsequent :func:`reset` calls rebuild from it instead of
    the environment), and resets rollout state.

    Args:
        count: Target maximum worker count.

    Raises:
        TypeError: If ``count`` is not an ``int`` (``bool`` is rejected too).
        ValueError: If ``count`` is outside ``[1, 50]``.
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError(
            f"MAX_WORKER_COUNT must be int, got {type(count).__name__}"
        )
    if not _MIN_MAX_WORKER_COUNT <= count <= _MAX_MAX_WORKER_COUNT:
        raise ValueError(
            f"MAX_WORKER_COUNT={count} out of range "
            f"[{_MIN_MAX_WORKER_COUNT},{_MAX_MAX_WORKER_COUNT}]"
        )
    global _runtime_max_worker_count, _runtime_scale_steps  # pylint: disable=global-statement,invalid-name
    with _lock:
        _runtime_max_worker_count = count
        _runtime_scale_steps = None
    # reset() picks up the override we just installed and rebuilds SCALE_STEPS
    # from it, so the cap survives subsequent reset() calls.
    reset()
    _logger.info(
        "configure_max_workers: cap=%d steps=%s", count, SCALE_STEPS
    )


def set_scale_steps(steps) -> None:
    """Install an explicit scaling-step tuple and reset rollout state.

    The supplied ``steps`` must be a non-empty iterable of positive ``int``s
    that is strictly ascending, starts at ``1``, and whose final element is
    at most ``50``.  Starting at ``1`` is a rollout invariant (initial worker
    count must always be ``1``) so this helper is primarily intended for
    installing custom progressions in tests; production code should prefer
    :func:`configure_max_workers`.

    The installed tuple is preserved across subsequent :func:`reset` calls
    until another override (via this function or
    :func:`configure_max_workers`) replaces it.

    Args:
        steps: Iterable of ``int`` scaling steps (e.g. ``(1, 2, 4)``).

    Raises:
        TypeError: If ``steps`` is not iterable or contains non-ints.
        ValueError: If ``steps`` is empty, does not start at ``1``, is not
            strictly ascending, contains a non-positive value, or exceeds
            the ``50`` cap.
    """
    try:
        candidate = tuple(steps)
    except TypeError as exc:
        raise TypeError(f"steps must be iterable, got {type(steps).__name__}") from exc
    if not candidate:
        raise ValueError("steps must be non-empty")
    for value in candidate:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                f"steps must contain ints, got {type(value).__name__}"
            )
        if value < 1:
            raise ValueError(f"steps must be positive, got {value}")
    if candidate[0] != 1:
        raise ValueError(
            f"steps must start at 1 (rollout invariant), got {candidate[0]}"
        )
    for prev, nxt in zip(candidate, candidate[1:]):
        if nxt <= prev:
            raise ValueError(f"steps must be strictly ascending: {candidate}")
    if candidate[-1] > _MAX_MAX_WORKER_COUNT:
        raise ValueError(
            f"final step {candidate[-1]} exceeds cap {_MAX_MAX_WORKER_COUNT}"
        )
    global _runtime_max_worker_count, _runtime_scale_steps  # pylint: disable=global-statement,invalid-name
    with _lock:
        _runtime_scale_steps = candidate
        _runtime_max_worker_count = None
    reset()
    _logger.info("set_scale_steps: steps=%s", candidate)
