"""Orchestration layer — coordinates FSM, Watchdog, Billing, and CDP modules.

All inter-module communication uses modules.common types only.
No cross-module imports exist within the individual modules themselves;
this file is the single integration point that wires them together.
"""

import atexit
import concurrent.futures
import dataclasses
import datetime
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from modules.common.exceptions import CDPError, InvalidTransitionError, SessionFlaggedError
from modules.common.types import State
from modules.common.sanitize import sanitize_error as _canonical_sanitize_error
from modules.common.sanitize import sanitize_redis_url as _sanitize_redis_url
# Optional autoscaler integration — module is available once PR-P (SCALE-001) is merged.
# Import fails gracefully so orchestrator works before that PR lands.
try:
    from modules.rollout.autoscaler import get_autoscaler as _get_autoscaler
except ImportError:
    _get_autoscaler = None  # type: ignore[assignment]
from modules.billing import main as billing
from modules.cdp import main as cdp
from modules.delay.config import CDP_CALL_TIMEOUT as _CDP_CALL_TIMEOUT_CONFIG
from modules.delay.state import get_current_sm as _get_current_sm
from modules.fsm import main as fsm
from modules.fsm.main import ALLOWED_STATES as _FSM_STATES  # Canonical FSM state set; enforces INV-FSM-01 at import time
from modules.monitor import main as monitor
from modules.observability import alerting as _alerting
from modules.rollout import main as rollout
from modules.watchdog import main as watchdog

# INVARIANT: _WATCHDOG_TIMEOUT MUST satisfy:
#   _WATCHDOG_TIMEOUT > _CDP_CALL_TIMEOUT + _STEP_BUDGET_TOTAL
#   i.e. 30 > 15.0 + 10.0 = 25.0  ✓
# Rationale: a legitimate cycle can take up to _CDP_CALL_TIMEOUT seconds
# waiting for a CDP call PLUS _STEP_BUDGET_TOTAL seconds of behavioral
# delay. Setting _WATCHDOG_TIMEOUT below this sum causes the watchdog
# to fire before the cycle can legitimately complete (false timeout).
# If you change any of these three values, re-verify this invariant.
_WATCHDOG_TIMEOUT = 30
# Default caller-supplied timeout documented in spec/watchdog.md.
_WATCHDOG_TIMEOUT_DEFAULT = _WATCHDOG_TIMEOUT

# C5 — Blueprint §5 payment-step specific watchdog timeout.
# Blueprint §5 mandates a 10s total-response watchdog on the payment step.
# spec/watchdog.md keeps the module contract as 30s (caller-controlled);
# this constant narrows the timeout only for `run_payment_step`.
# Operators can override via the PAYMENT_WATCHDOG_TIMEOUT_S env var (integer
# or float seconds, must be > 0).  On invalid override the default 10s is
# used and a warning is logged.
def _load_payment_watchdog_timeout() -> float:
    raw = os.environ.get("PAYMENT_WATCHDOG_TIMEOUT_S", "").strip()
    if not raw:
        return 10.0
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "PAYMENT_WATCHDOG_TIMEOUT_S=%r is not numeric; defaulting to 10s",
            raw,
        )
        return 10.0
    if value <= 0:
        _logger.warning(
            "PAYMENT_WATCHDOG_TIMEOUT_S=%r must be > 0; defaulting to 10s",
            raw,
        )
        return 10.0
    return value


_WATCHDOG_TIMEOUT_PAYMENT = _load_payment_watchdog_timeout()

# P0-2 — Retry loop feature flag.
# Set ENABLE_RETRY_LOOP=0 to fall back to the original single-shot behaviour.
# Default is enabled (any value other than "0", "false", or "no").
_ENABLE_RETRY_LOOP: bool = os.getenv("ENABLE_RETRY_LOOP", "1") not in ("0", "false", "no")

# P0-4 — UI lock focus-shift retry feature flag.
# Set ENABLE_RETRY_UI_LOCK=0 to disable automatic UI lock recovery.
# Default is enabled (any value other than "0", "false", or "no").
_ENABLE_RETRY_UI_LOCK: bool = os.getenv("ENABLE_RETRY_UI_LOCK", "1") not in ("0", "false", "no")
_MAX_UI_LOCK_RETRIES: int = 2
_UI_BUSY_STATE = "ui_busy"
_UI_BUSY_RECHECK_INTERVAL = 0.3
_UI_BUSY_RECHECK_TIMEOUT = 10.0
_UI_BUSY_RECHECK_ATTEMPTS = math.ceil(
    _UI_BUSY_RECHECK_TIMEOUT / _UI_BUSY_RECHECK_INTERVAL
)

# P1-2 — Clear/refill after "Thank you" popup feature flag.
# Set ENABLE_CLEAR_REFILL_AFTER_POPUP=0 to disable clear/refill after confirmation.
# Default is enabled (any value other than "0", "false", or "no").
_ENABLE_CLEAR_REFILL_AFTER_POPUP: bool = (
    os.getenv("ENABLE_CLEAR_REFILL_AFTER_POPUP", "1") not in ("0", "false", "no")
)

_logger = logging.getLogger(__name__)
_AUDIT_LOGGER = logging.getLogger(f"{__name__}.audit")


def _sanitize_error(exc: Exception) -> str:
    """Redact PII from an exception message before logging.

    Delegates to the canonical sanitiser in ``modules.common.sanitize``.
    Accepts an Exception for backward-compatibility with existing call-sites.
    """
    return _canonical_sanitize_error(str(exc))


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


def _unwrap_raw_driver(driver_obj):
    """Return the underlying Selenium driver if *driver_obj* is a GivexDriver wrapper.

    ``GivexDriver`` holds the raw Selenium driver as ``self._driver``.  When
    the orchestrator receives a ``GivexDriver``, calling CDP / Selenium-native
    methods directly on the wrapper raises ``AttributeError`` because those
    methods are not forwarded.  This helper unwraps safely.

    Contract: only unwrap when the inner ``_driver`` attribute is not ``None``,
    is a different object from *driver_obj* itself, exposes ``execute_cdp_cmd``
    (looks like a real Selenium driver), and *driver_obj* itself does **not**
    already expose ``execute_cdp_cmd`` (i.e. it is a real wrapper, not a
    ``MagicMock`` or already-raw driver).  This keeps ``MagicMock``-based unit
    tests working unchanged.
    """
    # Only unwrap when the wrapper lacks execute_cdp_cmd (real GivexDriver
    # pattern) but the inner _driver has it (real Selenium driver).
    inner = getattr(driver_obj, "_driver", None)
    if (
        inner is not None
        and inner is not driver_obj
        and hasattr(inner, "execute_cdp_cmd")
        and not hasattr(driver_obj, "execute_cdp_cmd")
    ):
        return inner
    return driver_obj


def _get_consecutive_failures(worker_id: str) -> int:
    """Return autoscaler consecutive failure count, or -1 if unavailable."""
    try:
        if _get_autoscaler is not None:
            return _get_autoscaler().get_consecutive_failures(worker_id)
        return -1
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        return -1


def _record_autoscaler_success(worker_id: str) -> None:
    """Record a successful payment cycle in the autoscaler. No-op if unavailable."""
    try:
        if _get_autoscaler is not None:
            _get_autoscaler().record_success(worker_id)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.debug("autoscaler.record_success skipped", exc_info=True)


def _record_autoscaler_failure(worker_id: str) -> None:
    """Record a failed payment cycle in the autoscaler. No-op if unavailable."""
    try:
        if _get_autoscaler is not None:
            _get_autoscaler().record_failure(worker_id)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.debug("autoscaler.record_failure skipped", exc_info=True)


def _notify_success(task, worker_id: str, total, ctx=None) -> None:
    """Send success screenshot+notification (Blueprint §6 Ngã rẽ 2). Never raises.

    The registry stores ``GivexDriver`` wrappers (see ``integration.worker_task``);
    screenshot capture must use the underlying Selenium WebDriver (``._driver``).
    Pass *ctx* for full caption including billing and duration.
    """
    # pylint: disable=import-outside-toplevel
    try:
        from modules.notification.screenshot_blur import capture_and_blur  # noqa: PLC0415
        from modules.notification.telegram_notifier import send_success_notification  # noqa: PLC0415
        try:
            wrapper = cdp._get_driver(worker_id)  # pylint: disable=protected-access
        except RuntimeError:
            wrapper = None
        # Unwrap GivexDriver → raw Selenium WebDriver so get_screenshot_as_png()
        # is invoked on the actual browser session, not the wrapper layer.
        raw_driver = getattr(wrapper, "_driver", wrapper)
        screenshot = (
            capture_and_blur(raw_driver, task.primary_card.card_number)
            if raw_driver else None
        )
        send_success_notification(worker_id, task, total, screenshot, ctx=ctx)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning("[trace=%s] success notify failed worker=%s: %s",
                        _get_trace_id(), worker_id, exc)

# TTL-based idempotency cache with in-flight tracking.
_IDEMPOTENCY_TTL = 3600  # 1 hour
_IN_FLIGHT_TTL_SECONDS: int = 300  # 5 min — stale in-flight eviction
# TTL for submitted-but-unconfirmed tasks (watchdog timed out AFTER submit); 24h default.
_UNCONFIRMED_TTL_SECONDS: int = int(os.getenv("IDEMPOTENCY_UNCONFIRMED_TTL_SECONDS", "86400"))
_completed_task_ids: dict[str, float] = {}  # task_id → monotonic timestamp
_submitted_task_ids: dict[str, float] = {}  # task_id → monotonic timestamp; payment sent but result unconfirmed
# task_id → (monotonic insertion ts, ttl_seconds); per-entry TTL for manual reconciliation.
_unconfirmed_task_ids: dict[str, tuple[float, float]] = {}
_idempotency_lock = threading.Lock()


class _InFlightTaskIds(dict):  # dict[str, float]
    """In-flight task ID tracker: maps task_id → monotonic insertion timestamp.

    Subclasses dict so that TTL eviction can inspect insertion times.
    Provides .add() and .discard() compatibility shims so existing callers
    (including test helpers that pre-populate the set) continue to work unchanged.
    """

    def add(self, task_id: str) -> None:
        """Compatibility shim: record task_id as in-flight with current timestamp."""
        self[task_id] = time.monotonic()

    def discard(self, task_id: str) -> None:
        """Compatibility shim: remove task_id if present."""
        self.pop(task_id, None)


_in_flight_task_ids: _InFlightTaskIds = _InFlightTaskIds()

# Persistent idempotency store — survives process restarts to prevent double-charges.
# Configurable via IDEMPOTENCY_STORE_PATH env var.
_IDEMPOTENCY_STORE_PATH = Path(
    os.getenv("IDEMPOTENCY_STORE_PATH", ".idempotency_store.json")
)

