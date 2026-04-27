"""Autoscaler helpers for scale-down safeguards."""

import logging
import threading
from typing import Dict, List, Optional, Tuple

from modules.common.thresholds import ERROR_RATE_THRESHOLD
from . import main as rollout

_logger = logging.getLogger(__name__)


class AutoScaler:
    """Evaluate scale-down conditions from runtime signals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures: Dict[str, int] = {}
        self._CONSECUTIVE_FAILURE_THRESHOLD: int = 5

    def _scale_down(self, reason: str) -> int:  # pylint: disable=no-self-use
        # Guard: when the rollout has only one scaling step (MAX_WORKER_COUNT=1)
        # there is nowhere to roll back to.  Skip the force_rollback call so we
        # don't emit spurious "1 → 1 workers" warnings from the rollout module.
        # Read status via the lock-safe rollout API rather than touching
        # SCALE_STEPS directly — SCALE_STEPS is rebound under rollout._lock by
        # reset() / configure_max_workers() / set_scale_steps().
        status = rollout.get_status()
        if status["max_step_index"] <= 0:
            _logger.info(
                "scale-down skipped: only one scaling step configured (reason=%s)",
                reason,
            )
            return status["current_workers"]
        return rollout.force_rollback(reason=reason)

    def _scale_down_worker(self, worker_id: str) -> None:
        self._scale_down(
            reason=f"worker {worker_id} hit {self._CONSECUTIVE_FAILURE_THRESHOLD} consecutive failures"
        )

    def _evaluate_scale_down(self, error_rate: float = 0.0) -> Optional[int]:
        """On-demand scale-down evaluation.

        Two distinct sub-paths share this method:

        1. **Per-worker failure path** (production-wired). Called with the
           default ``error_rate=0.0`` by ``get_recommended_scale_down_target``,
           which is invoked from ``integration.runtime._runtime_loop`` every
           tick.  Any worker whose ``_consecutive_failures`` counter has
           crossed ``_CONSECUTIVE_FAILURE_THRESHOLD`` is scaled down
           individually.  The global error-rate threshold check is skipped.

        2. **Global error-rate path** (Option B — *external-only / opt-in*,
           Blueprint §14.5). When ``error_rate > ERROR_RATE_THRESHOLD`` the
           whole service is scaled down via ``_scale_down``.  This branch is
           **deliberately not wired into any automatic production loop**; the
           production global-error-rate response is owned by the behavior path
           (``behavior.evaluate`` → runtime → ``rollout.force_rollback`` under
           the ``_is_safe_locked`` safety gate, see Blueprint §14.1).  Wire-in
           here would duplicate that path *without* the safety gate, so the
           ``error_rate`` argument is reserved for explicit external triggers
           — e.g. a monitoring dashboard, an emergency-rollback CLI, or an
           integration test asserting the threshold contract.  Callers that
           opt in supply ``error_rate`` in ``[0.0, 1.0]``; negative values are
           treated the same as ``0.0`` (threshold check not triggered).

        Returns:
            The recommended target worker count when scale-down is triggered,
            or ``None`` when no action is warranted.
        """
        if error_rate > ERROR_RATE_THRESHOLD:
            return self._scale_down(
                reason=(
                    f"error_rate {error_rate:.3f} exceeded threshold "
                    f"{ERROR_RATE_THRESHOLD:.3f}"
                )
            )
        with self._lock:
            workers_to_scale: List[Tuple[str, int]] = []
            for worker_id, count in self._consecutive_failures.items():
                if count >= self._CONSECUTIVE_FAILURE_THRESHOLD:
                    workers_to_scale.append((worker_id, count))
            if not workers_to_scale:
                return None
        # Lock is released before calling _scale_down_worker() to avoid
        # deadlocking inside rollout.force_rollback().  workers_to_scale is a
        # stable snapshot; the counter re-check inside the loop (line below)
        # ensures we only reset counts that have not changed since the snapshot.
        for worker_id, count in workers_to_scale:
            self._scale_down_worker(worker_id)
            with self._lock:
                if self._consecutive_failures.get(worker_id, 0) == count:
                    self._consecutive_failures[worker_id] = 0
        return rollout.get_current_workers()

    def get_recommended_scale_down_target(self) -> Optional[int]:
        """Return a recommended worker count if scale-down is warranted, else None.

        Delegates to _evaluate_scale_down(). Previously this method had no call site
        (dead code). Now called by integration/runtime._runtime_loop().
        """
        return self._evaluate_scale_down()

    def record_failure(self, worker_id: str) -> None:
        """Record a consecutive failure for worker. Auto-triggers scale-down check.

        Note (Blueprint §14.1): when the per-worker failure threshold is reached
        this path calls ``rollout.force_rollback()`` directly without going
        through the runtime ``_is_safe_locked()`` safety gate. This is the
        intentional emergency scale-down behaviour — repeated worker failures
        must shrink the pool immediately, even when other workers are mid-cycle.
        """
        with self._lock:
            self._consecutive_failures[worker_id] = self._consecutive_failures.get(worker_id, 0) + 1
            current_count = self._consecutive_failures[worker_id]
            should_scale = current_count >= self._CONSECUTIVE_FAILURE_THRESHOLD
        if should_scale:
            _logger.warning(
                "Worker %s hit %d consecutive failures — triggering scale-down",
                worker_id,
                current_count,
            )
            try:
                self._scale_down_worker(worker_id)
            except Exception:  # noqa: BLE001  # pylint: disable=broad-except
                _logger.exception(
                    "scale-down for worker %s failed; failure count retained for retry",
                    worker_id,
                )
                return
            with self._lock:
                # Only reset if no new failures arrived during the scale-down
                # call; if new failures came in, leave the count as-is so the
                # signal is preserved for the next trigger.
                if self._consecutive_failures.get(worker_id, 0) == current_count:
                    self._consecutive_failures[worker_id] = 0

    def record_success(self, worker_id: str) -> None:
        """Reset consecutive failure counter on success."""
        with self._lock:
            self._consecutive_failures[worker_id] = 0

    def get_consecutive_failures(self, worker_id: str) -> int:
        with self._lock:
            return self._consecutive_failures.get(worker_id, 0)

    def _reset_internal_state_for_test(self) -> None:
        """Clear consecutive-failure counters under the instance lock.

        Internal helper used by the module-level ``reset()`` so that callers
        do not need to reach into protected attributes from outside the
        class. Not part of the public API; intended for testing and runtime
        lifecycle restarts only.
        """
        with self._lock:
            self._consecutive_failures.clear()


_autoscaler_instance: Optional["AutoScaler"] = None  # pylint: disable=invalid-name
_autoscaler_lock = threading.Lock()


def get_autoscaler() -> "AutoScaler":
    global _autoscaler_instance
    if _autoscaler_instance is None:
        with _autoscaler_lock:
            if _autoscaler_instance is None:
                _autoscaler_instance = AutoScaler()
    return _autoscaler_instance


def reset() -> None:
    """Reset autoscaler state. Intended for testing and runtime lifecycle restarts."""
    with _autoscaler_lock:
        instance = _autoscaler_instance
    if instance is not None:
        instance._reset_internal_state_for_test()  # pylint: disable=protected-access
