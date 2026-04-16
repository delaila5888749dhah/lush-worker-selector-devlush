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
    # _closed is set to True when a session is completed, timed out, replaced, or
    # explicitly removed.  notify_total() checks this flag so that any late/stale
    # callback targeting this session becomes a safe no-op instead of mutating
    # total_value or setting the event on an already-finished session.
    _closed: bool = False


def enable_network_monitor(worker_id: str) -> None:
    """Create or reset a watchdog session for the given worker.

    If a session already exists for worker_id, it is marked closed before being
    replaced.  This ensures any in-flight notify_total() callback that still
    holds the old session reference (via the registry look-up race window) will
    see _closed=True and become a no-op rather than contaminating the new session.
    """
    with _registry_lock:
        old = _watchdog_registry.get(worker_id)
        if old is not None:
            old._closed = True
        session = _WatchdogSession()
        session.enabled = True
        _watchdog_registry[worker_id] = session


def wait_for_total(worker_id: str, timeout: float | None) -> object:
    """Block until notify_total() is called for worker_id, or timeout expires.

    Event.wait(timeout) is intentionally called **outside** _registry_lock to
    prevent deadlock: notify_total() also acquires _registry_lock to write
    total_value before signalling the event, so holding the lock here would
    cause a deadlock.

    Args:
        worker_id: The worker identifier whose session to wait on.
        timeout: Maximum seconds to wait.  Must be ``None`` (wait indefinitely)
            or a strictly positive number.  Passing ``0`` or a negative value
            raises ``ValueError`` because such timeouts produce an immediate
            False return from Event.wait() without any actual wait, violating
            the blocking contract.

    Returns:
        The total value delivered by notify_total().

    Raises:
        ValueError: if timeout is 0 or negative.
        RuntimeError: if enable_network_monitor() was not called first.
        SessionFlaggedError: if timeout expires before notify_total() fires.
    """
    if timeout is not None and timeout <= 0:
        raise ValueError(
            f"timeout must be None or a positive number, got {timeout!r}"
        )

    with _registry_lock:
        session = _watchdog_registry.get(worker_id)
        if session is None or not session.enabled:
            raise RuntimeError(f"Network monitor is not enabled for worker '{worker_id}'")

    # --- Event.wait() is called OUTSIDE _registry_lock (see docstring). ---
    try:
        received = session.event.wait(timeout=timeout)
        if not received:
            raise SessionFlaggedError(
                f"Timeout ({timeout}s) waiting for total amount for worker '{worker_id}'"
            )
        return session.total_value
    finally:
        # Re-acquire the lock to atomically mark the session closed and remove
        # it from the registry.  Identity check (``is``) ensures we only remove
        # the exact session we were waiting on; a concurrent
        # enable_network_monitor() may have already replaced the registry entry
        # with a new session and closed this session itself.
        # session._closed = True is set unconditionally (outside the ``if``
        # block) so that any code holding a direct reference to this session
        # object — rather than looking it up through the registry — cannot
        # signal it via notify_total() after wait_for_total() has exited.
        with _registry_lock:
            if _watchdog_registry.get(worker_id) is session:
                _watchdog_registry.pop(worker_id, None)
            session._closed = True


def notify_total(worker_id: str, value) -> None:
    """Signal that the checkout total has been received for worker_id.

    Safe to call from ANY thread, including the browser CDP event thread.
    No-op if no session exists for worker_id (idempotent).
    No-op if the session has already been completed, timed out, or replaced
    (_closed=True), preventing stale callbacks from contaminating a later
    session created for the same worker after a crash/reset/re-enable.
    """
    with _registry_lock:
        session = _watchdog_registry.get(worker_id)
        if session is not None and not session._closed:
            session.total_value = value
            session.event.set()


def _reset_session(worker_id: str) -> None:
    """Remove the session entry for worker_id and close it."""
    with _registry_lock:
        session = _watchdog_registry.pop(worker_id, None)
        if session is not None:
            session._closed = True


def reset_session(worker_id: str) -> None:
    """Reset the watchdog session for worker_id (public API for orchestrator)."""
    _reset_session(worker_id)


def reset() -> None:
    """Reset all watchdog state. Intended for testing only."""
    with _registry_lock:
        for session in _watchdog_registry.values():
            session._closed = True
        _watchdog_registry.clear()
