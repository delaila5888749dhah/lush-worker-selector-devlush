"""Orchestration layer — coordinates FSM, Watchdog, Billing, and CDP modules.

All inter-module communication uses modules.common types only.
No cross-module imports exist within the individual modules themselves;
this file is the single integration point that wires them together.
"""

import concurrent.futures
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from modules.billing import main as billing
from modules.cdp import main as cdp
from modules.fsm import main as fsm
from modules.fsm.main import ALLOWED_STATES as _FSM_STATES  # noqa: F401 — Imported from fsm canonical source; intentionally unused but enforces INV-FSM-01 at import time
from modules.monitor import main as monitor
from modules.rollout import main as rollout
from modules.watchdog import main as watchdog

_WATCHDOG_TIMEOUT = 30

_logger = logging.getLogger(__name__)

# Redact card-like digit sequences (13–16 consecutive digits) from error messages
# to prevent PII leakage when CDP exceptions contain card numbers.
_SENSITIVE_PATTERN = re.compile(r'(?<!\w)(?:\d[ -]?){13,16}(?!\w)')


def _sanitize_error(exc: Exception) -> str:
    """Redact card-like digit sequences from exception messages before logging."""
    return _SENSITIVE_PATTERN.sub("[REDACTED]", str(exc))


def _get_trace_id() -> str:
    """Retrieve the current trace_id from the runtime, or 'no-trace' if unavailable.

    This provides log correlation between orchestrator events and the
    runtime's structured log events without a hard import-time dependency.
    """
    try:
        from integration.runtime import get_trace_id
        return get_trace_id() or "no-trace"
    except ImportError:
        # Acceptable: orchestrator running standalone without runtime module
        return "no-trace"
    except Exception:
        # Unexpected error — log at DEBUG to avoid spam, but don't silently swallow
        _logger.debug(
            "get_trace_id() raised unexpectedly; log correlation unavailable",
            exc_info=True,
        )
        return "no-trace"

# TTL-based idempotency cache with in-flight tracking.
_IDEMPOTENCY_TTL = 3600  # 1 hour
_completed_task_ids: dict[str, float] = {}  # task_id → monotonic timestamp
_in_flight_task_ids: set[str] = set()
_submitted_task_ids: dict[str, float] = {}  # task_id → monotonic timestamp; payment sent but result unconfirmed
_idempotency_lock = threading.Lock()

# Persistent idempotency store — survives process restarts to prevent double-charges.
# Configurable via IDEMPOTENCY_STORE_PATH env var.
_IDEMPOTENCY_STORE_PATH = Path(
    os.getenv("IDEMPOTENCY_STORE_PATH", ".idempotency_store.json")
)

# CDP call timeout — prevents worker threads from blocking indefinitely.
_CDP_CALL_TIMEOUT = float(os.getenv("CDP_CALL_TIMEOUT_SECONDS", "15"))


def _load_idempotency_store() -> None:
    """Load previously completed/submitted task IDs from the persistent file store.

    Converts stored wall-clock timestamps to equivalent monotonic values so that
    TTL eviction works correctly after a restart.
    """
    def _restore_entries(raw: dict, target: dict, now_wall: float, now_mono: float) -> None:
        """Convert wall-clock → monotonic timestamps, skipping expired/malformed entries."""
        for k, wall_ts in raw.items():
            try:
                age = max(0.0, now_wall - float(wall_ts))
                if age < _IDEMPOTENCY_TTL:
                    target[k] = now_mono - age
            except (ValueError, TypeError) as parse_err:
                _logger.warning(
                    "Skipping malformed idempotency store entry: key=%r value=%r error=%s",
                    k, wall_ts, parse_err,
                )

    try:
        if _IDEMPOTENCY_STORE_PATH.exists():
            data = json.loads(_IDEMPOTENCY_STORE_PATH.read_text(encoding="utf-8"))
            completed = data.get("completed", {})
            submitted = data.get("submitted", [])
            now_wall = time.time()
            now_mono = time.monotonic()
            if isinstance(completed, dict):
                _restore_entries(completed, _completed_task_ids, now_wall, now_mono)
            if isinstance(submitted, list):
                # Legacy format: list of task_ids without timestamps.
                # Treat them as recently submitted (now) for TTL purposes.
                for s in submitted:
                    _submitted_task_ids[str(s)] = now_mono
            elif isinstance(submitted, dict):
                _restore_entries(submitted, _submitted_task_ids, now_wall, now_mono)
    except Exception:
        _logger.warning(
            "Failed to load idempotency store from %s; starting fresh.",
            _IDEMPOTENCY_STORE_PATH,
            exc_info=True,
        )


