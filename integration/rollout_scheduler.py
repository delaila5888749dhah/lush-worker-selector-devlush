"""DEPRECATED shim — use integration.runtime instead. Removal in next major version.

This module previously hosted the gradual rollout scheduler
(``ROLLOUT_STEPS`` → 1/3/5/10 workers).  Its responsibilities have been
absorbed by ``integration.runtime._runtime_loop``, which now owns every
rollout decision.

The public surface (``start_scheduler``, ``stop_scheduler``,
``get_scheduler_status``, ``advance_step``, ``reset``) is preserved for
backward compatibility but every call emits a :class:`DeprecationWarning`
and either returns a no-op value or delegates to ``integration.runtime``.
"""
import logging
import warnings

from modules.rollout import main as rollout

_logger = logging.getLogger(__name__)

# Legacy constants — kept as re-exports so external callers that referenced
# ``rollout_scheduler.ROLLOUT_STEPS`` continue to observe the canonical
# scale steps defined in ``modules/rollout/main.py``.
ROLLOUT_STEPS = rollout.SCALE_STEPS
STABLE_DURATION_SECONDS = 43200
MIN_SUCCESS_RATE = 0.70
MAX_ERROR_RATE = 0.05
MAX_RESTARTS_PER_HOUR = 3

_DEPRECATION_MSG = (
    "integration.rollout_scheduler is deprecated; "
    "use integration.runtime for rollout control. "
    "This shim will be removed in the next major version."
)


def _warn_deprecated(api: str) -> None:
    warnings.warn(
        "%s: %s" % (api, _DEPRECATION_MSG),
        DeprecationWarning,
        stacklevel=3,
    )
    _logger.warning("rollout_scheduler.%s called on deprecated shim", api)


def start_scheduler(interval: float = 300.0) -> bool:  # pylint: disable=unused-argument
    """DEPRECATED: no-op. Rollout is owned by ``integration.runtime``."""
    _warn_deprecated("start_scheduler")
    return False


def stop_scheduler(timeout: float = 10.0) -> bool:  # pylint: disable=unused-argument
    """DEPRECATED: no-op. There is no legacy scheduler thread to stop."""
    _warn_deprecated("stop_scheduler")
    return False


def get_scheduler_status() -> dict:
    """DEPRECATED: return a status dict shaped like the legacy payload."""
    _warn_deprecated("get_scheduler_status")
    step = rollout.get_current_step_index()
    workers = rollout.get_current_workers()
    max_idx = len(ROLLOUT_STEPS) - 1
    next_workers = ROLLOUT_STEPS[step + 1] if step < max_idx else None
    return {
        "running": False,
        "current_step": step,
        "current_workers": workers,
        "next_workers": next_workers,
        "stable_since": None,
        "seconds_until_advance": None,
        "advance_eligible": False,
        "rollout_complete": step == max_idx,
    }


def advance_step() -> tuple:
    """DEPRECATED: no-op. Returns ``(False, 'deprecated')``."""
    _warn_deprecated("advance_step")
    return False, "deprecated"


def reset() -> None:
    """DEPRECATED: no-op, retained for backward-compatible tests."""
    _warn_deprecated("reset")