# CDP call timeout — prevents worker threads from blocking indefinitely.
_CDP_CALL_TIMEOUT = float(os.getenv("CDP_CALL_TIMEOUT_SECONDS", str(_CDP_CALL_TIMEOUT_CONFIG)))
CDP_EXECUTOR_MAX_WORKERS: int = int(os.getenv("CDP_EXECUTOR_MAX_WORKERS", "8"))
_cdp_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=CDP_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="cdp-timeout",
)
_cdp_executor_lock = threading.Lock()

_cdp_timeout_count: int = 0          # total CDP calls that timed out (caller-side)
_active_cdp_requests: int = 0        # orchestration-level tracking only
# Timed-out threads that may still occupy executor slots.
# Protected by _cdp_executor_lock (not _cdp_metric_lock).
_cdp_orphaned_threads: int = 0
# protects _cdp_timeout_count and _active_cdp_requests
_cdp_metric_lock = threading.Lock()  # pylint: disable=invalid-name
# Guards watchdog.notify_total() calls that may be triggered concurrently from
# both the CDP callback path and the pre-wait DOM fallback path.
_network_listener_lock = threading.Lock()  # pylint: disable=invalid-name
# First-notify-wins guard: tracks workers that have already received a total this cycle.
# Cleared per cycle in run_payment_step before watchdog.enable_network_monitor().
# Protected by _network_listener_lock.
_notified_workers_this_cycle: set[str] = set()  # pylint: disable=unsubscriptable-object
# Phase A DOM-only fallback (ALLOW_DOM_ONLY_WATCHDOG=1): per-worker stop
# events used to terminate the polling thread.  Protected by
# _network_listener_lock.
_dom_polling_stop_events: dict[str, threading.Event] = {}
# CDP Network.responseReceived URL filter.
# A substring match against the request URL is sufficient to trigger the
# pricing-total notification path.  Each entry uniquely identifies a
# pricing endpoint (verified against the live Givex checkout page).
_CDP_NETWORK_URL_PATTERNS = ("/checkout/total", "/api/tax", "/api/checkout")

