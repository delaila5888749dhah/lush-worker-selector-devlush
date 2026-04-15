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
            return
        with self._lock:
            workers_to_scale: List[Tuple[str, int]] = []
            for worker_id, count in self._consecutive_failures.items():
                if count >= self._CONSECUTIVE_FAILURE_THRESHOLD:
                    workers_to_scale.append((worker_id, count))
                    self._consecutive_failures[worker_id] = 0
        for worker_id, _ in workers_to_scale:
            self._scale_down_worker(worker_id)

    def record_failure(self, worker_id: str) -> None:
        """Record a consecutive failure for worker. Auto-triggers scale-down check."""
        with self._lock:
            self._consecutive_failures[worker_id] = self._consecutive_failures.get(worker_id, 0) + 1
            current_count = self._consecutive_failures[worker_id]
            should_scale = current_count >= self._CONSECUTIVE_FAILURE_THRESHOLD
            if should_scale:
                self._consecutive_failures[worker_id] = 0
        if should_scale:
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
