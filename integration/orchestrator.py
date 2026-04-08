"""Orchestration layer — coordinates FSM, Watchdog, Billing, and CDP modules.

All inter-module communication uses modules.common types only.
No cross-module imports exist within the individual modules themselves;
this file is the single integration point that wires them together.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

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
    except Exception:
        return "no-trace"

# TTL-based idempotency cache with in-flight tracking.
# NOTE: For production at scale (>10 workers), migrate to Redis SET NX with TTL.
_IDEMPOTENCY_TTL = 3600  # 1 hour
_completed_task_ids: dict[str, float] = {}  # task_id → monotonic timestamp
_in_flight_task_ids: set[str] = set()
_submitted_task_ids: dict[str, float] = {}  # task_id → monotonic timestamp; payment sent but result unconfirmed
_idempotency_lock = threading.Lock()

# Persistent idempotency store — survives process restarts to prevent double-charges.
# Configurable via IDEMPOTENCY_STORE_PATH env var.
# NOTE: For production at scale (>10 workers), migrate to Redis SET NX with TTL.
_IDEMPOTENCY_STORE_PATH = Path(
    os.getenv("IDEMPOTENCY_STORE_PATH", ".idempotency_store.json")
)

_init_warning_emitted = False


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
            except (ValueError, TypeError):
                pass  # Malformed timestamp — skip this entry, don't block other valid ones

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


# Load idempotency state from persistent store on startup.
_load_idempotency_store()


def _evict_expired_task_ids() -> None:
    """Remove task_ids that have exceeded the TTL. Must be called while holding _idempotency_lock."""
    cutoff = time.monotonic() - _IDEMPOTENCY_TTL
    expired = [k for k, ts in _completed_task_ids.items() if ts < cutoff]
    for k in expired:
        del _completed_task_ids[k]
    expired_sub = [k for k, ts in _submitted_task_ids.items() if ts < cutoff]
    for k in expired_sub:
        del _submitted_task_ids[k]


def initialize_cycle(worker_id: str = "default"):
    """Reset FSM registry and register all valid states for a new cycle."""
    global _init_warning_emitted
    if not _init_warning_emitted:
        _logger.warning(
            "Idempotency store is file-based (%s). "
            "For production at scale (>10 workers), migrate to Redis SET NX with TTL.",
            _IDEMPOTENCY_STORE_PATH,
        )
        _init_warning_emitted = True
    rollout.configure(monitor.check_rollback_needed, monitor.save_baseline)
    fsm.initialize_for_worker(worker_id)


def run_payment_step(task, zip_code=None, worker_id: str = "default"):
    """Execute one payment attempt.

    Steps:
      1. Select a billing profile from the pool.
      2. Enable the network watchdog for this worker.
      3. Fill billing and card data via CDP.
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
        cdp.fill_billing(profile, worker_id=worker_id)
        cdp.fill_card(task.primary_card, worker_id=worker_id)
        # Payment-submitted checkpoint: persist task_id before waiting for total.
        # If the process crashes here, _submitted_task_ids records that payment was sent.
        _task_id = getattr(task, "task_id", None)
        if _task_id is not None:
            with _idempotency_lock:
                _submitted_task_ids[_task_id] = time.monotonic()
                _save_idempotency_store()
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
        with _idempotency_lock:
            _evict_expired_task_ids()
            if task_id in _completed_task_ids or task_id in _in_flight_task_ids or task_id in _submitted_task_ids:
                _logger.warning("[trace=%s] Duplicate task_id=%s detected; skipping.", _get_trace_id(), task_id)
                return "complete", None, None
            # Mark as in-flight immediately to block concurrent duplicates
            _in_flight_task_ids.add(task_id)
    try:
        initialize_cycle(worker_id)
        state, total = run_payment_step(task, zip_code, worker_id=worker_id)
        action = handle_outcome(state, task.order_queue, worker_id=worker_id)
        if task_id is not None:
            with _idempotency_lock:
                _completed_task_ids[task_id] = time.monotonic()
                _submitted_task_ids.pop(task_id, None)
                _save_idempotency_store()
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
            with _idempotency_lock:
                _in_flight_task_ids.discard(task_id)
        # Clean up CDP driver to prevent registry memory leak (GAP-CDP-01).
        cdp.unregister_driver(worker_id)