def _save_idempotency_store() -> None:
    """Atomically persist the idempotency store to disk.

    Uses a temp-file + rename pattern to ensure atomic writes.

    .. warning::
        Must be called while the caller already holds ``_idempotency_lock``.
    """
    try:
        now_wall = time.time()
        now_mono = time.monotonic()
        cutoff_mono = now_mono - _IDEMPOTENCY_TTL
        # Convert monotonic → wall-clock timestamps for cross-restart portability,
        # and filter out already-expired entries to keep the file compact.
        completed_wall = {
            k: now_wall - (now_mono - ts)
            for k, ts in _completed_task_ids.items()
            if ts >= cutoff_mono
        }
        data = {
            "completed": completed_wall,
            "submitted": {
                k: now_wall - (now_mono - ts)
                for k, ts in _submitted_task_ids.items()
                if ts >= cutoff_mono
            },
        }
        parent = _IDEMPOTENCY_STORE_PATH.parent
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=parent, suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp)
            tmp_path = Path(tmp.name)
        tmp_path.replace(_IDEMPOTENCY_STORE_PATH)
    except Exception:
        _logger.warning(
            "Failed to persist idempotency store to %s",
            _IDEMPOTENCY_STORE_PATH,
            exc_info=True,
        )


# ── Idempotency store abstraction (CRIT-01) ────────────────────────────────


class _IdempotencyStore:
    """Abstract base for idempotency backends."""

    def is_duplicate(self, task_id: str) -> bool:
        """Return True if task_id is already known (completed/in-flight/submitted).

        For FileIdempotencyStore, also marks the task as in-flight on first
        encounter.  For RedisIdempotencyStore, atomically sets the key.
        """
        raise NotImplementedError

    def mark_submitted(self, task_id: str) -> None:
        """Record that payment was submitted for task_id (but not yet confirmed)."""
        raise NotImplementedError

    def mark_completed(self, task_id: str) -> None:
        """Record that task_id completed successfully."""
        raise NotImplementedError

    def release_inflight(self, task_id: str) -> None:
        """Remove task_id from the in-flight set (called in finally block)."""
        raise NotImplementedError

    def flush(self) -> None:
        """Force-persist state.  No-op for backends that are always persistent."""
        raise NotImplementedError

    def load(self) -> None:
        """Load persisted state on startup.  No-op for always-persistent backends."""
        raise NotImplementedError


class _FileIdempotencyStore(_IdempotencyStore):
    """File-backed idempotency store using module-level dicts under ``_idempotency_lock``."""

    def is_duplicate(self, task_id: str) -> bool:
        with _idempotency_lock:
            _evict_expired_task_ids()
            if (
                task_id in _completed_task_ids
                or task_id in _in_flight_task_ids
                or task_id in _submitted_task_ids
            ):
                return True
            # Mark as in-flight immediately to block concurrent duplicates.
            _in_flight_task_ids.add(task_id)
            return False

    def mark_submitted(self, task_id: str) -> None:
        with _idempotency_lock:
            _submitted_task_ids[task_id] = time.monotonic()
            _save_idempotency_store()

    def mark_completed(self, task_id: str) -> None:
        with _idempotency_lock:
            _completed_task_ids[task_id] = time.monotonic()
            _submitted_task_ids.pop(task_id, None)
            _save_idempotency_store()

    def release_inflight(self, task_id: str) -> None:
        with _idempotency_lock:
            _in_flight_task_ids.discard(task_id)

    def flush(self) -> None:
        with _idempotency_lock:
            _save_idempotency_store()

    def load(self) -> None:
        _load_idempotency_store()


