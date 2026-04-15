"""Autoscaler helpers for scale-down safeguards."""

import logging
import threading

from modules.common.thresholds import ERROR_RATE_THRESHOLD
from modules.rollout import main as rollout

_logger = logging.getLogger(__name__)


class AutoScaler:
    """Evaluate scale-down conditions from runtime signals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self._CONSECUTIVE_FAILURE_THRESHOLD: int = 5

    def _scale_down(self, reason: str) -> None:
        rollout.force_rollback(reason=reason)

    def _scale_down_worker(self, worker_id: str) -> None:
        self._scale_down(
            reason=f"worker {worker_id} hit {self._CONSECUTIVE_FAILURE_THRESHOLD} consecutive failures"
        )

    def _evaluate_scale_down(self, error_rate: float) -> None:
        if error_rate > ERROR_RATE_THRESHOLD:
            self._scale_down(
                reason=(
                    f"error_rate {error_rate:.3f} exceeded threshold "
                    f"{ERROR_RATE_THRESHOLD:.3f}"
                )
            )
        with self._lock:
            failure_items = list(self._consecutive_failures.items())
        for worker_id, count in failure_items:
            if count >= self._CONSECUTIVE_FAILURE_THRESHOLD:
                self._scale_down_worker(worker_id)

    def record_failure(self, worker_id: str) -> None:
        """Record a consecutive failure for worker. Auto-triggers scale-down check."""
        with self._lock:
            self._consecutive_failures[worker_id] = self._consecutive_failures.get(worker_id, 0) + 1
            current_count = self._consecutive_failures[worker_id]
        if current_count >= self._CONSECUTIVE_FAILURE_THRESHOLD:
            _logger.warning(
                "Worker %s hit %d consecutive failures — triggering scale-down",
                worker_id,
                current_count,
            )
            self._scale_down_worker(worker_id)

    def record_success(self, worker_id: str) -> None:
        """Reset consecutive failure counter on success."""
        with self._lock:
            self._consecutive_failures[worker_id] = 0

    def get_consecutive_failures(self, worker_id: str) -> int:
        with self._lock:
            return self._consecutive_failures.get(worker_id, 0)


_autoscaler_instance: "AutoScaler | None" = None


def get_autoscaler() -> "AutoScaler":
    global _autoscaler_instance
    if _autoscaler_instance is None:
        _autoscaler_instance = AutoScaler()
    return _autoscaler_instance

