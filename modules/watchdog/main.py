"""Watchdog — Per-worker network monitor with cross-thread safe notify.

Uses a worker_id-keyed registry so that CDP callbacks fired from any thread
(including the browser's internal event thread) can signal the correct
waiting worker without threading.local() blindspot issues.
"""
import threading
from dataclasses import dataclass, field

from modules.common.exceptions import SessionFlaggedError

_registry_lock = threading.Lock()
_watchdog_registry: dict[str, "_WatchdogSession"] = {}


@dataclass
class _WatchdogSession:
    event: threading.Event = field(default_factory=threading.Event)
    total_value: object = None
    enabled: bool = False


def enable_network_monitor(worker_id: str) -> None:
    """Create or reset a watchdog session for the given worker."""
    with _registry_lock:
        session = _WatchdogSession()
        session.enabled = True
        _watchdog_registry[worker_id] = session


def wait_for_total(worker_id: str, timeout) -> object:
    """Block until notify_total() is called for worker_id, or timeout expires.

    Raises:
        RuntimeError: if enable_network_monitor() was not called first.
        SessionFlaggedError: if timeout expires before notify_total() fires.
    """
    with _registry_lock:
        session = _watchdog_registry.get(worker_id)
        if session is None or not session.enabled:
            raise RuntimeError(f"Network monitor is not enabled for worker '{worker_id}'")

    try:
        received = session.event.wait(timeout=timeout)
        if not received:
            raise SessionFlaggedError(
                f"Timeout ({timeout}s) waiting for total amount for worker '{worker_id}'"
            )
        with _registry_lock:
            return _watchdog_registry[worker_id].total_value
    finally:
        _reset_session(worker_id)


def notify_total(worker_id: str, value) -> None:
    """Signal that the checkout total has been received for worker_id.

    Safe to call from ANY thread, including the browser CDP event thread.
    No-op if no session exists for worker_id (idempotent).
    """
    with _registry_lock:
        session = _watchdog_registry.get(worker_id)
    if session is not None:
        session.total_value = value
        session.event.set()


def _reset_session(worker_id: str) -> None:
    """Remove the session entry for worker_id."""
    with _registry_lock:
        _watchdog_registry.pop(worker_id, None)


def reset() -> None:
    """Reset all watchdog state. Intended for testing only."""
    with _registry_lock:
        _watchdog_registry.clear()