# NOTE on _active_cdp_requests:
# This counter reflects orchestration-level tracking only.
# It does NOT accurately reflect executor thread occupancy.
# After a caller times out and future.cancel() is called,
# the underlying thread may still be running while this
# counter has already been decremented.


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
            unconfirmed = data.get("unconfirmed", {})
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
            if isinstance(unconfirmed, dict):
                _loaded_unconfirmed_keys: list[str] = []
                for k, entry in unconfirmed.items():
                    try:
                        # Entry format: {"ts": wall_ts, "ttl": ttl_seconds}
                        wall_ts = float(entry.get("ts"))
                        ttl_seconds = float(entry.get("ttl", _UNCONFIRMED_TTL_SECONDS))
                        age = max(0.0, now_wall - wall_ts)
                        if age < ttl_seconds:
                            _unconfirmed_task_ids[k] = (now_mono - age, ttl_seconds)
                            _loaded_unconfirmed_keys.append(k)
                    except (ValueError, TypeError, AttributeError) as parse_err:
                        _logger.warning(
                            "Skipping malformed unconfirmed entry: key=%r value=%r error=%s",
                            k, entry, parse_err,
                        )
                if _loaded_unconfirmed_keys:
                    _logger.warning(
                        "Crash-recovery: %d unconfirmed task(s) reloaded — manual review required.",
                        len(_loaded_unconfirmed_keys),
                    )
            if _submitted_task_ids:
                _logger.warning(
                    "Crash-recovery: %d submitted task(s) reloaded from idempotency store. "
                    "These tasks had payment submitted but not confirmed before the last "
                    "process exit. They will be treated as duplicates to prevent double-charge.",
                    len(_submitted_task_ids),
                )
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
            "unconfirmed": {
                k: {
                    "ts": now_wall - (now_mono - ts),
                    "ttl": ttl,
                }
                for k, (ts, ttl) in _unconfirmed_task_ids.items()
                if (now_mono - ts) < ttl
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

    def mark_unconfirmed(self, task_id: str, ttl_seconds: float | None = None) -> None:
        """Track *task_id* as submitted-but-unconfirmed for ``ttl_seconds`` (see ``reconcile_unconfirmed``)."""
        raise NotImplementedError

    def list_unconfirmed(self) -> list[str]:
        """Return task_ids currently in the unconfirmed state (for manual reconciliation)."""
        raise NotImplementedError

    def clear_unconfirmed(self, task_id: str) -> None:
        """Remove *task_id* from the unconfirmed set (after manual verification)."""
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
                or task_id in _unconfirmed_task_ids
            ):
                return True
            # Mark as in-flight immediately to block concurrent duplicates.
            _in_flight_task_ids[task_id] = time.monotonic()
            return False

    def mark_submitted(self, task_id: str) -> None:
        with _idempotency_lock:
            _submitted_task_ids[task_id] = time.monotonic()
            _save_idempotency_store()

    def mark_unconfirmed(self, task_id: str, ttl_seconds: float | None = None) -> None:
        ttl = float(ttl_seconds) if ttl_seconds is not None else float(_UNCONFIRMED_TTL_SECONDS)
        with _idempotency_lock:
            _unconfirmed_task_ids[task_id] = (time.monotonic(), ttl)
            # Also remove from submitted set — unconfirmed is a strictly stronger state.
            _submitted_task_ids.pop(task_id, None)
            _save_idempotency_store()

    def list_unconfirmed(self) -> list[str]:
        with _idempotency_lock:
            now_mono = time.monotonic()
            return [
                k for k, (ts, ttl) in _unconfirmed_task_ids.items()
                if (now_mono - ts) < ttl
            ]

    def clear_unconfirmed(self, task_id: str) -> None:
        with _idempotency_lock:
            if _unconfirmed_task_ids.pop(task_id, None) is not None:
                _save_idempotency_store()

    def mark_completed(self, task_id: str) -> None:
        with _idempotency_lock:
            _completed_task_ids[task_id] = time.monotonic()
            _submitted_task_ids.pop(task_id, None)
            _unconfirmed_task_ids.pop(task_id, None)
            _save_idempotency_store()

    def release_inflight(self, task_id: str) -> None:
        with _idempotency_lock:
            _in_flight_task_ids.pop(task_id, None)

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
                f"Redis ping failed for url={_sanitize_redis_url(redis_url)}: {exc}"
            ) from exc

    def _key(self, task_id: str) -> str:
        return f"idempotency:lush-givex:{task_id}"

    def is_duplicate(self, task_id: str) -> bool:
        with _idempotency_lock:
            _evict_expired_task_ids()
        # SET NX returns True when the key was set (first time → not a duplicate).
        # Returns None/False when the key already exists → duplicate.
        try:
            result = self._redis.set(self._key(task_id), "inflight", nx=True, ex=_IDEMPOTENCY_TTL)
            return result is None or result is False
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.error(
                "RedisIdempotencyStore.is_duplicate failed for task_id=%s: %s; "
                "treating as duplicate (fail-safe) to prevent double-charge.", task_id, exc,
            )
            return True  # fail-safe: treat as duplicate

    def mark_submitted(self, task_id: str) -> None:
        try:
            self._redis.set(self._key(task_id), "submitted", ex=_IDEMPOTENCY_TTL)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.error(
                "RedisIdempotencyStore.mark_submitted failed for task_id=%s: %s", task_id, exc,
            )
            raise

    def mark_unconfirmed(self, task_id: str, ttl_seconds: float | None = None) -> None:
        ttl = int(ttl_seconds) if ttl_seconds is not None else int(_UNCONFIRMED_TTL_SECONDS)
        try:
            # Value carries wall-clock submit ts so operators can see when it was flagged.
            self._redis.set(self._key(task_id), f"unconfirmed:{int(time.time())}", ex=ttl)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.error(
                "RedisIdempotencyStore.mark_unconfirmed failed for task_id=%s: %s", task_id, exc,
            )
            raise

    def list_unconfirmed(self) -> list[str]:
        """Scan Redis for keys whose value is prefixed ``unconfirmed:``."""
        prefix = self._key("")
        try:
            result: list[str] = []
            for key in self._redis.scan_iter(match=f"{prefix}*"):
                try:
                    val = self._redis.get(key)
                except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.warning(
                        "RedisIdempotencyStore.list_unconfirmed: GET failed for key=%r (skipped): %s",
                        key, exc,
                    )
                    val = None
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                if isinstance(val, str) and val.startswith("unconfirmed"):
                    key_str = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
                    result.append(key_str[len(prefix):])
            return result
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.warning(
                "RedisIdempotencyStore.list_unconfirmed failed: %s", exc,
            )
            return []

    def clear_unconfirmed(self, task_id: str) -> None:
        try:
            self._redis.delete(self._key(task_id))
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.warning(
                "RedisIdempotencyStore.clear_unconfirmed failed for task_id=%s: %s", task_id, exc,
            )

    def mark_completed(self, task_id: str) -> None:
        try:
            self._redis.set(self._key(task_id), "completed", ex=_IDEMPOTENCY_TTL)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.warning(
                "RedisIdempotencyStore.mark_completed failed for task_id=%s: %s", task_id, exc,
            )
            # Do not re-raise: completion-recording failure is not critical; task already submitted.

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
        safe_redis_url = _sanitize_redis_url(redis_url)
        try:
            store = _RedisIdempotencyStore(redis_url)
            _logger.info("Using Redis-based idempotency store (url=%s).", safe_redis_url)
            return store
        except Exception:
            _logger.warning(
                "Failed to initialise RedisIdempotencyStore (url=%s); "
                "falling back to file-based store.",
                safe_redis_url,
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


def list_unconfirmed_task_ids() -> list[str]:
    """Return task IDs currently in the submitted-but-unconfirmed state (operator tooling)."""
    try:
        return _get_idempotency_store().list_unconfirmed()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning("list_unconfirmed_task_ids failed", exc_info=True)
        return []


def reconcile_unconfirmed(
    verifier: Callable[[str], bool] | None = None,
) -> dict[str, int]:
    """Reconcile submitted-but-unconfirmed tasks.

    For each unconfirmed task_id: ``verifier(task_id)`` True → promote to
    completed; False → clear (eligible for retry); raises → leave intact for
    next pass / TTL eviction. If *verifier* is None, only TTL eviction runs.

    Returns counters ``{"checked", "confirmed", "cleared", "remaining"}``.
    """
    store = _get_idempotency_store()
    task_ids = store.list_unconfirmed()
    stats = {"checked": 0, "confirmed": 0, "cleared": 0, "remaining": 0}
    for task_id in task_ids:
        stats["checked"] += 1
        if verifier is None:
            continue
        try:
            ok = bool(verifier(task_id))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.warning(
                "reconcile_unconfirmed verifier raised for task_id=%s — leaving entry intact",
                task_id, exc_info=True,
            )
            continue
        try:
            if ok:
                store.mark_completed(task_id)
                stats["confirmed"] += 1
            else:
                store.clear_unconfirmed(task_id)
                stats["cleared"] += 1
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.warning(
                "reconcile_unconfirmed failed to update store for task_id=%s",
                task_id, exc_info=True,
            )
    # Trigger TTL-based eviction for anything that aged out without reconciliation.
    with _idempotency_lock:
        _evict_expired_task_ids()
    stats["remaining"] = len(store.list_unconfirmed())
    return stats


def _shutdown_cdp_executor(*, expected: bool = False) -> None:
    """Shutdown the shared CDP executor.

    Args:
        expected: When True, called during normal process-exit/atexit shutdown
            and should not emit Bug 8 forensic stack diagnostics. When False,
            indicates an unexpected runtime shutdown and emits stack traces for
            forensic tracing.

    Uses ``wait=False`` so this function returns immediately and never blocks
    on hung CDP calls. In-flight threads continue running until their CDP call
    completes or the process terminates. The ``_cdp_orphaned_threads`` metric
    gives a best-estimate of how many threads may still be running at shutdown.
    """
    with _cdp_executor_lock:
        if expected:
            _logger.info(
                "CDP executor shutdown requested during expected process exit.",
            )
        else:
            # Bug 8: capture caller stack so premature shutdowns are traceable
            # in production logs.
            _logger.error(
                "_shutdown_cdp_executor called before expected process exit — "
                "capturing stack for Bug 8 forensics.",
                stack_info=True,
            )
        with _cdp_metric_lock:
            _snap_active = _active_cdp_requests
        _snap_orphaned = _cdp_orphaned_threads  # protected by _cdp_executor_lock
        _logger.info(
            "Shutting down CDP executor (active_cdp_requests=%d, orphaned_threads=%d). "
            "In-flight threads will continue running until natural completion.",
            _snap_active, _snap_orphaned,
        )
        _cdp_executor.shutdown(wait=False, cancel_futures=True)
        _logger.info("CDP executor shutdown issued.")

atexit.register(_shutdown_cdp_executor, expected=True)


def _get_cdp_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the active CDP executor, replacing it if saturated with orphaned threads.

    Acquires ``_cdp_executor_lock`` internally. When ``_cdp_orphaned_threads``
    reaches ``CDP_EXECUTOR_MAX_WORKERS``, all slots are likely occupied by hung
    (timed-out) threads and no new submissions will start immediately. In that
    case the old executor is discarded (without waiting) and a fresh one is
    created so that subsequent calls are not permanently queued.

    Bug 8 recovery: if the executor has been shut down through any path other
    than the saturation branch (atexit hook misfire, test pollution, future
    regression), this function transparently recreates a fresh executor and
    emits a CRITICAL log so operators can investigate the upstream caller.
    """
    global _cdp_executor, _cdp_orphaned_threads  # pylint: disable=global-statement,invalid-name
    with _cdp_executor_lock:
        # Bug 8: recover from external shutdown. Must be checked BEFORE the
        # saturation branch so a dead executor is never returned to callers.
        if getattr(_cdp_executor, "_shutdown", False):
            _logger.critical(
                "[trace=%s] CDP executor was found _shutdown=True at runtime; "
                "recreating. This indicates a premature shutdown bug — "
                "see Bug 8 / inspect '_shutdown_cdp_executor called' stack logs.",
                _get_trace_id(),
            )
            _cdp_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=CDP_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="cdp-timeout",
            )
            _cdp_orphaned_threads = 0
            return _cdp_executor

        if _cdp_orphaned_threads >= CDP_EXECUTOR_MAX_WORKERS:
            _cdp_executor.shutdown(  # pylint: disable=unexpected-keyword-arg
                wait=False, cancel_futures=False,
            )
            # cancel_futures=False is intentional: already-running threads cannot be
            # interrupted in CPython's ThreadPoolExecutor — they will run to completion
            # regardless. We only want to stop accepting new submissions on the old
            # executor and let the OS reclaim resources naturally.
            _logger.critical(
                "CDP executor saturated (%d orphans) — replacing executor",
                _cdp_orphaned_threads,
            )
            _cdp_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=CDP_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="cdp-timeout",
            )
            _cdp_orphaned_threads = 0
        return _cdp_executor


def _evict_expired_task_ids() -> None:
    """Remove task_ids that have exceeded the TTL. Must be called while holding _idempotency_lock."""
    cutoff = time.monotonic() - _IDEMPOTENCY_TTL
    expired = [k for k, ts in _completed_task_ids.items() if ts < cutoff]
    for k in expired:
        del _completed_task_ids[k]
    expired_sub = [k for k, ts in _submitted_task_ids.items() if ts < cutoff]
    for k in expired_sub:
        del _submitted_task_ids[k]
    # Unconfirmed entries use their own per-entry TTL (not _IDEMPOTENCY_TTL).
    now_mono = time.monotonic()
    expired_unconf = [
        k for k, (ts, ttl) in _unconfirmed_task_ids.items() if (now_mono - ts) >= ttl
    ]
    for k in expired_unconf:
        del _unconfirmed_task_ids[k]
        _logger.warning(
            "Unconfirmed task_id=%s TTL expired without reconciliation — "
            "entry cleared; manual review may still be required", k,
        )
    inflight_cutoff = time.monotonic() - _IN_FLIGHT_TTL_SECONDS
    stale_inflight = [k for k, ts in _in_flight_task_ids.items() if ts < inflight_cutoff]
    for k in stale_inflight:
        del _in_flight_task_ids[k]
        _logger.warning("Evicting stale in-flight task_id %s — worker likely crashed mid-cycle", k)


# ── CDP timeout helper (HIGH-02) ──────────────────────────────────

def _cdp_call_with_timeout(fn: Callable, *args: Any, timeout: float = _CDP_CALL_TIMEOUT, **kwargs: Any) -> Any:
    """Execute a CDP call with a caller-side timeout using the shared CDP executor.

    Submits *fn* to the shared ``_cdp_executor`` (ThreadPoolExecutor). The task
    is enqueued immediately — submit() does not block waiting for a free thread
    slot. ``future.result(timeout=timeout)`` is then called to wait for the result.

    If the timeout expires:
    - The caller is unblocked and ``SessionFlaggedError`` is raised.
    - ``future.cancel()`` is attempted as a best-effort hint. Because the task
      is likely already running, cancel() is a no-op in the common case — the
      underlying thread continues running until the CDP call completes or the
      browser process is killed. This is an inherent limitation of CPython's
      ThreadPoolExecutor; there is no mechanism to interrupt a running thread.
    - If all ``max_workers`` slots are occupied by hung (timed-out) tasks,
      new submissions will queue in the executor's internal work queue, increasing
      end-to-end latency. Monitor ``_active_cdp_requests`` and ``_cdp_timeout_count``
      to detect this condition.

    Args:
        fn: CDP callable to invoke.
        *args: Positional arguments forwarded to *fn*.
        timeout: Maximum seconds to wait (default: ``_CDP_CALL_TIMEOUT``).
        **kwargs: Keyword arguments forwarded to *fn*.

    Raises:
        SessionFlaggedError: If the call does not complete within *timeout*
            seconds, or if the executor is unavailable (e.g. after shutdown).
    """
    global _cdp_timeout_count  # pylint: disable=global-statement,invalid-name
    global _active_cdp_requests  # pylint: disable=global-statement,invalid-name
    global _cdp_orphaned_threads  # pylint: disable=global-statement,invalid-name
    fn_name = getattr(fn, "__name__", repr(fn))
    executor = _get_cdp_executor()

    with _cdp_metric_lock:
        _active_cdp_requests += 1
    try:
        try:
            future = executor.submit(fn, *args, **kwargs)
        except RuntimeError as exc:
            msg = str(exc)
            is_interpreter_shutdown = "interpreter shutdown" in msg.lower()
            _logger.error(
                "[trace=%s] CDP executor submit failed for '%s': %r "
                "(executor_shutdown=%s, orphaned_threads=%d, max_workers=%d, "
                "interpreter_shutdown=%s)",
                _get_trace_id(),
                fn_name,
                exc,
                getattr(executor, "_shutdown", None),
                _cdp_orphaned_threads,
                CDP_EXECUTOR_MAX_WORKERS,
                is_interpreter_shutdown,
                exc_info=True,
            )
            if is_interpreter_shutdown:
                # Process is tearing down — recovery is meaningless.
                raise SessionFlaggedError(
                    f"CDP call '{fn_name}' rejected because the Python interpreter "
                    "is shutting down"
                ) from exc
            raise SessionFlaggedError(
                f"CDP call '{fn_name}' could not be scheduled because "
                "the CDP executor is unavailable"
            ) from exc
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()  # Best-effort; no-op if the task is already running.
            with _cdp_executor_lock:
                _cdp_orphaned_threads += 1  # thread may still occupy an executor slot
                _snapshot_orphaned = _cdp_orphaned_threads
            with _cdp_metric_lock:
                _cdp_timeout_count += 1
                _snapshot_active = _active_cdp_requests
                _snapshot_timeouts = _cdp_timeout_count
            _logger.warning(
                "[trace=%s] CDP call '%s' timed out after %.1fs "
                "(active_cdp_requests=%d, total_timeouts=%d, orphaned_threads=%d). "
                "Note: the underlying thread may still be running; "
                "executor saturation risk if orphaned_threads approaches max_workers.",
                _get_trace_id(),
                fn_name,
                timeout,
                _snapshot_active,
                _snapshot_timeouts,
                _snapshot_orphaned,
            )
            raise SessionFlaggedError(
                f"CDP call '{fn_name}' timed out after {timeout}s for worker"
            )
    finally:
        with _cdp_metric_lock:
            _active_cdp_requests -= 1


def get_cdp_metrics() -> dict:
    """Return a snapshot of CDP executor health metrics.

    Returns:
        dict with keys:
            ``total_timeouts``: cumulative count of caller-side timeouts.
                Incremented each time ``future.result(timeout=...)`` raises
                ``TimeoutError`` inside ``_cdp_call_with_timeout()``.
            ``active_cdp_requests``: orchestration-level in-flight count.
                Incremented before ``_cdp_executor.submit()`` and
                decremented in the ``finally`` block — always on the
                **caller's** thread.
            ``orphaned_cdp_threads``: cumulative count of CDP threads that
                timed out and may still be occupying an executor slot.
                After a timeout, ``future.cancel()`` is a no-op for running
                tasks, so the thread may continue running. When
                ``orphaned_cdp_threads`` approaches ``CDP_EXECUTOR_MAX_WORKERS``,
                the executor is likely saturated — new calls will queue rather
                than start immediately.

                .. warning::
                    After a timeout, the caller's ``finally`` block
                    decrements ``active_cdp_requests`` immediately, but the
                    underlying executor thread may still be running the CDP call.
                    ``active_cdp_requests == 0`` does NOT mean all executor
                    threads are idle. Monitor ``orphaned_cdp_threads`` growth
                    relative to ``CDP_EXECUTOR_MAX_WORKERS`` for saturation risk.
    """
    with _cdp_metric_lock:
        total_timeouts = _cdp_timeout_count
        active_cdp_requests = _active_cdp_requests
    with _cdp_executor_lock:
        orphaned_cdp_threads = _cdp_orphaned_threads
    return {
        "total_timeouts": total_timeouts,
        "active_cdp_requests": active_cdp_requests,
        "orphaned_cdp_threads": orphaned_cdp_threads,
    }


def initialize_cycle(worker_id: str = "default"):
    """Reset FSM registry and register all valid states for a new cycle."""
    _get_idempotency_store()
    rollout.configure(monitor.check_rollback_needed, monitor.save_baseline)
    fsm.initialize_for_worker(worker_id)


def _make_profile_id(profile: "billing.BillingProfile") -> str:
    """Create a one-way anonymized identifier for a billing profile.

    Uses SHA-256 hash of 'first_name|last_name|zip_code' to produce
    a non-reversible profile fingerprint. No raw PII is included in logs.

    Privacy: one-way hash; used only as grouping key in audit logs.
    No raw PII leaves this function.

    Returns:
        Full 64-character lowercase SHA-256 hex digest (Blueprint §12 line 703).
    """
    raw = f"{profile.first_name}|{profile.last_name}|{profile.zip_code}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _emit_billing_audit_event(
    profile: "billing.BillingProfile",
    worker_id: str,
    task_id: str | None,
    zip_code: str | int | None,
    selection_method: str | None = None,
) -> None:
    """Emit a structured audit event for a successful billing profile selection.

    Privacy contract:
    - No raw PII (name, address, phone, email) is included in the event.
    - profile_id is a one-way SHA-256 hash of 'first_name|last_name|zip_code'.
    - requested_zip is the raw zip_code argument (proxy zip), logged for tracing only.

    Selection method contract (Blueprint §12):
    - ``selection_method`` reflects the *actual outcome* of the selection, not
      the request intent.  When provided by the caller it is used verbatim.
    - When ``selection_method`` is ``None`` the event is labeled with the
      explicit ``billing.SELECTION_METHOD_UNKNOWN`` sentinel.  The audit MUST
      NEVER infer the label from the request (e.g. "zip was provided →
      zip_match"); doing so causes the §12 monitoring drift this contract
      exists to prevent.  Direct callers that bypass ``select_profile`` are
      expected to either pass the resolved outcome or accept the
      ``unknown`` label.

    Non-interference contract:
    - This function MUST only be called AFTER billing.select_profile() has returned.
    - Exceptions are caught and logged as warnings; they never propagate.
    - No delay, no state mutation, no FSM interaction.
    """
    try:
        requested_zip = None if zip_code is None else str(zip_code)
        if not isinstance(selection_method, str):
            # Fall back to the explicit unknown sentinel.  Use ``getattr`` so a
            # wholesale-mocked ``billing`` module (common in integration tests)
            # cannot poison the audit payload with a non-string MagicMock; if
            # the constant isn't a real ``str`` we hard-code the literal
            # "unknown" value so the event remains JSON-serializable.
            unknown_const = getattr(billing, "SELECTION_METHOD_UNKNOWN", "unknown")
            selection_method = unknown_const if isinstance(unknown_const, str) else "unknown"
        event = {
            "event_type": "billing_selection",
            "worker_id": worker_id,
            "task_id": task_id,
            "selection_method": selection_method,
            "requested_zip": requested_zip,
            "profile_id": _make_profile_id(profile),
            "trace_id": _get_trace_id(),
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _AUDIT_LOGGER.info("billing_selection %s", json.dumps(event, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning(
            "[trace=%s] Failed to emit billing audit event for worker=%s: %s",
            _get_trace_id(),
            worker_id,
            exc,
        )


def _select_profile_with_audit(
    zip_code: str | int | None,
    worker_id: str,
    task_id: str | None,
    *,
    include_worker_id: bool,
):
    """Select a billing profile and emit the audit event exactly once.

    This helper is the single call-site for ``_emit_billing_audit_event`` so
    both ``run_cycle`` and direct ``run_payment_step(..., _profile=None)``
    paths share the same "select -> emit" contract without duplicating logic.

    The actual selection outcome (``zip_match`` vs ``round_robin``) is
    captured from billing's thread-local side channel via
    ``billing.get_last_selection_method()`` so the audit event reflects what
    really happened — not just whether a zip was requested (Blueprint §12).
    The thread-local is cleared *before* the select call so a stale value
    from a prior real selection cannot leak into this audit when
    ``billing.select_profile`` is wholesale-mocked in tests.
    """
    # Clear the side channel up-front: if billing is fully mocked (common in
    # tests) select_profile() won't write a fresh outcome, and we must not
    # observe whatever the previous real call left behind.
    try:
        billing.clear_last_selection_method()
    except AttributeError:
        logging.debug(
            "billing.clear_last_selection_method unavailable; "
            "continuing without pre-clear (expected for some test mocks)."
        )
    if include_worker_id:
        profile = billing.select_profile(zip_code, worker_id=worker_id)
    else:
        profile = billing.select_profile(zip_code)
    # Read the *actual* outcome via billing's thread-local side channel.
    # In production select_profile() sets this on every successful return.
    # When ``billing`` is wholesale-mocked in tests the accessor may be
    # missing or return a non-string MagicMock; coerce either case to
    # ``None`` so the audit emitter labels the event with the explicit
    # ``SELECTION_METHOD_UNKNOWN`` sentinel — never inferred from request
    # intent — preserving outcome-only semantics (Blueprint §12).
    try:
        raw_method = billing.get_last_selection_method()
    except AttributeError:
        raw_method = None
    selection_method = raw_method if isinstance(raw_method, str) else None
    _emit_billing_audit_event(
        profile=profile,
        worker_id=worker_id,
        task_id=task_id,
        zip_code=zip_code,
        selection_method=selection_method,
    )
    return profile


def _validated_notify_total(worker_id: str, value: float) -> bool:
    """Validate *value* is finite and do a first-notify-wins call to watchdog.

    Acquires ``_network_listener_lock`` internally. Must NOT be called while
    the caller already holds that lock.

    Returns:
        ``True`` if ``watchdog.notify_total()`` was called, ``False`` if the
        value was rejected (non-finite) or a notification was already sent for
        this worker in the current cycle.
    """
    if not math.isfinite(value):
        _logger.warning(
            "[trace=%s] DOM total for worker=%s is non-finite (%s); skipping.",
            _get_trace_id(), worker_id, value,
        )
        return False
    with _network_listener_lock:
        if worker_id in _notified_workers_this_cycle:
            return False
        watchdog.notify_total(worker_id, value)
        _notified_workers_this_cycle.add(worker_id)
    return True


def _notify_total_from_dom(driver_obj, worker_id: str) -> None:
    """DOM fallback: read checkout total from DOM and notify watchdog.

    Called only when the primary CDP ``Network.getResponseBody`` path fails,
    returns an empty body, or yields no recognised total key.

    First-notify-wins: if a total has already been notified for *worker_id* in
    the current cycle, subsequent calls are silently skipped under
    ``_network_listener_lock`` to prevent value overwrite races.
    """
    raw_driver = _unwrap_raw_driver(driver_obj)
    try:
        result = raw_driver.execute_script(
            "var el = document.querySelector('#cws_lbl_orderTotal, .order-total, .checkout-total, [data-total]');"
            "return el ? el.innerText : null;"
        )
        if isinstance(result, (int, float)):
            _validated_notify_total(worker_id, float(result))
            return
        if isinstance(result, str) and result:
            # Locale-aware money parser shared with GivexDriver._read_dom_order_total
            # so US ($1,234.56), European (1.234,56) and bare-decimal (49,99)
            # totals all decode consistently across the watchdog DOM-fallback
            # and submit-time cross-check paths (E3 audit).
            from modules.cdp.driver import _parse_money_text  # noqa: PLC0415
            parsed = _parse_money_text(result)
            if parsed is not None:
                _validated_notify_total(worker_id, float(parsed))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning("[trace=%s] DOM total read failed: %s", _get_trace_id(), exc)


def _dom_only_fallback_enabled() -> bool:
    """Lazy proxy for ``integration.runtime.is_dom_only_watchdog_allowed``."""
    try:
        from integration.runtime import is_dom_only_watchdog_allowed  # noqa: PLC0415
        return is_dom_only_watchdog_allowed()
    except ImportError:  # pragma: no cover — defensive only
        return False


def _start_phase_a_dom_polling(driver_obj, worker_id: str) -> None:
    """Start a daemon thread that polls the DOM for the Phase A pricing total.

    Used only when the registered driver lacks ``add_cdp_listener`` AND the
    operator opted into the ``ALLOW_DOM_ONLY_WATCHDOG`` fallback (see
    :func:`integration.runtime.probe_cdp_listener_support`).  Without this
    polling Phase A always times out after ``_WATCHDOG_TIMEOUT_PAYMENT``s.

    The thread polls every ~0.5s and exits as soon as the worker is in
    ``_notified_workers_this_cycle`` (first-notify-wins), the per-worker stop
    event is set, or the ``_WATCHDOG_TIMEOUT_PAYMENT + 1.0``s deadline lapses.
    """
    poll_interval = 0.5
    deadline = time.monotonic() + float(_WATCHDOG_TIMEOUT_PAYMENT) + 1.0
    stop_event = threading.Event()
    with _network_listener_lock:
        prev = _dom_polling_stop_events.get(worker_id)
        if prev is not None:
            prev.set()
        _dom_polling_stop_events[worker_id] = stop_event

    def _poll() -> None:
        try:
            while time.monotonic() < deadline:
                if stop_event.is_set():
                    return
                with _network_listener_lock:
                    already_notified = worker_id in _notified_workers_this_cycle
                if already_notified:
                    return
                try:
                    _notify_total_from_dom(driver_obj, worker_id)
                except Exception:  # noqa: BLE001  # pylint: disable=broad-except
                    pass  # _notify_total_from_dom already logs.
                if stop_event.wait(poll_interval):
                    return
        finally:
            with _network_listener_lock:
                # Only remove the entry if it is still ours (a concurrent
                # restart may have replaced it with a fresh event).
                if _dom_polling_stop_events.get(worker_id) is stop_event:
                    _dom_polling_stop_events.pop(worker_id, None)

    thread = threading.Thread(
        target=_poll, name=f"phaseA-dom-poll-{worker_id}", daemon=True,
    )
    thread.start()


def _stop_phase_a_dom_polling(worker_id: str) -> None:
    """Signal the Phase A polling thread (if any) to exit promptly."""
    with _network_listener_lock:
        stop_event = _dom_polling_stop_events.get(worker_id)
    if stop_event is not None:
        stop_event.set()


def _setup_network_total_listener(driver_obj, worker_id: str) -> None:
    """Enable CDP Network monitoring and set up total interception.

    Runtime guarantee (issue F2 audit): when the driver exposes a callable
    ``add_cdp_listener``, attach the ``Network.responseReceived`` callback
    (drives Phase A + Phase C signals).  When the hook is absent and
    ``ALLOW_DOM_ONLY_WATCHDOG=1``, start a Phase A DOM-polling thread (Phase C
    already runs ``_notify_total_from_dom`` synchronously, so no extra polling
    is needed there).  The probe at registration time guarantees we never
    reach the silent-skip branch in production; the silent skip is retained
    only so test stubs without the hook keep working.
    """
    raw_driver = _unwrap_raw_driver(driver_obj)
    network_enabled = False
    try:
        raw_driver.execute_cdp_cmd("Network.enable", {})
        network_enabled = True
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning("[trace=%s] Network.enable failed: %s", _get_trace_id(), exc)
    try:
        add_listener = getattr(raw_driver, "add_cdp_listener", None)
        # Register the CDP listener only after Network.enable succeeds; when
        # Network.enable fails we intentionally fall through to the DOM-only
        # watchdog fallback below if the operator opted in.
        if network_enabled and callable(add_listener):
            def _on_response(params):
                # pylint: disable=too-many-nested-blocks
                try:
                    if not isinstance(params, dict):
                        return
                    response = params.get("response", {})
                    url = str(response.get("url", ""))
                    if not any(part in url for part in _CDP_NETWORK_URL_PATTERNS):
                        return

                    request_id = params.get("requestId")
                    body_parsed = False

                    if request_id:
                        try:
                            body_resp = raw_driver.execute_cdp_cmd(
                                "Network.getResponseBody",
                                {"requestId": request_id},
                            )
                            body_str = (
                                body_resp.get("body", "")
                                if isinstance(body_resp, dict)
                                else ""
                            )
                            if body_str:
                                body_data = json.loads(body_str)
                                total_raw = None
                                for key in ("total", "order_total", "orderTotal", "amount"):
                                    candidate = body_data.get(key)
                                    if candidate is not None:
                                        total_raw = candidate
                                        break
                                if total_raw is not None:
                                    _validated_notify_total(worker_id, float(total_raw))
                                    body_parsed = True
                        except Exception as body_exc:  # noqa: BLE001  # pylint: disable=broad-except
                            _logger.warning(
                                "[trace=%s] Network.getResponseBody parse failed for "
                                "worker=%s; falling back to DOM: %s",
                                _get_trace_id(),
                                worker_id,
                                body_exc,
                            )

                    if not body_parsed:
                        _logger.warning(
                            "[trace=%s] CDP body unavailable for worker=%s; "
                            "using DOM fallback",
                            _get_trace_id(),
                            worker_id,
                        )
                        _notify_total_from_dom(raw_driver, worker_id)
                except Exception as callback_exc:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.warning(
                        "[trace=%s] Network.responseReceived callback failed: %s",
                        _get_trace_id(),
                        callback_exc,
                    )
            add_listener("Network.responseReceived", _on_response)
        elif _dom_only_fallback_enabled():
            # Listener unavailable or Network.enable failed + fallback opt-in:
            # start Phase A DOM polling.  Without env opt-in we keep legacy
            # silent-skip so test stubs work.
            _logger.warning(
                "[trace=%s] DEGRADED mode: ALLOW_DOM_ONLY_WATCHDOG=1 — "
                "starting Phase A DOM polling fallback "
                "(network_enabled=%s, add_cdp_listener=%s) for worker=%s.",
                _get_trace_id(),
                network_enabled,
                callable(add_listener),
                worker_id,
            )
            _start_phase_a_dom_polling(raw_driver, worker_id)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning(
            "[trace=%s] Failed to set Network.responseReceived listener: %s",
            _get_trace_id(),
            exc,
        )


def run_payment_step(task, zip_code=None, worker_id: str = "default", _profile=None):
    """Execute one payment attempt.

    Steps:
      1. Select a billing profile from the pool (or use *_profile* if provided).
      2. Enable the network watchdog + DOM listener for this worker.
      3. Run pre-card checkout preparation via CDP (preflight_geo → navigate →
         fill eGift form → add to cart → guest checkout).  DOM polling is
         already armed at this point so Phase A can observe the real page.
      4. Wait for the pricing total (Phase A — INV-PAYMENT-01 gate).
         If the watchdog times out here, abort BEFORE any card field is typed.
      5. Wire Phase A total into the driver for DOM cross-check.
      6. Re-arm watchdog for Phase C.
      7. Fill card / billing payment fields via CDP (only after pricing confirmed).
      8. Persist the idempotency checkpoint (U-07: mark_submitted BEFORE submit).
      9. Submit the purchase via CDP (the irreversible action).
     10. Wait for the checkout total to be confirmed by the watchdog (Phase C).
     11. Return (state, total).

    Args:
        task: WorkerTask containing card and order information.
        zip_code: Optional zip code for billing profile matching.
        worker_id: Unique identifier for this worker (used to key the watchdog session).
        _profile: Pre-selected :class:`~modules.common.types.BillingProfile`.
            When provided, ``billing.select_profile`` is **not** called (billing
            is locked for the cycle by the caller, e.g. via :class:`CycleContext`).
            When ``None`` (default), a new profile is selected from the pool.

    Returns:
        A (state, total) tuple where state is a State object or None,
        and total is the confirmed checkout amount.

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out waiting for the total.
        RuntimeError: if no CDP driver has been registered.
    """
    if _profile is not None:
        profile = _profile
        # Audit event already emitted on the initial selection earlier in the
        # cycle; do NOT re-emit when the same profile is reused on a retry /
        # swap-card iteration.  Blueprint §12 line 693: "Mỗi lần
        # billing.select_profile() trả về thành công..." — one audit event
        # per actual select_profile() call, not per run_payment_step() call.
    else:
        profile = _select_profile_with_audit(
            zip_code,
            worker_id=worker_id,
            task_id=getattr(task, "task_id", None),
            include_worker_id=False,
        )
    driver_obj = cdp._get_driver(worker_id)  # pylint: disable=protected-access
    if driver_obj is None:
        raise RuntimeError(f"No driver object returned for worker '{worker_id}'.")
    # Reset first-notify-wins guard for this worker's new cycle before enabling the watchdog.
    with _network_listener_lock:
        _notified_workers_this_cycle.discard(worker_id)
    watchdog.enable_network_monitor(worker_id)
    _setup_network_total_listener(driver_obj, worker_id)
    _submitted_before_wait = False
    # Phase 5A Task 2C: surface the active BehaviorStateMachine (published
    # by the behaviour wrapper into a ContextVar) so the wait_for_total
    # call-sites below can flip the CRITICAL_SECTION flag for the
    # duration of the blocking watchdog wait.
    _behavior_sm = _get_current_sm()
    try:
        # ── Pre-card preparation: navigate + eGift form + cart + guest checkout.
        # This runs BEFORE the Phase A pricing wait so that the DOM polling
        # thread (ALLOW_DOM_ONLY_WATCHDOG=1 / degraded mode) has a real Givex
        # checkout page to query instead of about:blank.  No card/billing data
        # is typed here — INV-PAYMENT-01 is preserved.
        _logger.info(
            "[trace=%s] pre_card_prepare_started for worker=%s",
            _get_trace_id(), worker_id,
        )
        try:
            _cdp_call_with_timeout(
                cdp.run_pre_card_checkout_prepare,
                task,
                profile,
                worker_id=worker_id,
            )
        except Exception:
            # If pre-card prepare fails, Phase A wait will not run — we must
            # stop the DOM polling thread that ``_setup_network_total_listener``
            # armed above, otherwise it keeps querying the (still-stalled) page
            # until ``PAYMENT_WATCHDOG_TIMEOUT_S`` expires on its own.  Safe
            # to call even when no polling thread exists (degraded vs CDP path).
            # ``Exception`` (not ``BaseException``) so KeyboardInterrupt /
            # SystemExit / cancellation propagate untouched for clean teardown.
            _stop_phase_a_dom_polling(worker_id)
            raise
        _logger.info(
            "[trace=%s] pre_card_prepare_completed for worker=%s",
            _get_trace_id(), worker_id,
        )

        # ── Phase A: Wait for pricing total BEFORE touching card fields.
        # Anti-fraud INV-PAYMENT-01 (Blueprint §5):  if the pricing endpoint
        # stalls, abort BEFORE typing any card data so no submission occurs.
        _logger.info(
            "[trace=%s] phase_a_wait_started for worker=%s",
            _get_trace_id(), worker_id,
        )
        try:
            if _behavior_sm is not None:
                _behavior_sm.enter_critical_zone("api_wait")
            try:
                preflight_total = watchdog.wait_for_total(
                    worker_id, timeout=_WATCHDOG_TIMEOUT_PAYMENT,
                )
            finally:
                if _behavior_sm is not None:
                    _behavior_sm.exit_critical_zone()
                # Stop Phase A DOM polling (no-op when CDP listener is in use).
                _stop_phase_a_dom_polling(worker_id)
        except SessionFlaggedError:
            _logger.warning(
                "[trace=%s] Pricing watchdog timeout BEFORE fill for worker=%s, task_id=%s; "
                "aborting before any card field is typed",
                _get_trace_id(),
                worker_id,
                getattr(task, "task_id", None),
            )
            raise
        _logger.info(
            "[trace=%s] phase_a_total_received total=%s for worker=%s",
            _get_trace_id(), preflight_total, worker_id,
        )
        # E3 audit: wire the Phase A total into the driver so submit_purchase
        # can cross-check the on-page Order Total via DOM right before the
        # irreversible COMPLETE PURCHASE click (Spec §5 line 287).  Best-
        # effort: a wiring failure (e.g. driver does not implement the new
        # contract in older deployments) must not block submit; the driver
        # then falls back to the legacy no-cross-check path.
        try:
            cdp.set_expected_total(worker_id, preflight_total)
        except (AttributeError, RuntimeError, ValueError) as _exp_exc:
            _logger.warning(
                "[trace=%s] set_expected_total failed for worker=%s: %s; "
                "submit will proceed without DOM cross-check",
                _get_trace_id(), worker_id, _exp_exc,
            )
        # Re-arm watchdog for the post-submit confirmation total (Phase C).
        watchdog.enable_network_monitor(worker_id)

        # ── Phase B: Card fill + submit (only after pricing is confirmed).
        # INV-PAYMENT-01: card and billing-payment fields MUST only be typed
        # after the Phase A total is confirmed above.
        _logger.info(
            "[trace=%s] card_fill_started for worker=%s",
            _get_trace_id(), worker_id,
        )
        _cdp_call_with_timeout(
            cdp.run_payment_card_fill,
            task.primary_card,
            profile,
            worker_id=worker_id,
        )
        _logger.info(
            "[trace=%s] card_fill_completed for worker=%s",
            _get_trace_id(), worker_id,
        )
        # U-07: Persist the idempotency checkpoint BEFORE the irreversible submit
        # action.  If the process crashes between mark_submitted and submit_purchase,
        # the submitted state blocks re-execution on restart, preventing a
        # double-charge even though no payment was actually processed.
        _task_id = getattr(task, "task_id", None)
        if _task_id is not None:
            _get_idempotency_store().mark_submitted(_task_id)
            _submitted_before_wait = True
        # Submit the purchase (the irreversible action — must come AFTER mark_submitted).
        _logger.info(
            "[trace=%s] submit_started for worker=%s",
            _get_trace_id(), worker_id,
        )
        _cdp_call_with_timeout(
            cdp.submit_purchase,
            worker_id=worker_id,
        )
        _logger.info(
            "[trace=%s] submit_completed for worker=%s",
            _get_trace_id(), worker_id,
        )
        # P0-1: Detect page state immediately after submit and wire FSM transition.
        # This is the primary path for all FSM state changes in production.
        # Spinner-visible ``ui_busy`` is treated as active loading: re-probe
        # until a stable state appears (or until the busy-recheck budget is
        # exhausted). Only canonical FSM states are forwarded to
        # ``transition_for_worker``; a residual ``ui_busy`` is left to the
        # post-submit fallback below to retry once Phase C completes.
        try:
            _page_state = _settle_busy_page_state(
                lambda: cdp.detect_page_state(worker_id)
            )
            if _page_state in _FSM_STATES:
                fsm.transition_for_worker(worker_id, _page_state)
            else:
                _logger.info(
                    "[trace=%s] worker=%s post-submit state=%s is non-FSM; "
                    "deferring transition to fallback path",
                    _get_trace_id(), worker_id, _page_state,
                )
        except InvalidTransitionError as _fsm_exc:
            _logger.warning(
                "[trace=%s] FSM InvalidTransitionError after submit for worker=%s: %s",
                _get_trace_id(), worker_id, _fsm_exc,
            )
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.warning(
                "[trace=%s] detect_page_state failed after submit for worker=%s — "
                "FSM transition skipped; fallback will attempt at state read.",
                _get_trace_id(), worker_id, exc_info=True,
            )
        # ── Phase C: Post-submit confirmation total (best-effort).
        # Submit already happened; if the confirmation total never arrives we
        # mark the task as unconfirmed (TTL) but do NOT raise — the submit is
        # irreversible and downstream reconciliation handles the unconfirmed
        # state.  This preserves the existing "submitted-but-unconfirmed"
        # recovery path.
        _notify_total_from_dom(driver_obj, worker_id)
        try:
            if _behavior_sm is not None:
                _behavior_sm.enter_critical_zone("api_wait")
            try:
                total = watchdog.wait_for_total(
                    worker_id, timeout=_WATCHDOG_TIMEOUT_PAYMENT,
                )
            finally:
                if _behavior_sm is not None:
                    _behavior_sm.exit_critical_zone()
        except SessionFlaggedError as _post_exc:
            _logger.warning(
                "[trace=%s] Post-submit total missing for worker=%s, task_id=%s; "
                "marking unconfirmed: %s",
                _get_trace_id(), worker_id, _task_id, _post_exc,
            )
            watchdog.reset_session(worker_id)
            if _task_id is not None:
                try:
                    _get_idempotency_store().mark_unconfirmed(
                        _task_id, ttl_seconds=_UNCONFIRMED_TTL_SECONDS,
                    )
                except Exception:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.error(
                        "Failed to mark task_id=%s as unconfirmed in idempotency store",
                        _task_id, exc_info=True,
                    )
            total = None
    except SessionFlaggedError as exc:
        _task_id_log = getattr(task, "task_id", None)
        try:
            _alerting.send_alert(
                f"Watchdog timeout worker={worker_id} task_id={_task_id_log}"
            )
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.debug("alerting.send_alert (watchdog) failed", exc_info=True)
        if _submitted_before_wait:
            _logger.error(
                "[trace=%s] Watchdog timeout AFTER payment submission for worker=%s, task_id=%s. "
                "Payment may have been processed; task marked submitted. Do NOT retry blindly: %s",
                _get_trace_id(), worker_id, _task_id_log, exc,
            )
        else:
            _logger.error(
                "[trace=%s] Watchdog timeout BEFORE payment submission "
                "for worker=%s, task_id=%s: %s",
                _get_trace_id(), worker_id, _task_id_log, exc,
            )
        watchdog.reset_session(worker_id)
        if _submitted_before_wait:
            _logger.critical(
                "PAYMENT_SUBMITTED_UNCONFIRMED: task_id=%s worker_id=%s — total was never "
                "confirmed; manual review required before retrying",
                _task_id_log, worker_id,
            )
            # Track as unconfirmed (TTL); is_duplicate blocks retries until
            # reconcile_unconfirmed promotes/clears or the TTL expires.
            if _task_id_log is not None:
                try:
                    _get_idempotency_store().mark_unconfirmed(
                        _task_id_log, ttl_seconds=_UNCONFIRMED_TTL_SECONDS,
                    )
                except Exception:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.error(
                        "Failed to mark task_id=%s as unconfirmed in idempotency store",
                        _task_id_log, exc_info=True,
                    )
        raise
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
    if state is None:
        # P0-1 fallback: FSM was never transitioned by the primary path (e.g.
        # detect_page_state raised after submit, or returned ``ui_busy``).  Try
        # once more here, settling any remaining spinner-visible loading first
        # so we don't push a non-FSM ``ui_busy`` value into the FSM.
        try:
            _page_state = _settle_busy_page_state(
                lambda: cdp.detect_page_state(worker_id)
            )
            if _page_state in _FSM_STATES:
                state = fsm.transition_for_worker(worker_id, _page_state)
            else:
                _logger.warning(
                    "[trace=%s] FSM fallback state=%s is non-FSM for worker=%s — "
                    "returning state=None; handle_outcome will retry.",
                    _get_trace_id(), _page_state, worker_id,
                )
        except InvalidTransitionError as _fsm_exc:
            _logger.warning(
                "[trace=%s] FSM fallback InvalidTransitionError for worker=%s: %s",
                _get_trace_id(), worker_id, _fsm_exc,
            )
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.warning(
                "[trace=%s] FSM fallback detect_page_state failed for worker=%s — "
                "returning state=None; handle_outcome will retry.",
                _get_trace_id(), worker_id, exc_info=True,
            )
    return state, total


def _ctx_next_swap_card(ctx):
    if ctx.task is None or ctx.swap_count >= len(ctx.task.order_queue):
        return None
    return ctx.task.order_queue[ctx.swap_count]


def clear_refill_after_thank_you_popup(driver, new_card) -> None:
    """Clear card fields then refill with *new_card* after a "Thank you" popup.

    This implements the P1-2 workflow: after :func:`detect_popup_thank_you`
    confirms that the payment page is showing a success/confirmation signal,
    the card form fields are cleared via CDP (Ctrl+A + Backspace) and then
    refilled with the next card from the order queue.

    Steps:
    1. ``clear_card_fields_cdp`` — wipe card-number and CVV fields.
    2. ``fill_card_fields``      — fill card fields with *new_card*.

    Args:
        driver: GivexDriver instance (or compatible test double).
        new_card: :class:`~modules.common.types.CardInfo` for the next order.
    """
    try:
        _logger.info("[thank-you-refill] step=clear_card_fields_cdp")
        driver.clear_card_fields_cdp()
        _logger.info("[thank-you-refill] step=fill_card_fields (new card)")
        driver.fill_card_fields(new_card)
        _logger.info("[thank-you-refill] clear/refill sequence complete")
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning(
            "[thank-you-refill] clear/refill failed: %s", _sanitize_error(exc)
        )


def _settle_busy_page_state(get_page_state: Callable[[], str]) -> str:
    """Re-probe while the page is still visibly busy after submit.

    Spinner-visible pages are treated as active loading, not ``ui_lock``.
    Callers use this helper to wait for a stable non-busy state before making
    retry/focus-shift decisions.
    """
    page_state = get_page_state()
    attempts = 0
    while page_state == _UI_BUSY_STATE and attempts < _UI_BUSY_RECHECK_ATTEMPTS:
        attempts += 1
        time.sleep(_UI_BUSY_RECHECK_INTERVAL)
        page_state = get_page_state()
    return page_state


def _payment_url_matches(current: str, canonical: str) -> bool:
    """Compare a current URL against :data:`URL_PAYMENT`.

    Matches on scheme, host (case-insensitive) and path only — query string
    and fragment are intentionally ignored so benign cache-busters or
    post-VBV markers (e.g. ``?t=123``) still count as the canonical
    payment page. A trailing slash on either path is normalised away.
    """
    if not current or not canonical:
        return False
    try:
        from urllib.parse import urlsplit  # noqa: PLC0415

        cur = urlsplit(current)
        can = urlsplit(canonical)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        return False

    def _norm_path(p: str) -> str:
        return p.rstrip("/") if p not in ("", "/") else p

    return (
        cur.scheme.lower() == can.scheme.lower()
        and cur.netloc.lower() == can.netloc.lower()
        and _norm_path(cur.path) == _norm_path(can.path)
    )


def is_payment_page_reloaded(driver, *, check_billing_empty: bool = False) -> bool:
    """Return True when the browser is back on the canonical payment page.

    Primary signal is a direct match of ``driver.current_url`` against the
    canonical :data:`~modules.cdp.driver.URL_PAYMENT` (scheme + host + path,
    ignoring query / fragment). This replaces the prior billing-field-empty
    heuristic, which suffered from

    - *false negatives* — stale billing value left in the DOM after a VBV
      cancel reload caused the function to return False and skip the refill,
      creating a double-charge risk on the next retry; and
    - *false positives* — a missing billing selector (e.g. during a page
      transition to an unrelated route) caused the function to return True
      and trigger a refill on the wrong page.

    The billing-empty heuristic is preserved as an opt-in belt-and-suspenders
    check via ``check_billing_empty=True``: when enabled, *both* the URL
    match *and* an empty billing field are required before returning True.

    Args:
        driver: Selenium-compatible driver exposing ``current_url`` and
            (when ``check_billing_empty`` is True) ``find_elements``.
        check_billing_empty: If True, additionally require the billing
            address field to be missing or empty. Defaults to False —
            URL match alone is authoritative.

    Returns:
        bool: True when the driver is on the canonical payment page (and,
        when requested, also has an empty billing field). On unexpected
        errors reading the URL, returns True to stay on the conservative
        side (skipping a refill is the worse failure mode — double-charge).
    """
    try:
        from modules.cdp.driver import URL_PAYMENT  # noqa: PLC0415

        try:
            current = driver.current_url
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            # current_url briefly unavailable during page transition —
            # conservative: assume reloaded so we refill rather than skip.
            return True

        if not _payment_url_matches(current or "", URL_PAYMENT):
            return False

        if not check_billing_empty:
            return True

        # Opt-in legacy heuristic: URL matches AND billing field empty.
        from modules.cdp.driver import SEL_BILLING_ADDRESS  # noqa: PLC0415
        try:
            elements = driver.find_elements(SEL_BILLING_ADDRESS)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            return True
        return not elements or not elements[0].get_attribute("value")
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        return True


def refill_after_vbv_reload(driver, ctx, new_card) -> None:
    """Refill all form fields after a VBV cancel reload.

    When ``ctx.task`` is available, performs the complete purchase-path sequence:
    preflight → navigate → eGift → cart → guest → payment (with ``new_card``).
    This covers the case where the VBV cancel caused the browser to reload the
    page from the beginning, so every step must be re-executed in order.

    When ``ctx.task`` is ``None`` (legacy / partial-reload path), only billing
    and card fields are re-filled (original behaviour).

    Each step is logged at INFO level so the journey can be traced in logs.
    """
    if ctx.billing_profile is None:
        _logger.warning("refill_after_vbv_reload skipped: billing_profile missing")
        return
    # Phase 5A Task 2D: gate the entire reload chain behind the
    # CRITICAL_SECTION flag.  Page reload after VBV cancel is a fragile
    # window where any background biometric / hesitation delay would
    # risk the watchdog budget; flip the flag on the SM published into
    # the behaviour ContextVar so all delay sites see the active CS.
    sm = _get_current_sm()
    if sm is not None:
        sm.enter_critical_zone("page_reload")
    try:
        if ctx.task is not None:
            # Full refill: page was reloaded to the beginning after VBV cancel.
            _logger.info("[VBV-refill] step=preflight_geo_check")
            driver.preflight_geo_check()
            _logger.info("[VBV-refill] step=navigate_to_egift")
            driver.navigate_to_egift()
            _logger.info("[VBV-refill] step=fill_egift_form")
            driver.fill_egift_form(ctx.task, ctx.billing_profile)
            _logger.info("[VBV-refill] step=add_to_cart_and_checkout")
            driver.add_to_cart_and_checkout()
            _logger.info("[VBV-refill] step=select_guest_checkout")
            driver.select_guest_checkout(ctx.billing_profile.email)
            _logger.info("[VBV-refill] step=fill_payment_and_billing (new card)")
            driver.fill_payment_and_billing(new_card, ctx.billing_profile)
            _logger.info("[VBV-refill] full refill sequence complete")
        else:
            # Legacy / partial-reload fallback: fill billing + card fields only.
            driver.fill_billing(ctx.billing_profile)
            driver.fill_card_fields(new_card)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.warning("refill_after_vbv_reload failed: %s", _sanitize_error(exc))
    finally:
        if sm is not None:
            sm.exit_critical_zone()


def _record_fork_safe(branch: str) -> None:
    """Increment the monitor fork counter for *branch*, never propagating errors.

    Observability must never destabilise the FSM — any exception raised by
    the monitor sink (e.g. lock contention issue, import-time bug) is
    swallowed after a DEBUG log (Phase 4 audit [H3]).
    """
    try:
        monitor.record_fork(branch)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        _logger.debug("monitor.record_fork(%s) failed", branch, exc_info=True)


def handle_outcome(state, order_queue, worker_id: str = "default", ctx=None):
    """Determine the next action based on the current FSM state.

    Returns one of: "complete", "retry", "await_3ds", "abort_cycle", or
    ("retry_new_card", CardInfo) when a swap is available. ``ctx`` is an
    optional :class:`CycleContext` tracking swap counts across retries.
    """
    if state is None:
        _logger.warning(
            "[trace=%s] handle_outcome called with state=None for worker=%s: "
            "FSM was never transitioned — upstream anomaly (page-state detection failed "
            "or run_payment_step returned without a state transition). Returning 'retry'.",
            _get_trace_id(),
            worker_id,
        )
        return "retry"
    _swap = ctx.swap_count if ctx is not None else 0
    if state.name == "success":
        _logger.info(
            "[trace=%s] FORK=%s worker=%s swap=%d",
            _get_trace_id(), state.name, worker_id, _swap,
        )
        _record_fork_safe("success")
        return "complete"
    if state.name in ("declined", "vbv_cancelled"):
        _logger.info(
            "[trace=%s] FORK=%s worker=%s swap=%d",
            _get_trace_id(), state.name, worker_id, _swap,
        )
        _record_fork_safe(state.name)
        try:
            _alerting.send_alert(
                f"Card declined: worker={worker_id} state={state.name}"
            )
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            _logger.debug("alerting.send_alert (decline) failed", exc_info=True)
        if ctx is None:
            return "retry_new_card" if order_queue else "retry"
        next_card = _ctx_next_swap_card(ctx)
        if next_card is None:
            _record_fork_safe("abort_cycle")
            return "abort_cycle"
        ctx.swap_count += 1
        if state.name == "vbv_cancelled":
            try:
                driver = cdp._get_driver(worker_id)  # pylint: disable=protected-access
                url_before = driver.current_url if driver else None
                if is_payment_page_reloaded(driver):
                    refill_after_vbv_reload(driver, ctx, next_card)
                else:
                    url_after = driver.current_url if driver else None
                    if (url_before is not None and url_after is not None
                            and url_after != url_before):
                        _logger.warning(
                            "No-reload invariant violated: %s → %s (state=%s)",
                            _sanitize_error(url_before),
                            _sanitize_error(url_after),
                            state.name,
                        )
                        refill_after_vbv_reload(driver, ctx, next_card)
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
                _logger.warning("VBV reload refill failed: %s", _sanitize_error(exc))
        return ("retry_new_card", next_card)
    if state.name == "ui_lock":
        _logger.info(
            "[trace=%s] FORK=%s worker=%s swap=%d",
            _get_trace_id(), state.name, worker_id, _swap,
        )
        _record_fork_safe("ui_lock")
        return "retry"
    if state.name == "vbv_3ds":
        _logger.info(
            "[trace=%s] FORK=%s worker=%s swap=%d",
            _get_trace_id(), state.name, worker_id, _swap,
        )
        _record_fork_safe("vbv_3ds")
        try:
            driver = cdp._get_driver(worker_id)  # pylint: disable=protected-access
            result = driver.handle_vbv_challenge()
            if result in ("cancelled", "iframe_missing"):
                post_state = driver.detect_page_state()
                # If the 3DS iframe is still present after the cancel click
                # (post_state == "vbv_3ds"), keep waiting on the 3DS path
                # rather than redispatching: the upstream polling layer is
                # responsible for the next probe.  Unknown post_state →
                # legacy "vbv_cancelled" fallback.
                if post_state == "vbv_3ds":
                    return "await_3ds"
                next_state = (
                    post_state if post_state in _FSM_STATES else "vbv_cancelled"
                )
                return handle_outcome(
                    State(next_state), order_queue,
                    worker_id=worker_id, ctx=ctx,
                )
            elif result == "cdp_fail":
                # retry once on CDP/WebDriver failure
                result = driver.handle_vbv_challenge()
                if result in ("cancelled", "iframe_missing"):
                    post_state = driver.detect_page_state()
                    if post_state == "vbv_3ds":
                        return "await_3ds"
                    next_state = (
                        post_state if post_state in _FSM_STATES else "vbv_cancelled"
                    )
                    return handle_outcome(
                        State(next_state), order_queue,
                        worker_id=worker_id, ctx=ctx,
                    )
        except Exception as exc:
            _logger.warning(
                "[trace=%s] VBV challenge handling failed for worker=%s: %s",
                _get_trace_id(), worker_id, _sanitize_error(exc),
            )
        return "await_3ds"
    _logger.info(
        "[trace=%s] FORK=%s worker=%s swap=%d",
        _get_trace_id(), state.name, worker_id, _swap,
    )
    return "retry"


def run_cycle(task, zip_code=None, worker_id: str = "default", ctx=None, abort_check=None):
    """Run a full payment cycle for a WorkerTask.

    Initializes the FSM, executes one payment attempt, and returns the
    outcome action together with the final state and confirmed total.

    When *ctx* is supplied (a :class:`~modules.common.types.CycleContext`),
    the billing profile is locked for the entire cycle:

    * **First call** (``ctx.billing_profile is None``): billing is selected
      once via :func:`~modules.billing.main.select_profile` and stored in
      ``ctx.billing_profile``.
    * **Subsequent calls** with the same *ctx* (e.g. card-swap retries): the
      already-selected profile is reused — ``billing.select_profile`` is NOT
      called again.

    When *ctx* is ``None`` (default), a fresh :class:`CycleContext` is created
    internally and billing is selected once for this invocation (backward-
    compatible with existing callers that omit *ctx*).

    Args:
        task: WorkerTask containing the recipient, amount, and card data.
        zip_code: Optional zip code for billing profile selection.
        worker_id: Unique identifier for this worker.
        ctx: Optional :class:`~modules.common.types.CycleContext` for
            cross-retry billing lock.  If ``None``, a new context is created.
        abort_check: Optional ``() -> bool``; when it returns ``True`` the
            cycle aborts and returns ``"abort_cycle"`` (P1-5).

    Returns:
        A (action, state, total) tuple where action is one of:
        "complete" | "retry" | "retry_new_card" | "await_3ds" | "abort_cycle".

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out.
        RuntimeError: if no CDP driver has been registered.
    """
    import uuid as _uuid  # noqa: PLC0415
    from modules.common.types import CycleContext  # noqa: PLC0415

    task_id = getattr(task, "task_id", None)
    success = False
    if task_id is not None:
        if _get_idempotency_store().is_duplicate(task_id):
            _logger.warning(
                "[trace=%s] Duplicate task_id=%s detected; skipping.",
                _get_trace_id(),
                task_id,
            )
            return "complete", None, None

    # Create or receive CycleContext.
    if ctx is None:
        ctx = CycleContext(
            cycle_id=_uuid.uuid4().hex,
            worker_id=worker_id,
            zip_code=zip_code,
        )
    if ctx.task is None:
        ctx.task = task

    # Select billing profile once per ctx — reuse on card-swap retries.
    if ctx.billing_profile is None:
        # Prefer zip_code from ctx if set, otherwise use the argument.
        effective_zip = ctx.zip_code if ctx.zip_code is not None else zip_code
        ctx.billing_profile = _select_profile_with_audit(
            effective_zip,
            worker_id=worker_id,
            task_id=task_id,
            include_worker_id=True,
        )

    try:
        if not _ENABLE_RETRY_LOOP:
            if abort_check is not None and abort_check():
                return "abort_cycle", None, None
            initialize_cycle(worker_id)
            state, total = run_payment_step(
                task, zip_code, worker_id=worker_id, _profile=ctx.billing_profile,
            )
            action = handle_outcome(state, task.order_queue, worker_id=worker_id, ctx=ctx)
            if action == "complete":
                success = True
                _record_autoscaler_success(worker_id)
                # Ngã rẽ 2: Screenshot + Blur + Telegram (Blueprint §6)
                _notify_success(task, worker_id, total, ctx=ctx)
                if task_id is not None:
                    _get_idempotency_store().mark_completed(task_id)
            else:
                _record_autoscaler_failure(worker_id)
            return action, state, total

        # P0-2 — Retry loop: wrap run_payment_step + handle_outcome so that
        # declined/retry_new_card outcomes are consumed instead of silently dropped.
        current_card = task.primary_card
        retry_count = 0
        ui_lock_retry_count = 0  # P0-4 — separate cap for UI lock focus-shift retries
        # Cap: len(order_queue) card-swap slots + 2 buffer for ui_lock retries.
        max_iters = len(ctx.task.order_queue) + 2
        # action is str for simple outcomes or (str, CardInfo) for retry_new_card.
        action: str | tuple = "abort_cycle"
        state = None
        total = None

        for _loop_iter in range(max_iters):
            if abort_check is not None and abort_check():
                action = "abort_cycle"
                break
            initialize_cycle(worker_id)
            # Build an effective task that carries the current (possibly swapped) card.
            effective_task = (
                dataclasses.replace(task, primary_card=current_card)
                if current_card is not task.primary_card
                else task
            )
            state, total = run_payment_step(
                effective_task, zip_code, worker_id=worker_id, _profile=ctx.billing_profile,
            )

            # P0-4 — UI lock auto-recovery (Blueprint §6 Ngã rẽ 1).
            # When the page is UI-locked, call handle_ui_lock_focus_shift to click a
            # neutral point and re-submit, then re-detect to see if the lock cleared.
            # Cap at _MAX_UI_LOCK_RETRIES (default 2) per card to avoid infinite loops.
            if (_ENABLE_RETRY_UI_LOCK
                    and state is not None
                    and state.name == "ui_lock"
                    and ui_lock_retry_count < _MAX_UI_LOCK_RETRIES):
                ui_lock_retry_count += 1
                try:
                    monitor.record_ui_lock_retry()
                except Exception as _met_exc:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.debug(
                        "[trace=%s] monitor.record_ui_lock_retry failed for "
                        "worker=%s: %s",
                        _get_trace_id(), worker_id, _sanitize_error(_met_exc),
                    )
                _logger.info(
                    "[trace=%s] UI lock detected for worker=%s — calling "
                    "handle_ui_lock_focus_shift (attempt %d/%d)",
                    _get_trace_id(), worker_id, ui_lock_retry_count, _MAX_UI_LOCK_RETRIES,
                )
                try:
                    cdp.handle_ui_lock_focus_shift(worker_id)
                except Exception as _uil_exc:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.warning(
                        "[trace=%s] handle_ui_lock_focus_shift failed for worker=%s: %s",
                        _get_trace_id(), worker_id, _sanitize_error(_uil_exc),
                    )
                # Re-detect page state: if UI lock cleared, transition FSM so that
                # handle_outcome below receives the updated state rather than ui_lock.
                _ui_lock_cleared = False
                try:
                    _new_page_state = _settle_busy_page_state(
                        lambda: cdp.detect_page_state(worker_id)
                    )
                    if _new_page_state in _FSM_STATES and _new_page_state != "ui_lock":
                        state = fsm.transition_for_worker(worker_id, _new_page_state)
                        _ui_lock_cleared = True
                        try:
                            monitor.record_ui_lock_recovered()
                        except Exception as _met_exc:  # noqa: BLE001  # pylint: disable=broad-except
                            _logger.debug(
                                "[trace=%s] monitor.record_ui_lock_recovered "
                                "failed for worker=%s: %s",
                                _get_trace_id(), worker_id, _sanitize_error(_met_exc),
                            )
                    elif _new_page_state == _UI_BUSY_STATE:
                        _logger.info(
                            "[trace=%s] worker=%s still loading after focus-shift; "
                            "deferring ui_lock recovery until a stable state appears",
                            _get_trace_id(), worker_id,
                        )
                except Exception as _det_exc:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.warning(
                        "[trace=%s] detect_page_state retry after ui_lock failed "
                        "for worker=%s: %s",
                        _get_trace_id(), worker_id, _sanitize_error(_det_exc),
                    )
                # Exhaustion: the last permitted retry did not clear the lock.
                # This runs at most once per card: we only enter the outer `if`
                # while count < _MAX_UI_LOCK_RETRIES, and count was just
                # incremented to exactly _MAX_UI_LOCK_RETRIES on this attempt,
                # so subsequent loop iterations skip the outer branch entirely.
                if (not _ui_lock_cleared
                        and ui_lock_retry_count == _MAX_UI_LOCK_RETRIES):
                    try:
                        monitor.record_ui_lock_exhausted()
                    except Exception as _met_exc:  # noqa: BLE001  # pylint: disable=broad-except
                        _logger.debug(
                            "[trace=%s] monitor.record_ui_lock_exhausted "
                            "failed for worker=%s: %s",
                            _get_trace_id(), worker_id, _sanitize_error(_met_exc),
                        )
                    _logger.warning(
                        "[trace=%s] UI lock retry budget exhausted for worker=%s "
                        "after %d attempts",
                        _get_trace_id(), worker_id, _MAX_UI_LOCK_RETRIES,
                    )

            action = handle_outcome(state, task.order_queue, worker_id=worker_id, ctx=ctx)

            _action_key = action[0] if isinstance(action, tuple) else action

            # P1-2 — Clear/refill after "Thank you" popup.
            # When the payment was successful and more cards remain in the queue,
            # verify the thank-you popup via text/URL match then clear card fields
            # and refill with the next card so the form is ready for the caller.
            # ctx is always set by this point (created at top of run_cycle when None).
            if _ENABLE_CLEAR_REFILL_AFTER_POPUP and _action_key == "complete":
                _next_refill_card = _ctx_next_swap_card(ctx)

                if _next_refill_card is not None:
                    try:
                        _ty_detected = cdp.detect_popup_thank_you(worker_id)
                    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
                        _ty_detected = False
                    if _ty_detected:
                        try:
                            _ty_driver = cdp._get_driver(worker_id)  # pylint: disable=protected-access
                            if _ty_driver is not None:
                                clear_refill_after_thank_you_popup(_ty_driver, _next_refill_card)
                        except Exception as _ty_exc:  # noqa: BLE001  # pylint: disable=broad-except
                            _logger.warning(
                                "[trace=%s] clear_refill_after_thank_you_popup failed "
                                "for worker=%s: %s",
                                _get_trace_id(), worker_id, _sanitize_error(_ty_exc),
                            )

            if _action_key in ("complete", "abort_cycle", "await_3ds"):
                break

            if isinstance(action, tuple) and _action_key == "retry_new_card":
                _, new_card = action
                # CDP card-swap: clear existing card fields then fill with new card.
                # P1-4: a CDPError from clear_card_fields_cdp means the field may
                # still hold stale card data — abort the cycle instead of resubmitting
                # to avoid a double-charge.
                try:
                    _swap_driver = cdp._get_driver(worker_id)  # pylint: disable=protected-access
                    if _swap_driver is not None:
                        _swap_driver.clear_card_fields_cdp()
                        _swap_driver.fill_card_fields(new_card)
                except CDPError as _cdp_exc:
                    _logger.error(
                        "[trace=%s] CDP card-swap clear failed for worker=%s: %s "
                        "— aborting cycle to avoid double-charge",
                        _get_trace_id(), worker_id, _sanitize_error(_cdp_exc),
                    )
                    action = "abort_cycle"
                    break
                except Exception as _swap_exc:  # noqa: BLE001  # pylint: disable=broad-except
                    _logger.warning(
                        "[trace=%s] Card swap CDP prep failed for worker=%s: %s",
                        _get_trace_id(), worker_id, _sanitize_error(_swap_exc),
                    )
                current_card = new_card
                retry_count = 0  # reset general retry counter after a card swap
                ui_lock_retry_count = 0  # P0-4 — reset ui_lock counter after card swap
            elif action == "retry_new_card":
                # Legacy path (ctx=None): no card info available — abort.
                action = "abort_cycle"
                break
            elif action == "retry":
                retry_count += 1
                if retry_count >= 2:
                    action = "abort_cycle"
                    break
            # Unknown actions: continue loop (guards against future outcome additions).
        else:
            # Loop cap exhausted without a terminal break — treat as abort.
            action = "abort_cycle"

        if action == "complete":
            success = True
            _record_autoscaler_success(worker_id)
            # Ngã rẽ 2: Screenshot + Blur + Telegram (Blueprint §6)
            _notify_success(task, worker_id, total, ctx=ctx)
            if task_id is not None:
                _get_idempotency_store().mark_completed(task_id)
        else:
            _record_autoscaler_failure(worker_id)
        return action, state, total
    except SessionFlaggedError as exc:
        _logger.error(
            "[trace=%s] worker=%s, task_id=%s SessionFlaggedError: %s",
            _get_trace_id(), worker_id, task_id, exc
        )
        _record_autoscaler_failure(worker_id)
        raise
    except Exception as exc:
        _record_autoscaler_failure(worker_id)
        _logger.error(
            "[trace=%s] Cycle failed for worker=%s, task_id=%s: %s",
            _get_trace_id(),
            worker_id,
            task_id,
            _sanitize_error(exc),
        )
        raise
    finally:
        _logger.info(
            "[trace=%s] worker=%s cycle_result=%s consecutive_failures=%d",
            _get_trace_id(), worker_id, "success" if success else "failure",
            _get_consecutive_failures(worker_id)
        )
        if task_id is not None:
            _get_idempotency_store().release_inflight(task_id)
        # Clean up CDP driver to prevent registry memory leak (GAP-CDP-01).
        cdp.unregister_driver(worker_id)
        # Clean up FSM state to prevent registry memory leak (HIGH-02 / FSM-002).
        fsm.cleanup_worker(worker_id)
