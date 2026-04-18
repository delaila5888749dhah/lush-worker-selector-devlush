# DEPRECATED: This scheduler is superseded by integration/runtime._runtime_loop().
# Controlled by the ROLLOUT_MANAGED_BY_RUNTIME environment variable (default: true = disabled).
"""Automatic rollout scheduler — manages production rollout via ROLLOUT_STEPS."""
import logging
import os as _os
import threading
import time
from modules.monitor import main as monitor
from modules.rollout import main as rollout

_ROLLOUT_MANAGED_BY_RUNTIME: bool = (
    _os.environ.get("ROLLOUT_MANAGED_BY_RUNTIME", "true").lower() == "true"
)

_logger = logging.getLogger(__name__)
# Single source of truth: re-export rollout.SCALE_STEPS so this legacy module
# cannot drift from the canonical scaling steps defined in modules/rollout/main.py.
ROLLOUT_STEPS = rollout.SCALE_STEPS
STABLE_DURATION_SECONDS = 43200
MIN_SUCCESS_RATE = 0.70
MAX_ERROR_RATE = 0.05
MAX_RESTARTS_PER_HOUR = 3
_lock = threading.Lock()
_stop_event = threading.Event()
_scheduler_thread = None
_stable_since = None
_MIN_INTERVAL = 1.0


def _is_stable(m):
    return (m["success_rate"] >= MIN_SUCCESS_RATE
            and m["error_rate"] <= MAX_ERROR_RATE
            and m["restarts_last_hour"] <= MAX_RESTARTS_PER_HOUR)


def _needs_rollback(m):
    """Check metrics against rollback thresholds, return list of reasons."""
    reasons = []
    if m["error_rate"] > MAX_ERROR_RATE:
        reasons.append(f"error rate {m['error_rate']:.1%} exceeds {MAX_ERROR_RATE:.0%}")
    if m["restarts_last_hour"] > MAX_RESTARTS_PER_HOUR:
        reasons.append(f"restarts {m['restarts_last_hour']} exceeds {MAX_RESTARTS_PER_HOUR}/hr")
    if m["success_rate"] < MIN_SUCCESS_RATE:
        reasons.append(f"success rate {m['success_rate']:.1%} below {MIN_SUCCESS_RATE:.0%}")
    return reasons


def _try_advance():
    global _stable_since
    workers, action, reasons = rollout.try_scale_up()
    if action == "scaled_up":
        _logger.info("advancing to step %d: %d workers",
                     rollout.get_current_step_index(), workers)
        with _lock:
            _stable_since = None
    elif action == "rollback":
        _logger.warning("rollback: %s", "; ".join(reasons))
        with _lock:
            _stable_since = None
    elif action == "at_max":
        _logger.info("rollout complete: at max workers")


def _scheduler_loop(interval):
    global _stable_since
    while not _stop_event.is_set():
        try:
            metrics = monitor.get_metrics()
            reasons = _needs_rollback(metrics)
            now = time.monotonic()
            if reasons:
                rollout.force_rollback(reason="; ".join(reasons))
                with _lock:
                    _stable_since = None
            elif _is_stable(metrics):
                with _lock:
                    if _stable_since is None:
                        _stable_since = now
                    snap = _stable_since
                if now - snap >= STABLE_DURATION_SECONDS and rollout.can_scale_up():
                    _try_advance()
            else:
                with _lock:
                    _stable_since = None
        except Exception:
            _logger.exception("scheduler loop error")
        _stop_event.wait(timeout=interval)


def start_scheduler(interval: float = 300.0) -> bool:
    """Start the rollout scheduler loop in a background thread.

    Args:
        interval: Polling interval in seconds (default 300s = 5 min).

    Returns True if started, False if already running.
    """
    with _lock:
        managed = _ROLLOUT_MANAGED_BY_RUNTIME
    if managed:
        _logger.warning(
            "rollout_scheduler: ROLLOUT_MANAGED_BY_RUNTIME=true — "
            "this legacy scheduler is disabled. All rollout decisions are "
            "handled exclusively by integration/runtime._runtime_loop(). "
            "Set ROLLOUT_MANAGED_BY_RUNTIME=false to re-enable (not recommended)."
        )
        return False
    _logger.info("Scheduler is passive; runtime module owns scaling.")
    global _scheduler_thread
    clamped = max(float(interval), _MIN_INTERVAL)
    with _lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _stop_event.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop, args=(clamped,),
            daemon=True, name="rollout-scheduler",
        )
        _scheduler_thread.start()
    return True


def stop_scheduler(timeout: float = 10.0) -> bool:
    """Stop the rollout scheduler loop.

    Returns True if stopped cleanly, False if timed out.
    """
    with _lock:
        thread = _scheduler_thread
    if thread is None or not thread.is_alive():
        return False
    _stop_event.set()
    thread.join(timeout=timeout)
    return not thread.is_alive()


def get_scheduler_status() -> dict:
    """Return scheduler status snapshot."""
    with _lock:
        running = _scheduler_thread is not None and _scheduler_thread.is_alive()
        stable_since = _stable_since
    step = rollout.get_current_step_index()
    workers = rollout.get_current_workers()
    max_idx = len(ROLLOUT_STEPS) - 1
    complete = step == max_idx
    next_workers = ROLLOUT_STEPS[step + 1] if step < max_idx else None
    now = time.monotonic()
    if stable_since is not None:
        elapsed = now - stable_since
        seconds_until = max(0.0, STABLE_DURATION_SECONDS - elapsed)
        eligible = elapsed >= STABLE_DURATION_SECONDS
    else:
        seconds_until, eligible = None, False
    return {
        "running": running, "current_step": step, "current_workers": workers,
        "next_workers": next_workers, "stable_since": stable_since,
        "seconds_until_advance": seconds_until, "advance_eligible": eligible,
        "rollout_complete": complete,
    }


def advance_step() -> tuple[bool, str]:
    """Manually trigger advance to next rollout step.

    Returns (success, reason).
    """
    global _stable_since
    if not rollout.can_scale_up():
        return False, "at max step"
    workers, action, reasons = rollout.try_scale_up()
    if action == "scaled_up":
        with _lock:
            _stable_since = None
        return True, f"advanced to {workers} workers"
    if action == "rollback":
        with _lock:
            _stable_since = None
        return False, "rollback: " + "; ".join(reasons)
    return False, action


def reset() -> None:
    """Reset scheduler state. Intended for testing."""
    global _scheduler_thread, _stable_since, _ROLLOUT_MANAGED_BY_RUNTIME  # pylint: disable=global-statement,invalid-name
    _stop_event.set()
    with _lock:
        thread = _scheduler_thread
    if thread is not None:
        thread.join(timeout=5.0)
    with _lock:
        _scheduler_thread = None
        _stable_since = None
    _stop_event.clear()
    _ROLLOUT_MANAGED_BY_RUNTIME = False
