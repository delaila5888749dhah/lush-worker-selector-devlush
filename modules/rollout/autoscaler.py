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
        return rollout.force_rollback(reason=reason)

    def _scale_down_worker(self, worker_id: str) -> None:
        self._scale_down(
            reason=f"worker {worker_id} hit {self._CONSECUTIVE_FAILURE_THRESHOLD} consecutive failures"
        )

    def _evaluate_scale_down(self, error_rate: float = 0.0) -> Optional[int]:
        """On-demand scale-down evaluation driven by an external error-rate signal.

        This is an explicit external-trigger API and is *not* called by the
        automatic per-failure path (``record_failure``).  Callers supply an
        aggregate ``error_rate``; if it exceeds ``ERROR_RATE_THRESHOLD`` the
        whole service is scaled down via ``_scale_down``, otherwise any workers
        whose accumulated failure count has already crossed the threshold are
        scaled down individually.

        When called with ``error_rate=0.0`` (the default, used by
        ``get_recommended_scale_down_target``), the global error-rate threshold
        check is skipped and only per-worker failure counts are evaluated.
        ``error_rate`` is expected to be in the range ``[0.0, 1.0]``; negative
        values are treated the same as ``0.0`` (threshold check not triggered).

        Production path status: not wired into any automatic call-site.  Invoke
        this method directly when reacting to an external error-rate metric
        (e.g. from a monitoring dashboard or a periodic health-check loop).

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
        """Record a consecutive failure for worker. Auto-triggers scale-down check."""
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
        with instance._lock:  # pylint: disable=protected-access
            instance._consecutive_failures.clear()  # pylint: disable=protected-access