class _RedisIdempotencyStore(_IdempotencyStore):
    """Redis-backed idempotency store.  Uses SET NX EX for atomic cross-process safety."""

    def __init__(self, redis_url: str) -> None:
        import redis as _redis_lib
        self._redis = _redis_lib.Redis.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        try:
            self._redis.ping()
        except Exception as exc:
            raise ConnectionError(
                f"Redis ping failed for url={redis_url}: {exc}"
            ) from exc

    def _key(self, task_id: str) -> str:
        return f"idempotency:lush-givex:{task_id}"

    def is_duplicate(self, task_id: str) -> bool:
        # SET NX returns True when the key was set (first time → not a duplicate).
        # Returns None/False when the key already exists → duplicate.
        result = self._redis.set(self._key(task_id), "inflight", nx=True, ex=_IDEMPOTENCY_TTL)
        return result is None or result is False

    def mark_submitted(self, task_id: str) -> None:
        self._redis.set(self._key(task_id), "submitted", ex=_IDEMPOTENCY_TTL)

    def mark_completed(self, task_id: str) -> None:
        self._redis.set(self._key(task_id), "completed", ex=_IDEMPOTENCY_TTL)

    def release_inflight(self, task_id: str) -> None:
        pass  # Key persists with TTL; no explicit cleanup needed.

    def flush(self) -> None:
        pass  # Redis is always persistent; no explicit flush required.

    def load(self) -> None:
        pass  # Redis does not require a load step.


def _build_idempotency_store() -> _IdempotencyStore:
    """Select the appropriate idempotency backend based on environment."""
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            store = _RedisIdempotencyStore(redis_url)
            _logger.info("Using Redis-based idempotency store (url=%s).", redis_url)
            return store
        except Exception:
            _logger.warning(
                "Failed to initialise RedisIdempotencyStore (url=%s); "
                "falling back to file-based store.",
                redis_url,
                exc_info=True,
            )
    _logger.warning(
        "[WARN] Using file-based idempotency store. "
        "Set REDIS_URL for production multi-process deployments."
    )
    return _FileIdempotencyStore()


_idempotency_store: _IdempotencyStore | None = None
_idempotency_store_lock = threading.Lock()


def _get_idempotency_store() -> _IdempotencyStore:
    """Return the idempotency store, building and loading it on first call.

    Thread-safe. Combines lazy construction (_build_idempotency_store) and
    lazy load (_load) into a single atomic init so there is no separate
    _store_loaded sentinel variable.
    """
    global _idempotency_store
    with _idempotency_store_lock:
        if _idempotency_store is None:
            store = _build_idempotency_store()
            store.load()
            _idempotency_store = store
        return _idempotency_store


def _flush_idempotency_store() -> None:
    """Force-persist idempotency store. Called by runtime on graceful shutdown."""
    _get_idempotency_store().flush()


def _evict_expired_task_ids() -> None:
    """Remove task_ids that have exceeded the TTL. Must be called while holding _idempotency_lock."""
    cutoff = time.monotonic() - _IDEMPOTENCY_TTL
    expired = [k for k, ts in _completed_task_ids.items() if ts < cutoff]
    for k in expired:
        del _completed_task_ids[k]
    expired_sub = [k for k, ts in _submitted_task_ids.items() if ts < cutoff]
    for k in expired_sub:
        del _submitted_task_ids[k]


# ── CDP timeout helper (HIGH-02) ──────────────────────────────────


def _cdp_call_with_timeout(fn: Callable, *args: Any, timeout: float = _CDP_CALL_TIMEOUT, **kwargs: Any) -> Any:
    """Execute a CDP call with a hard timeout.

    Wraps *fn* in a single-worker ``ThreadPoolExecutor`` and waits at most
    *timeout* seconds.  Raises ``SessionFlaggedError`` if the call does not
    complete in time so the runtime treats the session as flagged.

    Args:
        fn: CDP callable to invoke.
        *args: Positional arguments forwarded to *fn*.
        timeout: Maximum seconds to wait (default: ``_CDP_CALL_TIMEOUT``).
        **kwargs: Keyword arguments forwarded to *fn*.

    Raises:
        SessionFlaggedError: If the call does not complete within *timeout* seconds.
    """
    from modules.common.exceptions import SessionFlaggedError
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise SessionFlaggedError(
                f"CDP call '{getattr(fn, '__name__', repr(fn))}' "
                f"timed out after {timeout}s for worker"
            )


def initialize_cycle(worker_id: str = "default"):
    """Reset FSM registry and register all valid states for a new cycle."""
    _get_idempotency_store()
    rollout.configure(monitor.check_rollback_needed, monitor.save_baseline)
    fsm.initialize_for_worker(worker_id)


