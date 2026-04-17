"""Behavior decision engine — rule-based scaling decisions (Task 9.2).

Pure module: only stdlib imports (logging, threading, time).
Thread-safe via threading.Lock.  No cross-module imports.
"""
import logging
import threading
import time
from modules.common.thresholds import (
    ERROR_RATE_THRESHOLD,
    RESTART_RATE_THRESHOLD,
    SUCCESS_RATE_DROP_THRESHOLD,
    SUCCESS_RATE_MIN,
)

_logger = logging.getLogger(__name__)
_lock = threading.Lock()

SCALE_UP = "scale_up"
SCALE_DOWN = "scale_down"
HOLD = "hold"
VALID_DECISIONS = {SCALE_UP, SCALE_DOWN, HOLD}

COOLDOWN_SECONDS = 30             # minimum seconds between scaling changes
_HISTORY_LIMIT = 100              # max entries in decision ring buffer
_last_decision_time = 0.0
_decision_history = []


def _in_cooldown(now=None):
    """Return True if a cooldown period is active."""
    if _last_decision_time == 0.0:
        return False
    ts = time.monotonic() if now is None else now
    return (ts - _last_decision_time) < COOLDOWN_SECONDS


def evaluate(
    metrics: dict[str, float | int | None],
    current_step_index: int,
    max_step_index: int,
) -> tuple[str, list[str]]:
    """Evaluate metrics and return a scaling decision.

    This is the core decision function.  It applies rule-based logic to
    determine whether the system should scale up, scale down, or hold.

    Args:
        metrics: dict with keys ``error_rate``, ``success_rate``,
            ``restarts_last_hour``, ``baseline_success_rate`` (may be None).
            Values are expected to be finite floats as produced by
            ``monitor.get_metrics()``.  Non-finite values (NaN, inf) from
            custom callers are not guarded against and may produce
            unreliable decisions.
        current_step_index: zero-based index of the current scaling step.
        max_step_index: maximum step index (len(SCALE_STEPS) - 1).

    Returns:
        ``(action, reasons)`` where *action* is one of :data:`SCALE_UP`,
        :data:`SCALE_DOWN`, or :data:`HOLD`, and *reasons* is a list of
        human-readable strings explaining the decision.
    """
    global _last_decision_time

    now = time.monotonic()

    with _lock:
        # ── Rule 0: Cooldown guard ──────────────────────────────
        if _in_cooldown(now):
            return HOLD, ["cooldown_active"]

        reasons = []
        action = HOLD  # default safe action

        error_rate = metrics.get("error_rate", 0.0)
        success_rate = metrics.get("success_rate", 1.0)
        restarts = metrics.get("restarts_last_hour", 0)
        baseline = metrics.get("baseline_success_rate")

        # ── Rule 1: Scale DOWN on high error rate ───────────────
        if error_rate > ERROR_RATE_THRESHOLD:
            reasons.append(
                f"error_rate {error_rate:.1%} exceeds {ERROR_RATE_THRESHOLD:.0%}"
            )
            action = SCALE_DOWN

        # ── Rule 2: Scale DOWN on excessive restarts ────────────
        if restarts > RESTART_RATE_THRESHOLD:
            reasons.append(
                f"restarts {restarts} exceeds {RESTART_RATE_THRESHOLD}/hour"
            )
            action = SCALE_DOWN

        # ── Rule 3: Scale DOWN on success rate drop ─────────────
        if baseline is not None:
            drop = baseline - success_rate
            if drop > SUCCESS_RATE_DROP_THRESHOLD:
                reasons.append(
                    f"success_rate dropped {drop:.1%} from baseline {baseline:.1%}"
                )
                action = SCALE_DOWN

        # ── Rule 4: Scale UP only when healthy ──────────────────
        if action != SCALE_DOWN:
            if (
                error_rate <= ERROR_RATE_THRESHOLD
                and restarts <= RESTART_RATE_THRESHOLD
                and success_rate >= SUCCESS_RATE_MIN
            ):
                if current_step_index < max_step_index:
                    action = SCALE_UP
                    reasons.append("all_metrics_healthy")
                else:
                    action = HOLD
                    reasons.append("at_max_scale")
            elif not reasons:
                # Metrics not bad enough to scale down, not good enough to up
                reasons.append("metrics_marginal")

        # ── Rule 5: Never scale below step 0 ───────────────────
        if action == SCALE_DOWN and current_step_index <= 0:
            action = HOLD
            reasons.append("already_at_min_scale")

        # Record decision
        _last_decision_time = now
        _decision_history.append({
            "time": now,
            "action": action,
            "reasons": list(reasons),
            "metrics_snapshot": {
                "error_rate": error_rate,
                "success_rate": success_rate,
                "restarts_last_hour": restarts,
            },
        })
        # Keep history bounded
        if len(_decision_history) > _HISTORY_LIMIT:
            _decision_history[:] = _decision_history[-_HISTORY_LIMIT:]

        level = logging.WARNING if action == SCALE_DOWN else logging.INFO
        if action == HOLD and "cooldown_active" in reasons:
            level = logging.DEBUG
        _logger.log(level, "Behavior decision: %s — %s", action, "; ".join(reasons))
        return action, reasons


def get_decision_history():
    """Return a deep copy of recent decision history."""
    with _lock:
        return [
            {
                "time": entry["time"],
                "action": entry["action"],
                "reasons": list(entry["reasons"]),
                "metrics_snapshot": dict(entry["metrics_snapshot"]),
            }
            for entry in _decision_history
        ]


def get_last_decision_time():
    """Return the monotonic timestamp of the last decision.

    The value is produced by :func:`time.monotonic` and is only meaningful
    for computing elapsed durations — it is **not** a wall-clock epoch.
    """
    with _lock:
        return _last_decision_time


def get_status():
    """Return a snapshot of the behavior system status."""
    with _lock:
        return {
            "last_decision_time": _last_decision_time,
            "decision_count": len(_decision_history),
            "cooldown_seconds": COOLDOWN_SECONDS,
            "thresholds": {
                "error_rate": ERROR_RATE_THRESHOLD,
                "success_rate_min": SUCCESS_RATE_MIN,
                "restart_rate": RESTART_RATE_THRESHOLD,
                "success_rate_drop": SUCCESS_RATE_DROP_THRESHOLD,
            },
        }


def reset():
    """Reset all behavior state.  Intended for testing."""
    global _last_decision_time, _decision_history
    with _lock:
        _last_decision_time = 0.0
        _decision_history = []


def expire_cooldown_for_testing():
    """Force-expire the cooldown timer.  Intended for testing only."""
    global _last_decision_time
    with _lock:
        _last_decision_time = 0.0
