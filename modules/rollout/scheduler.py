"""Rollout scheduler: stable-window tracking and interval management."""

import logging
import math
import threading
import time
from typing import Optional, Tuple

from modules.rollout import main as rollout

_logger = logging.getLogger(__name__)

STABLE_DURATION_SECONDS: float = 43200.0
_MIN_INTERVAL: float = 1.0
_MAX_INTERVAL: float = 86400.0
_DEFAULT_INTERVAL: float = 300.0

_lock = threading.Lock()
_stop_event = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None  # pylint: disable=invalid-name
# Stable-window anchor; all reads/writes must hold _lock.
_stable_since: Optional[float] = None  # pylint: disable=invalid-name
_is_stable_fn = None  # pylint: disable=invalid-name


def _clamp_interval(interval) -> float:
    """Clamp *interval* to [_MIN_INTERVAL, _MAX_INTERVAL]; handles NaN/inf."""
    try:
        interval_val = float(interval)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL
    if not math.isfinite(interval_val) or interval_val < _MIN_INTERVAL:
        return _MIN_INTERVAL
    return min(interval_val, _MAX_INTERVAL)


def configure(is_stable_fn=None) -> None:
    """Inject the stability-check callback used by the scheduler loop."""
    global _is_stable_fn  # pylint: disable=global-statement,invalid-name
    with _lock:
        _is_stable_fn = is_stable_fn


def _reset_stable_locked() -> None:
    """Reset the stable-window anchor. Caller must hold _lock."""
    global _stable_since  # pylint: disable=global-statement,invalid-name
    _stable_since = None


def _do_advance() -> None:
    """Attempt to advance to the next rollout step and reset stable window."""
    global _stable_since  # pylint: disable=global-statement,invalid-name
    workers, action, reasons = rollout.try_scale_up()
    if action == "scaled_up":
        _logger.info(
            "rollout advanced to step %d: %d workers",
            rollout.get_current_step_index(),
            workers,
        )
    elif action == "rollback":
        _logger.warning("rollback triggered: %s", "; ".join(reasons))
    elif action == "at_max":
        _logger.info("rollout complete: at max workers")
        return
    else:
        return
    with _lock:
        _reset_stable_locked()


def _scheduler_loop(interval: float) -> None:
    """Main scheduler loop; eligibility decided under lock (no TOCTOU)."""
    global _stable_since  # pylint: disable=global-statement,invalid-name
    while not _stop_event.is_set():
        try:
            now = time.monotonic()
            with _lock:
                is_stable_fn = _is_stable_fn
            stable = is_stable_fn() if is_stable_fn is not None else False
            if not stable:
                with _lock:
                    _reset_stable_locked()
            else:
                with _lock:
                    if _stable_since is None:
                        _stable_since = now
                    snap = _stable_since
                    eligible = (now - snap) >= STABLE_DURATION_SECONDS
                if eligible and rollout.can_scale_up():
                    _do_advance()
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.exception("scheduler loop error")
        _stop_event.wait(timeout=interval)


def start(interval: float = _DEFAULT_INTERVAL) -> bool:
    """Start the scheduler loop in a background daemon thread."""
    global _scheduler_thread  # pylint: disable=global-statement,invalid-name
    safe_interval = _clamp_interval(interval)
    with _lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _stop_event.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            args=(safe_interval,),
            daemon=True,
            name="rollout-scheduler",
        )
        _scheduler_thread.start()
    return True


def stop(timeout: float = 10.0) -> bool:
    """Stop the scheduler loop."""
    with _lock:
        thread = _scheduler_thread
    if thread is None or not thread.is_alive():
        return False
    _stop_event.set()
    thread.join(timeout=timeout)
    return not thread.is_alive()


def get_status() -> dict:
    """Return a scheduler status snapshot."""
    with _lock:
        running = _scheduler_thread is not None and _scheduler_thread.is_alive()
        stable_since = _stable_since
    step = rollout.get_current_step_index()
    workers = rollout.get_current_workers()
    max_idx = len(rollout.SCALE_STEPS) - 1
    complete = step == max_idx
    next_workers = rollout.SCALE_STEPS[step + 1] if step < max_idx else None
    now = time.monotonic()
    if stable_since is not None:
        elapsed = now - stable_since
        seconds_until = max(0.0, STABLE_DURATION_SECONDS - elapsed)
        eligible = elapsed >= STABLE_DURATION_SECONDS
    else:
        seconds_until, eligible = None, False
    return {
        "running": running,
        "current_step": step,
        "current_workers": workers,
        "next_workers": next_workers,
        "stable_since": stable_since,
        "seconds_until_advance": seconds_until,
        "advance_eligible": eligible,
        "rollout_complete": complete,
    }


def advance_step() -> Tuple[bool, str]:
    """Manually trigger advance to the next rollout step."""
    global _stable_since  # pylint: disable=global-statement,invalid-name
    if not rollout.can_scale_up():
        return False, "at max step"
    workers, action, reasons = rollout.try_scale_up()
    if action == "scaled_up":
        with _lock:
            _reset_stable_locked()
        return True, f"advanced to {workers} workers"
    if action == "rollback":
        with _lock:
            _reset_stable_locked()
        return False, "rollback: " + "; ".join(reasons)
    return False, action


def reset() -> None:
    """Reset all scheduler state. Intended for testing."""
    global _scheduler_thread, _stable_since  # pylint: disable=global-statement,invalid-name
    _stop_event.set()
    with _lock:
        thread = _scheduler_thread
    if thread is not None:
        thread.join(timeout=5.0)
    with _lock:
        _scheduler_thread = None
        _reset_stable_locked()
    _stop_event.clear()