def run_payment_step(task, zip_code=None, worker_id: str = "default"):
    """Execute one payment attempt.

    Steps:
      1. Select a billing profile from the pool.
      2. Enable the network watchdog for this worker.
      3. Fill billing and card data via CDP (with timeout).
      4. Wait for the checkout total to be confirmed by the watchdog.
      5. Return (state, total).

    Args:
        task: WorkerTask containing card and order information.
        zip_code: Optional zip code for billing profile matching.
        worker_id: Unique identifier for this worker (used to key the watchdog session).

    Returns:
        A (state, total) tuple where state is a State object or None,
        and total is the confirmed checkout amount.

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out waiting for the total.
        RuntimeError: if no CDP driver has been registered.
    """
    profile = billing.select_profile(zip_code)
    watchdog.enable_network_monitor(worker_id)
    try:
        _cdp_call_with_timeout(cdp.fill_billing, profile, worker_id=worker_id)
        _cdp_call_with_timeout(cdp.fill_card, task.primary_card, worker_id=worker_id)
        # Payment-submitted checkpoint: persist task_id before waiting for total.
        # If the process crashes here, the submitted state records that payment was sent.
        _task_id = getattr(task, "task_id", None)
        if _task_id is not None:
            _get_idempotency_store().mark_submitted(_task_id)
        total = watchdog.wait_for_total(worker_id, timeout=_WATCHDOG_TIMEOUT)
    except Exception as exc:
        _logger.error(
            "[trace=%s] Payment step failed for worker=%s, task_id=%s: %s",
            _get_trace_id(),
            worker_id,
            getattr(task, "task_id", None),
            _sanitize_error(exc),
        )
        # Clean up the orphaned watchdog session to prevent memory leaks.
        watchdog.reset_session(worker_id)
        raise
    state = fsm.get_current_state_for_worker(worker_id)
    return state, total


def handle_outcome(state, order_queue, worker_id: str = "default"):
    """Determine the next action based on the current FSM state.

    Args:
        state: Current State object (or None if FSM was never transitioned).
        order_queue: Remaining cards available for swap.
        worker_id: Unique identifier for this worker (used for log context).

    Returns:
        One of: "complete", "retry", "retry_new_card", "await_3ds".
    """
    if state is None:
        return "retry"
    if state.name == "success":
        return "complete"
    if state.name == "declined":
        return "retry_new_card" if order_queue else "retry"
    if state.name == "ui_lock":
        return "retry"
    if state.name == "vbv_3ds":
        try:
            cdp.clear_card_fields(worker_id=worker_id)
        except Exception as exc:
            _logger.warning(
                "[trace=%s] cdp.clear_card_fields() failed for worker=%s during vbv_3ds "
                "handling; proceeding to await_3ds: %s",
                _get_trace_id(),
                worker_id,
                _sanitize_error(exc),
            )
        return "await_3ds"
    return "retry"


def run_cycle(task, zip_code=None, worker_id: str = "default"):
    """Run a full payment cycle for a WorkerTask.

    Initializes the FSM, executes one payment attempt, and returns the
    outcome action together with the final state and confirmed total.

    Args:
        task: WorkerTask containing the recipient, amount, and card data.
        zip_code: Optional zip code for billing profile selection.
        worker_id: Unique identifier for this worker.

    Returns:
        A (action, state, total) tuple where action is one of:
        "complete" | "retry" | "retry_new_card" | "await_3ds".

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out.
        RuntimeError: if no CDP driver has been registered.
    """
    task_id = getattr(task, "task_id", None)
    if task_id is not None:
        if _get_idempotency_store().is_duplicate(task_id):
            _logger.warning(
                "[trace=%s] Duplicate task_id=%s detected; skipping.",
                _get_trace_id(),
                task_id,
            )
            return "complete", None, None
    try:
        initialize_cycle(worker_id)
        state, total = run_payment_step(task, zip_code, worker_id=worker_id)
        action = handle_outcome(state, task.order_queue, worker_id=worker_id)
        if task_id is not None:
            _get_idempotency_store().mark_completed(task_id)
        return action, state, total
    except Exception as exc:
        _logger.error(
            "[trace=%s] Cycle failed for worker=%s, task_id=%s: %s",
            _get_trace_id(),
            worker_id,
            task_id,
            _sanitize_error(exc),
        )
        raise
    finally:
        if task_id is not None:
            _get_idempotency_store().release_inflight(task_id)
        # Clean up CDP driver to prevent registry memory leak (GAP-CDP-01).
        cdp.unregister_driver(worker_id)
        # Clean up FSM state to prevent registry memory leak (HIGH-02 / FSM-002).
        fsm.cleanup_worker(worker_id)
