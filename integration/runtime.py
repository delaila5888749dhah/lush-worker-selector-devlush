"""Runtime orchestrator for worker scaling and monitoring."""
import atexit
from datetime import datetime, timezone
import logging
import os
import random
import signal
import threading
import time
import uuid
import zlib
from modules.behavior import main as behavior
from modules.fsm import main as fsm
from modules.monitor import main as monitor
from modules.observability import alerting
from modules.observability import metrics_exporter
from modules.observability import log_sink
from modules.rollout.autoscaler import get_autoscaler
from modules.rollout import autoscaler as _autoscaler_module
from modules.rollout import main as rollout
from modules.delay.config import DelayConfigError, validate_config
from modules.delay.wrapper import wrap as _behavior_wrap
from modules.delay.persona import PersonaProfile
from modules.billing import main as billing
from modules.cdp import main as cdp
from modules.cdp.proxy import get_default_pool
from modules.common.exceptions import CycleExhaustedError
from integration.cycle_outcome import CycleDidNotCompleteError
from modules.common.sanitize import sanitize_error as _canonical_sanitize_error  # INV-PII-UNIFIED-01
from modules.common.thresholds import ERROR_RATE_THRESHOLD, MAX_RESTARTS_PER_HOUR
_logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when runtime startup configuration is invalid."""


ALLOWED_STATES = {"INIT", "RUNNING", "STOPPING", "STOPPED"}
ALLOWED_WORKER_STATES = {"IDLE", "IN_CYCLE", "CRITICAL_SECTION", "SAFE_POINT"}
_VALID_TRANSITIONS = {
    "IDLE": {"IN_CYCLE"},
    "IN_CYCLE": {"CRITICAL_SECTION", "SAFE_POINT", "IDLE"},
    "CRITICAL_SECTION": {"IN_CYCLE"},
    "SAFE_POINT": {"IN_CYCLE"},
}
_lock = threading.Lock()
_state = "INIT"
_workers: dict[str, threading.Thread] = {}
_worker_states: dict[str, str] = {}
_worker_counter = 0
_loop_thread = None
_trace_id = None
_trace_lock = threading.Lock()
_NO_TRACE = "no-trace"
_DEFAULT_LOOP_INTERVAL = 10
_MIN_LOOP_INTERVAL = 0.1
_WORKER_TIMEOUT = 30
WORKER_TIMEOUT = _WORKER_TIMEOUT
_MAX_CONSECUTIVE_ROLLBACKS = 3
_CIRCUIT_BREAKER_PAUSE = 300
_consecutive_rollbacks = 0
_pending_restarts = 0
_stop_requests = set()
_behavior_delay_enabled = True
# RT-STAGGER-FLAG — stagger is decoupled from behavior delay so tests may
# disable behavior wrapping while still exercising the 12–25s launch gap
# (Blueprint §1).  Default mirrors _behavior_delay_enabled (True in prod,
# reset() flips to False for test hygiene).
_stagger_enabled = True
_stop_event = threading.Event()
_MAX_RESTART_BACKOFF = 60
_restart_delay: float = 0
_loop_error_count = 0
_MAX_LOOP_ERRORS = 10
# ── Billing-specific circuit breaker ──────────────────────────────
_BILLING_CB_THRESHOLD = max(1, int(os.environ.get("BILLING_CB_THRESHOLD", "3")))
_BILLING_CB_PAUSE = max(1, int(os.environ.get("BILLING_CB_PAUSE", "120")))
_consecutive_billing_failures = 0
_billing_throttled_until: float = 0.0
_log_sink_error_count = 0  # incremented on log_sink.emit() failure
# Dedicated lock for _log_sink_error_count — used consistently across all
# access paths (_log_event() write, get_status() read, reset() clear). Must
# not nest under _lock because _log_event() may be invoked while _lock is
# already held (e.g. billing circuit-breaker path); using a separate lock
# avoids self-deadlock on the non-reentrant _lock.
_log_sink_error_lock = threading.Lock()

# ── C1 — Stagger-start between worker launches (Blueprint §1, §8.4) ──
# Blueprint §1 requires random.uniform(12, 25) seconds between consecutive
# worker launches to avoid synchronised access patterns.  Stagger is
# INDEPENDENT from behavior delay (§8.4) — tracked here separately.
_STAGGER_RANGE = (12.0, 25.0)
_last_worker_launch_ts: float = 0.0
_stagger_lock = threading.Lock()


def _stagger_sleep_before_launch(rng=None) -> float:
    """Sleep between worker launches per Blueprint §1.

    Gated solely by ``_stagger_enabled`` (RT-STAGGER-FLAG).  When the flag is
    off the helper returns immediately.  Otherwise the first launch is
    immediate (no stagger) and subsequent launches wait so that the delta
    since the previous launch falls within ``_STAGGER_RANGE``.  Uses the
    interruptible ``_stop_event`` so that ``stop()`` can unblock the sleep.

    Returns the number of seconds slept (0.0 when no sleep was required).
    """
    global _last_worker_launch_ts
    with _lock:
        if not _stagger_enabled:
            return 0.0
    rng = rng or random
    with _stagger_lock:
        now = time.monotonic()
        target = rng.uniform(*_STAGGER_RANGE)
        if _last_worker_launch_ts > 0:
            elapsed = now - _last_worker_launch_ts
            wait_s = target - elapsed
        else:
            wait_s = 0.0
    if wait_s > 0:
        # Interruptible by stop_event so graceful shutdown preempts the wait.
        _stop_event.wait(timeout=wait_s)
        slept = wait_s
    else:
        slept = 0.0
    with _stagger_lock:
        _last_worker_launch_ts = time.monotonic()
    return slept


def _is_billing_throttled() -> bool:
    """Return True if billing circuit breaker is active (must hold _lock)."""
    return time.monotonic() < _billing_throttled_until
def _should_stop_worker(worker_id):
    t = _workers.get(worker_id)
    if t is not None and t is not threading.current_thread():
        return True
    return worker_id not in _workers or worker_id in _stop_requests or _state == "STOPPING"
def _log_event(worker_id, state, action, metrics=None) -> None:
    global _log_sink_error_count
    with _trace_lock:
        tid = _trace_id or _NO_TRACE
    _logger.info(
        "%s | %s | %s | %s | %s | %s",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        worker_id,
        tid,
        state,
        action,
        metrics or "",
    )
    try:
        log_sink.emit({
            "ts": time.time(),
            "source": worker_id,
            "level": state,
            "event": action,
            "data": metrics if isinstance(metrics, dict) else {},
        })
    except Exception as sink_exc:  # pylint: disable=broad-except  # prevent runtime crash on sink failure
        # Use a dedicated lock to avoid re-entering _lock — _log_event() may be
        # invoked while _lock is already held (e.g. billing circuit-breaker
        # path), so nesting on the non-reentrant _lock would self-deadlock.
        with _log_sink_error_lock:
            _log_sink_error_count += 1
            sink_fail_count = _log_sink_error_count
        _logger.warning(
            "log_sink.emit() failed (total sink failures: %d): %s",
            sink_fail_count,
            _sanitize_error(sink_exc),
        )
def _sanitize_error(exc: Exception) -> str:
    """Redact PII from exception messages before logging.

    Delegates to the canonical sanitiser (INV-PII-UNIFIED-01).
    """
    return _canonical_sanitize_error(str(exc))
def _safe_sleep(interval):
    try:
        _stop_event.wait(timeout=float(interval))
    except (TypeError, ValueError):
        _stop_event.wait(timeout=_MIN_LOOP_INTERVAL)
def _ensure_rollout_configured():
    if not rollout.is_configured():
        rollout.configure(monitor.check_rollback_needed, monitor.save_baseline)
def _transition_worker_state_locked(worker_id, new_state):
    """Transition worker state while holding _lock. Raises ValueError on invalid transition."""
    current = _worker_states.get(worker_id)
    if current is None:
        raise ValueError(f"Worker {worker_id} has no tracked state")
    if new_state not in _VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid worker state transition: {current} -> {new_state} for {worker_id}")
    _worker_states[worker_id] = new_state
def _increment_pending_restarts_locked(worker_id):
    """Increment _pending_restarts for a failed worker, capped at current worker count.

    Must be called while holding _lock.  No-op if the worker is not registered
    or already has a stop request (natural exit, not a failure restart).
    """
    global _pending_restarts
    if worker_id in _workers and worker_id not in _stop_requests:
        _pending_restarts = min(_pending_restarts + 1, max(1, len(_workers)))
def _worker_fn(worker_id, task_fn, persona):
    global _restart_delay, _consecutive_billing_failures, _billing_throttled_until
    with _lock:
        delay_enabled = _behavior_delay_enabled
    if delay_enabled and persona is not None:
        wrapped_task = _behavior_wrap(task_fn, persona, stop_event=_stop_event)
    else:
        wrapped_task = task_fn
    try:
        persona_ctx: dict = {}
        if persona is not None:
            persona_ctx = {
                "persona_seed": persona._seed,
                "persona_type": persona.persona_type,
            }
        _log_event(worker_id, "running", "start", persona_ctx)
        while True:
            with _lock:
                if _should_stop_worker(worker_id):
                    break
                billing_paused = _is_billing_throttled()
                pause_remaining = (_billing_throttled_until - time.monotonic()) if billing_paused else 0
            if pause_remaining > 0:
                _log_event(worker_id, "throttled", "billing_cb_wait", {"pause_seconds": round(pause_remaining, 1)})
                deadline = time.monotonic() + pause_remaining
                while time.monotonic() < deadline:
                    with _lock:
                        if _should_stop_worker(worker_id):
                            break
                    _safe_sleep(min(1.0, max(0, deadline - time.monotonic())))
                # Request a billing pool reload so profiles added during the
                # pause window are picked up before workers retry.
                try:
                    billing.request_pool_reload()
                    _logger.info(
                        "Billing CB pause ended for worker %s; "
                        "pool reload requested.",
                        worker_id,
                    )
                except Exception:  # pylint: disable=broad-except
                    _logger.warning(
                        "Failed to request billing pool reload after CB pause.",
                        exc_info=True,
                    )
                continue
            with _lock:
                if _should_stop_worker(worker_id):
                    break
                _transition_worker_state_locked(worker_id, "IN_CYCLE")
            try:
                wrapped_task(worker_id)
                persona_type_tag = persona.persona_type if persona is not None else None
                try:
                    monitor.record_success(persona_type=persona_type_tag)
                except Exception:
                    _logger.warning("monitor.record_success() failed for %s", worker_id, exc_info=True)
                get_autoscaler().record_success(worker_id)
                with _lock:
                    _restart_delay = 0
                    _consecutive_billing_failures = 0
            except CycleExhaustedError as exc:
                persona_type_tag = persona.persona_type if persona is not None else None
                try:
                    monitor.record_error(persona_type=persona_type_tag)
                except Exception:
                    _logger.warning("monitor.record_error() failed for %s", worker_id, exc_info=True)
                get_autoscaler().record_failure(worker_id)
                with _lock:
                    _consecutive_billing_failures += 1
                    if _consecutive_billing_failures >= _BILLING_CB_THRESHOLD:
                        if not _is_billing_throttled():  # Only arm CB if not already active
                            pause_dur = int(_BILLING_CB_PAUSE)
                            _billing_throttled_until = time.monotonic() + pause_dur
                            _log_event(
                                worker_id,
                                "critical",
                                "billing_cb_triggered",
                                {
                                    "threshold_reached": True,
                                    "pause_seconds": pause_dur,
                                },
                            )
                            _logger.error("Billing circuit breaker triggered. Pausing billing for %ds.", pause_dur)
                        # Always reset counter to prevent unbounded growth
                        _consecutive_billing_failures = 0
                    _increment_pending_restarts_locked(worker_id)
                err_data: dict = {"error": _sanitize_error(exc)}
                if persona_type_tag is not None:
                    err_data["persona_type"] = persona_type_tag
                    err_data["persona_seed"] = persona._seed
                _log_event(worker_id, "error", "billing_failure", err_data)
                break
            except CycleDidNotCompleteError as exc:
                # P0: run_cycle() returned a non-complete action (abort_cycle,
                # await_3ds, retry, retry_new_card). Record as error so
                # success_count is not contaminated, but keep the worker
                # alive — this is an expected per-cycle outcome, not a crash.
                persona_type_tag = persona.persona_type if persona is not None else None
                try:
                    monitor.record_error(persona_type=persona_type_tag)
                except Exception:
                    _logger.warning("monitor.record_error() failed for %s", worker_id, exc_info=True)
                # run_cycle() already accounted autoscaler failure for non-complete
                # outcomes — do NOT double-count here.
                # Non-complete cycle is not a billing failure — it breaks the
                # billing circuit-breaker consecutive streak. It is also a
                # healthy runtime cycle, so clear stale crash restart backoff.
                with _lock:
                    _restart_delay = 0
                    _consecutive_billing_failures = 0
                err_data: dict = {
                    "error_type": "CycleDidNotCompleteError",
                    "action": exc.action,
                }
                if getattr(exc, "reason", ""):
                    err_data["reason_present"] = True
                if persona_type_tag is not None:
                    err_data["persona_type"] = persona_type_tag
                    err_data["persona_seed"] = persona._seed
                _log_event(worker_id, "error", "cycle_not_complete", err_data)
            except Exception as exc:
                persona_type_tag = persona.persona_type if persona is not None else None
                try:
                    monitor.record_error(persona_type=persona_type_tag)
                except Exception:
                    _logger.warning("monitor.record_error() failed for %s", worker_id, exc_info=True)
                get_autoscaler().record_failure(worker_id)
                with _lock:
                    _increment_pending_restarts_locked(worker_id)
                err_data: dict = {"error": _sanitize_error(exc)}
                if persona_type_tag is not None:
                    err_data["persona_type"] = persona_type_tag
                    err_data["persona_seed"] = persona._seed
                _log_event(worker_id, "error", "task_failed", err_data)
                # T-G1: route worker crash to alerting so Telegram (and any
                # other registered handler) is notified.
                try:
                    alerting.send_alert(
                        f"Worker crashed: worker={worker_id} error={_sanitize_error(exc)}"
                    )
                except Exception:  # pylint: disable=broad-except
                    _logger.debug("alerting.send_alert (worker crash) failed", exc_info=True)
                break
            with _lock:
                current_state = _worker_states.get(worker_id)
                if current_state is not None:
                    if current_state in ("SAFE_POINT", "CRITICAL_SECTION"):
                        _transition_worker_state_locked(worker_id, "IN_CYCLE")
                        current_state = "IN_CYCLE"
                    if current_state == "IN_CYCLE":
                        _transition_worker_state_locked(worker_id, "IDLE")
                # Safe-point check: break early if stop was requested during cycle
                if _should_stop_worker(worker_id):
                    break
    except Exception as exc:
        _logger.error("Unexpected error in worker %s: %s", worker_id, exc, exc_info=True)
        try:
            alerting.send_alert(
                f"Worker crashed: worker={worker_id} error={_sanitize_error(exc)}"
            )
        except Exception:  # pylint: disable=broad-except
            _logger.debug("alerting.send_alert (outer crash) failed", exc_info=True)
    finally:
        with _lock:
            # Only remove if this thread owns the worker entry (prevents
            # stale threads from removing re-registered workers after reset)
            if _workers.get(worker_id) is threading.current_thread():
                _stop_requests.discard(worker_id)
                _workers.pop(worker_id, None)
                _worker_states.pop(worker_id, None)
        # Release proxy regardless of how this thread exits.
        # ProxyPool.release() is idempotent via pop(default=None) — safe to call
        # even if stop_worker() already released it.
        try:
            get_default_pool().release(worker_id)
        except Exception:  # pylint: disable=broad-except
            _logger.warning(
                "Failed to release proxy for worker %s during thread exit cleanup",
                worker_id, exc_info=True,
            )
        _log_event(worker_id, "stopped", "stop")
def start_worker(task_fn):
    """Start a new worker thread running *task_fn*. Returns the worker id."""
    global _worker_counter, _restart_delay
    # Compute backoff delay outside lock to avoid blocking other threads
    with _lock:
        delay = _restart_delay if _pending_restarts > 0 else 0
    if delay > 0:
        _safe_sleep(delay)
    with _lock:
        _worker_counter += 1
        wid = f"worker-{_worker_counter}"
        # Exponential backoff: increase delay for next restart, capped
        if _pending_restarts > 0:
            _restart_delay = min(_MAX_RESTART_BACKOFF, max(1, _restart_delay * 2) if _restart_delay > 0 else 1)
        # Generate a deterministic persona seed from the worker id
        persona_seed = zlib.crc32(wid.encode()) & 0xFFFFFFFF
        persona = PersonaProfile(persona_seed)
        t = threading.Thread(target=_worker_fn, args=(wid, task_fn, persona), daemon=False)
        _workers[wid] = t
        _worker_states[wid] = "IDLE"
    try:
        proxy = get_default_pool().acquire(wid)
        if proxy is None:
            _logger.warning("No proxy available for worker %s — running without proxy", wid)
        else:
            _logger.debug("Assigned proxy for worker %s", wid)
    except Exception:  # pylint: disable=broad-except
        _logger.warning(
            "Failed to acquire proxy from pool for worker %s — continuing without proxy",
            wid,
            exc_info=True,
        )
    try:
        t.start()
    except (RuntimeError, OSError):
        with _lock:
            _workers.pop(wid, None)
            _worker_states.pop(wid, None)
        try:
            get_default_pool().release(wid)
        except Exception:  # pylint: disable=broad-except  # proxy release must not suppress original exception
            _logger.warning("Failed to release proxy for worker %s after thread start failure", wid, exc_info=True)
        raise
    return wid
def stop_worker(worker_id, timeout=None):
    """Remove a worker from the active set and join its thread.

    Respects worker execution boundaries:
    - IDLE / SAFE_POINT: stop immediately (current behaviour).
    - IN_CYCLE: mark for stop; _worker_fn exits at the next safe point.
    - CRITICAL_SECTION: mark for stop; _worker_fn completes the critical
      operation then exits at the next safe point.  The thread join waits
      for the worker to finish the CS naturally.
    """
    timeout = _WORKER_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    with _lock:
        thread = _workers.get(worker_id)
        if thread is None:
            return False
        worker_state = _worker_states.get(worker_id)
        _stop_requests.add(worker_id)
    if worker_state == "CRITICAL_SECTION":
        _log_event(worker_id, "stopping", "awaiting_critical_section")
    if thread is threading.current_thread():
        raise RuntimeError("cannot join current thread")
    remaining = max(0, deadline - time.monotonic())
    if thread.ident is None:
        # Thread not yet started; _worker_fn will self-cleanup via _should_stop_worker.
        _logger.debug("join() on not-yet-started thread for %s; will self-cleanup via _worker_fn", worker_id)
    else:
        try:
            thread.join(timeout=remaining)
        except RuntimeError as exc:
            _logger.warning("RuntimeError joining worker %s: %s", worker_id, exc, exc_info=True)
            return False
    if thread.is_alive():
        _logger.warning("Worker %s did not stop within timeout", worker_id)
        # Do NOT remove from registry — the thread is still running and may
        # call set_worker_state().  Keep stop_request so worker exits at its
        # next safe point.  _worker_fn's finally block handles cleanup when
        # the thread eventually exits.
        return False
    with _lock:
        _stop_requests.discard(worker_id)
        _workers.pop(worker_id, None)
        _worker_states.pop(worker_id, None)
    _log_event(worker_id, "stopped", "stop_requested")
    try:
        get_default_pool().release(worker_id)
    except Exception:  # pylint: disable=broad-except
        _logger.warning("Failed to release proxy for worker %s", worker_id, exc_info=True)
    return True
def get_active_workers() -> list[str]:
    """Return a list of active worker ids."""
    with _lock:
        return list(_workers.keys())
def set_worker_state(worker_id, new_state):
    """Set the execution state of a worker with validated transitions.

    Raises ValueError if worker_id is not registered or the transition is invalid.
    """
    if new_state not in ALLOWED_WORKER_STATES:
        raise ValueError(f"Invalid worker state: {new_state}")
    with _lock:
        if worker_id not in _workers:
            raise ValueError(f"Worker {worker_id} not registered in _workers")
        _transition_worker_state_locked(worker_id, new_state)
def get_worker_state(worker_id):
    """Return the current execution state of a worker.

    Raises ValueError if worker_id is not tracked.
    """
    with _lock:
        state = _worker_states.get(worker_id)
        if state is None:
            raise ValueError(f"Worker {worker_id} has no tracked state")
        return state
def get_all_worker_states():
    """Return a snapshot dict of all worker execution states."""
    with _lock:
        return dict(_worker_states)
def _is_safe_locked():
    """Check worker safety while _lock is already held.

    Returns True only when every registered worker is IDLE or SAFE_POINT.
    Missing state entries → unsafe.  No workers → safe (vacuous truth).
    """
    for wid in _workers:
        ws = _worker_states.get(wid)
        if ws is None or ws not in ("IDLE", "SAFE_POINT"):
            return False
    return True
def is_safe_to_control():
    """Return True only when all tracked workers are IDLE or SAFE_POINT.

    Missing state entries are treated as UNSAFE.
    Returns True when there are no workers (vacuous truth for empty set).
    """
    with _lock:
        return _is_safe_locked()
def _apply_scale(target_count, task_fn):
    global _pending_restarts
    # Defensive cap clamp: rollout decisions should never emit a target above
    # SCALE_STEPS[-1] (== MAX_WORKER_COUNT), but clamp here as a belt-and-
    # suspenders guard so a drift bug upstream can't scale us past the cap.
    cap = rollout.SCALE_STEPS[-1]
    if target_count > cap:
        _logger.warning(
            "clamp target %d -> cap %d (SCALE_STEPS[-1])", target_count, cap,
        )
        _log_event(
            "runtime", "warning", "scale_target_clamped",
            {"requested": target_count, "cap": cap},
        )
        target_count = cap
    # Concurrency invariant for the worker-count comparisons below:
    # all mutations of ``_workers`` (start_worker / stop_worker / dict
    # assignment) happen under ``_lock``. We take a snapshot of the keys
    # under ``_lock`` into ``current_ids``; ``current_count`` and every
    # subsequent ``len()``/iteration in this function operate on that local
    # immutable list, never on ``_workers`` directly, so they are race-free
    # by construction regardless of the caller.
    #
    # In production the sole caller is the single-threaded ``_runtime_loop``,
    # so target_count itself is also stable across the call. Tests
    # (e.g. ``tests/test_runtime.py``, ``tests/test_phase1_runtime_bringup.py``)
    # invoke ``_apply_scale`` directly; they own concurrency in their own
    # setup and the snapshot above keeps the worker-count reads correct
    # there as well.
    #
    # The cap clamp above is an independent safeguard that bounds
    # ``target_count`` against ``SCALE_STEPS[-1]``; it does not protect the
    # worker-count read — that protection comes from the snapshot below.
    with _lock: current_ids = list(_workers.keys())
    current_count = len(current_ids)
    if target_count > current_count:
        with _lock:
            restarted = min(_pending_restarts, target_count - current_count); _pending_restarts -= restarted
            stagger_enabled = _stagger_enabled
        for i in range(target_count - current_count):
            # C1 — Blueprint §1: stagger launches 12–25s apart.  The first
            # launch in the process lifetime is not delayed (stagger is a
            # gap-between-launches contract).  Sleep is interruptible by
            # _stop_event so shutdown preempts it.  Gated by the independent
            # _stagger_enabled flag (RT-STAGGER-FLAG) — tests disable it via
            # set_stagger_enabled(False) to keep runs fast without having to
            # also disable behavior wrapping.
            if stagger_enabled:
                _stagger_sleep_before_launch()
                if _stop_event.is_set():
                    break
            start_worker(task_fn)
            if i < restarted: monitor.record_restart()
        _log_event("runtime", "scaling", "scale_up", {"from": current_count, "to": target_count})
    elif target_count < current_count:
        for wid in current_ids[target_count:]:
            stop_worker(wid, timeout=5)
        with _lock: _pending_restarts = 0
        _log_event("runtime", "scaling", "scale_down", {"from": current_count, "to": target_count})
def _runtime_loop(task_fn, interval):
    global _consecutive_rollbacks, _loop_error_count
    while True:
        with _lock:
            if _state != "RUNNING":
                break
        try:
            try:
                metrics = monitor.get_metrics()
            except Exception as exc:
                _log_event("runtime", "warning", "metrics_unavailable_scaling_deferred", {"error": _sanitize_error(exc)})
                _logger.warning("Metrics unavailable; scaling decision deferred for this tick")
                _safe_sleep(interval)
                continue
            metrics_exporter.export_metrics(metrics)
            _alerts = alerting.evaluate_alerts(metrics)
            for _alert_msg in _alerts:
                alerting.send_alert(_alert_msg)
            step_index = rollout.get_current_step_index()
            max_index = len(rollout.SCALE_STEPS) - 1
            decision, decision_reasons = behavior.evaluate(metrics, step_index, max_index)
            if decision == behavior.HOLD:
                target = rollout.get_current_workers()
                action = "hold"
            else:
                # SCALE_UP or SCALE_DOWN requires a worker count change.
                # Check safety BEFORE mutating rollout state so that
                # _current_step_index never drifts from the actual worker
                # count when scaling is deferred.
                with _lock:
                    current_count = len(_workers)
                    workers_safe = _is_safe_locked()
                if not workers_safe:
                    _log_event("runtime", "scaling_deferred", "unsafe_state",
                               {"target": decision, "current": current_count})
                    target = current_count
                    action = "hold_deferred"
                elif decision == behavior.SCALE_DOWN:
                    target = rollout.force_rollback(reason="; ".join(decision_reasons))
                    action = "rollback"
                else:
                    target, action, _ = rollout.try_scale_up()
            with _lock:
                if action == "rollback":
                    _consecutive_rollbacks += 1
                    if _consecutive_rollbacks >= _MAX_CONSECUTIVE_ROLLBACKS:
                        _log_event("runtime", "critical", "circuit_breaker_triggered", {"count": _consecutive_rollbacks})
                        _logger.error("Circuit breaker: %d consecutive rollbacks. Halting scale-up for %ds.", _consecutive_rollbacks, _CIRCUIT_BREAKER_PAUSE)
                        try:
                            alerting.send_alert(
                                f"CB triggered: {_consecutive_rollbacks} consecutive rollbacks; "
                                f"pausing scale-up for {_CIRCUIT_BREAKER_PAUSE}s"
                            )
                        except Exception:  # pylint: disable=broad-except
                            _logger.debug("alerting.send_alert (CB) failed", exc_info=True)
                        cb_pause = _CIRCUIT_BREAKER_PAUSE; _consecutive_rollbacks = 0
                    else:
                        cb_pause = 0
                elif action == "scaled_up":
                    _consecutive_rollbacks = 0; cb_pause = 0
                else:
                    cb_pause = 0
            if _stop_event.is_set():
                break
            _apply_scale(target, task_fn)
            # Autoscaler-driven scale-down (previously unwired dead code — now active).
            # Runs after rollout decisions so rollout always takes precedence.
            try:
                _scale_down_target = get_autoscaler().get_recommended_scale_down_target()
                if _scale_down_target is not None:
                    with _lock:
                        _current_count = len(_workers)
                    if _scale_down_target < _current_count:
                        # Only apply if strictly less: this path is scale-DOWN
                        # only; scale-up remains the sole responsibility of the
                        # rollout decision block above.
                        _logger.info(
                            "Autoscaler recommends scale-down to %d workers "
                            "(current: %d)",
                            _scale_down_target, _current_count,
                        )
                        _apply_scale(_scale_down_target, task_fn)
            except Exception:
                _logger.warning(
                    "Autoscaler scale-down evaluation failed.", exc_info=True
                )
            _log_event("runtime", action, "loop_tick", {"target": target, "metrics": metrics, "decision": decision})
            _loop_error_count = 0
            if cb_pause > 0:
                _safe_sleep(cb_pause)
        except Exception as exc:
            _loop_error_count += 1
            _log_event("runtime", "error", "loop_error", {"error": _sanitize_error(exc), "count": _loop_error_count})
            if _loop_error_count >= _MAX_LOOP_ERRORS:
                _logger.critical("Runtime loop exceeded %d consecutive errors; halting.", _MAX_LOOP_ERRORS)
                break
        _safe_sleep(interval)
def _handle_shutdown(signum, frame):
    """Signal handler for SIGTERM/SIGINT — initiate graceful shutdown."""
    _logger.info("Received signal %d, initiating graceful shutdown...", signum)
    stop(timeout=_WORKER_TIMEOUT)
def register_signal_handlers():
    """Register SIGTERM/SIGINT handlers and atexit hook for graceful shutdown.

    Signal handler registration is skipped when called from a non-main thread
    (Python only allows signal handlers on the main thread). The atexit hook
    is always registered regardless of calling thread.
    """
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
    else:
        _logger.debug(
            "register_signal_handlers() called from non-main thread; "
            "SIGTERM/SIGINT handlers not registered (atexit hook still active)"
        )
    atexit.register(stop, timeout=_WORKER_TIMEOUT)


def _validate_startup_config() -> None:
    """Validate critical startup config. Raises ConfigError on invalid values."""
    raw = os.environ.get("WORKER_COUNT", "")
    worker_count: int | None = None
    if not raw:
        _logger.warning("WORKER_COUNT not set — defaulting to 1")
    else:
        try:
            worker_count = int(raw)
        except ValueError as exc:
            raise ConfigError(f"WORKER_COUNT={raw!r} is not a valid integer") from exc
        if worker_count < 1 or worker_count > 500:
            raise ConfigError(f"WORKER_COUNT={worker_count} out of range [1, 500]")

    max_raw = os.environ.get("MAX_WORKER_COUNT", "")
    max_worker_count: int | None = None
    if max_raw:
        try:
            max_worker_count = int(max_raw)
        except ValueError as exc:
            raise ConfigError(
                f"MAX_WORKER_COUNT={max_raw!r} is not a valid integer"
            ) from exc
        if max_worker_count < 1 or max_worker_count > 500:
            raise ConfigError(
                f"MAX_WORKER_COUNT={max_worker_count} out of range [1, 500]"
            )
        # RT-CAP-50-VS-500 (option a): cap is 500, but anything >100 is a
        # high-concurrency regime the operator must have intentionally opted
        # into — emit a WARNING at startup so it shows up in logs.
        if max_worker_count > 100:
            _logger.warning(
                "MAX_WORKER_COUNT=%d exceeds 100 — high-concurrency regime; "
                "ensure billing pool, proxy pool, and BitBrowser profile pool "
                "are sized accordingly (Blueprint §1, §2.1).",
                max_worker_count,
            )
        if worker_count is not None and worker_count > max_worker_count:
            raise ConfigError(
                f"WORKER_COUNT={worker_count} exceeds "
                f"MAX_WORKER_COUNT={max_worker_count}"
            )

    try:
        validate_config()
    except DelayConfigError as exc:
        raise ConfigError(str(exc)) from exc

    if not os.environ.get("GIVEX_ENDPOINT"):
        _logger.warning("GIVEX_ENDPOINT not set — will use default")

def _validate_billing_pool_preflight() -> None:  # pylint: disable=protected-access
    """Validate billing pool before runtime startup. Raises RuntimeError if invalid.

    Checks:
    1. BILLING_POOL_DIR exists and is a directory.
    2. At least one .txt file exists in the pool directory.
    3. If MIN_BILLING_PROFILES > 0, at least that many valid profiles can be loaded.

    Raises:
        RuntimeError: with a descriptive operational message if any check fails.
    """
    pool_dir = billing._pool_dir()
    if not pool_dir.is_dir():
        raise RuntimeError(
            f"Billing pool directory '{pool_dir}' does not exist."
            " Startup aborted."
        )
    if not list(pool_dir.glob("*.txt")):
        raise RuntimeError(
            f"Billing pool directory '{pool_dir}' contains no"
            " .txt files. Startup aborted."
        )
    min_profiles = billing._MIN_BILLING_PROFILES
    if min_profiles > 0:
        profiles = billing._read_profiles_from_disk()
        count = len(profiles)
        if count < min_profiles:
            raise RuntimeError(
                f"Billing pool has {count} profiles, below minimum"
                f" threshold {min_profiles}. Startup aborted."
            )
    _logger.info("Billing pool preflight OK: dir=%s", pool_dir)
def is_dom_only_watchdog_allowed() -> bool:
    """Return ``True`` when ``ALLOW_DOM_ONLY_WATCHDOG`` is set to 1/true/yes.

    Documented degraded-mode opt-in for drivers lacking ``add_cdp_listener``.
    See ``docs/audit/addendum-selenium-flavor.md`` for the full contract.
    """
    return os.environ.get("ALLOW_DOM_ONLY_WATCHDOG", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def probe_cdp_listener_support(driver_obj: object) -> bool:
    """Assert *driver_obj* exposes a callable ``add_cdp_listener`` method.

    Returns ``True`` when the hook is callable. Returns ``False`` and logs a
    WARNING when the hook is missing **and** ``ALLOW_DOM_ONLY_WATCHDOG`` is
    set (DOM-polling fallback for Phase A + Phase C signal sources). Raises
    ``RuntimeError`` otherwise so misconfigured Selenium flavors are caught
    at driver bring-up time rather than silently as a 10s Phase A cycle
    timeout (issue F2 audit).

    Called from ``integration/worker_task.py`` immediately after the
    seleniumwire driver is constructed; single source of truth for the check.
    """
    if (hasattr(driver_obj, "add_cdp_listener")
            and callable(getattr(driver_obj, "add_cdp_listener", None))):
        return True
    if is_dom_only_watchdog_allowed():
        _logger.warning(
            "Driver lacks callable 'add_cdp_listener'; "
            "ALLOW_DOM_ONLY_WATCHDOG=1 — DOM polling for Phase A + Phase C. "
            "DEGRADED mode; re-install selenium-wire==5.1.0 to restore CDP."
        )
        return False
    raise RuntimeError(
        "Driver does not expose a callable 'add_cdp_listener'. "
        "Install selenium-wire==5.1.0 (or equivalent CDP-capable flavor), "
        "or set ALLOW_DOM_ONLY_WATCHDOG=1 to opt into DOM-polling fallback "
        "(see docs/audit/addendum-selenium-flavor.md)."
    )


def is_production_task_fn_enabled() -> bool:
    """Return True when the ``ENABLE_PRODUCTION_TASK_FN`` feature flag is active.

    The flag must be set to one of ``1``, ``true``, or ``yes`` (case-insensitive)
    to enable the production browser lifecycle wired in PR-04.  Defaults to
    ``False`` so that new deployments opt-in explicitly.
    """
    return os.environ.get("ENABLE_PRODUCTION_TASK_FN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def start(task_fn, interval=None):
    """Start the runtime loop. Returns True if started, False if already running."""
    global _state, _loop_thread, _trace_id
    interval = _DEFAULT_LOOP_INTERVAL if interval is None else interval
    try:
        if interval <= 0: interval = _MIN_LOOP_INTERVAL
    except TypeError:
        interval = _MIN_LOOP_INTERVAL
    with _lock:
        if _state not in ("INIT", "STOPPED"):
            return False
        _validate_startup_config()
    # Propagate the effective worker cap BEFORE the loop starts. This also
    # clears any stale runtime override from a previous start() call when the
    # env var is now unset (defaulting back to 10).
    max_raw = os.environ.get("MAX_WORKER_COUNT", "").strip()
    try:
        cap = int(max_raw) if max_raw else 10
    except ValueError as exc:
        raise ConfigError(
            f"Failed to configure rollout cap from MAX_WORKER_COUNT={max_raw!r}: {exc}"
        ) from exc
    if rollout.SCALE_STEPS[-1] != cap:
        try:
            rollout.configure_max_workers(cap)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Failed to configure rollout cap from MAX_WORKER_COUNT={max_raw!r}: {exc}"
            ) from exc
    if rollout.SCALE_STEPS[-1] != cap:
        raise ConfigError(
            f"rollout.SCALE_STEPS[-1]={rollout.SCALE_STEPS[-1]} != "
            f"MAX_WORKER_COUNT={cap}"
        )
    _ensure_rollout_configured()
    try:
        _validate_billing_pool_preflight()
    except RuntimeError as exc:
        _logger.error("Billing pool preflight validation failed: %s", exc)
        raise
    with _lock:
        if _state not in ("INIT", "STOPPED"):
            return False
        _stop_event.clear()
        _loop_thread = threading.Thread(target=_runtime_loop, args=(task_fn, interval), daemon=False)
        with _trace_lock:
            _trace_id = uuid.uuid4().hex[:12]
        _state = "RUNNING"
        loop_thread = _loop_thread
    loop_thread.start()
    register_signal_handlers()
    _log_event("runtime", "started", "runtime_start")
    return True
def stop(timeout=None):
    """Stop the runtime loop and all active workers.

    Sets state to STOPPING so workers check _should_stop_worker() at safe
    points.  After the graceful period, a hard timeout forces cleanup of
    any workers that did not reach a safe point in time.
    """
    global _state, _loop_thread
    timeout = _WORKER_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    with _lock:
        if _state != "RUNNING":
            return False
        _state = "STOPPING"
        loop_thread = _loop_thread
    _stop_event.set()
    loop_deadline = time.monotonic() + (timeout * 0.3)
    if loop_thread is not None and loop_thread.is_alive():
        loop_thread.join(timeout=max(0, loop_deadline - time.monotonic()))
    loop_stopped = loop_thread is None or not loop_thread.is_alive()
    with _lock:
        if loop_stopped:
            _loop_thread = None
        wids = list(_workers.keys())
    all_stopped = True
    per_worker_timeout = (timeout * 0.7) / max(1, len(wids)) if wids else 0
    for wid in wids:
        if not stop_worker(wid, timeout=per_worker_timeout):
            all_stopped = False
    # Hard timeout: log any workers still registered after graceful stop.
    # Stragglers are NOT removed from _workers/_worker_states so that their
    # still-running threads can call set_worker_state() without ValueError.
    # _worker_fn's finally block cleans them up when threads eventually exit.
    with _lock:
        stragglers = list(_workers.keys())
    if stragglers:
        _logger.warning("Hard timeout: %d workers still running: %s", len(stragglers), stragglers)
        for wid in stragglers:
            _log_event(wid, "stopping", "hard_timeout")
        all_stopped = False
        # Resource cleanup for straggler threads that exceeded shutdown timeout.
        # Proxy release is idempotent — safe even if the thread eventually
        # self-exits and triggers _worker_fn.finally release as well.
        for wid in stragglers:
            try:
                get_default_pool().release(wid)
            except Exception:  # pylint: disable=broad-except
                _logger.warning(
                    "stop(): failed to release proxy for straggler %s", wid, exc_info=True
                )
            # Clean up FSM state to prevent stale per-worker registry entries
            try:
                fsm.cleanup_worker(wid)
            except Exception:  # pylint: disable=broad-except
                _logger.debug(
                    "stop(): FSM cleanup_worker skipped for straggler %s",
                    wid, exc_info=True,
                )
            # stop_worker() already left a stop request in place for stragglers.
    # Second join: give the loop thread remaining budget to finish its
    # current tick.  _state is STOPPING and _stop_event is set, so the
    # loop will break at the top of the next iteration or at the
    # _stop_event guard before _apply_scale.
    if not loop_stopped and loop_thread is not None and loop_thread.is_alive():
        remaining = max(0, deadline - time.monotonic())
        loop_thread.join(timeout=remaining)
        loop_stopped = not loop_thread.is_alive()
        if loop_stopped:
            with _lock:
                _loop_thread = None
    with _lock:
        _state = "STOPPED"
    flush_ok = False
    try:
        from integration.orchestrator import _flush_idempotency_store
        _flush_idempotency_store()
        flush_ok = True
    except Exception:
        _logger.warning("Failed to flush idempotency store during shutdown", exc_info=True)
    if flush_ok:
        _logger.info("All workers stopped. Idempotency store flushed.")
    else:
        _logger.warning("All workers stopped. Idempotency store flush skipped or failed.")
    if not loop_stopped or not all_stopped:
        _log_event("runtime", "stopped", "runtime_stop_partial")
        return False
    _log_event("runtime", "stopped", "runtime_stop")
    return True


def wait(timeout: float | None = None) -> bool:
    """Block until the runtime loop thread exits.

    timeout: seconds to wait; None waits indefinitely.

    Returns True if the loop thread is no longer alive (or never existed).
    Returns False if timeout expires with the loop thread still running.
    Returns immediately with True when the loop thread is None.
    """
    with _lock:
        loop_thread = _loop_thread
    if loop_thread is None:
        return True
    loop_thread.join(timeout=timeout)
    return not loop_thread.is_alive()
def is_running() -> bool:
    with _lock:
        return _state == "RUNNING"
def get_status():
    """Return a snapshot of the runtime state."""
    # Read _log_sink_error_count under its dedicated lock (consistent with
    # _log_event() write path and reset() clear path).
    with _log_sink_error_lock:
        sink_errors = _log_sink_error_count
    with _lock:
        with _trace_lock:
            tid = _trace_id
        return {"running": _state == "RUNNING", "state": _state, "active_workers": list(_workers.keys()), "worker_count": len(_workers), "consecutive_rollbacks": _consecutive_rollbacks, "trace_id": tid, "billing_throttled": _is_billing_throttled(), "consecutive_billing_failures": _consecutive_billing_failures, "log_sink_errors": sink_errors}
def get_deployment_status():
    """Return a comprehensive production deployment health snapshot.

    Combines runtime state with monitor metrics for production monitoring.
    Tracks worker stability, restart patterns, and error rates.

    Returns a dict with keys:
        running (bool): Whether the runtime loop is active.
        state (str): Current lifecycle state.
        worker_count (int): Number of active workers.
        active_workers (list[str]): Active worker IDs.
        consecutive_rollbacks (int): Consecutive rollback count.
        trace_id (str | None): Current trace ID.
        log_sink_errors (int): Cumulative log_sink.emit() failure count.
        metrics (dict | None): Monitor metrics snapshot, or None if
            monitor.get_metrics() is unavailable.
    """
    status = get_status()
    try:
        metrics = monitor.get_metrics()
    except Exception as exc:
        _logger.warning("monitor.get_metrics() failed in get_deployment_status(): %s", exc, exc_info=True)
        metrics = None
    return {
        "running": status["running"],
        "state": status["state"],
        "worker_count": status["worker_count"],
        "active_workers": status["active_workers"],
        "consecutive_rollbacks": status["consecutive_rollbacks"],
        "trace_id": status["trace_id"],
        "log_sink_errors": status["log_sink_errors"],
        "metrics": metrics,
    }
def verify_deployment():
    """Verify production deployment status.

    Checks that the system is healthy according to the spec thresholds:
      - Service running (state == RUNNING)
      - Workers active (worker_count > 0)
      - No startup errors (error_rate <= 5%, restarts <= 3/hr,
        consecutive_rollbacks == 0)

    Returns a dict with keys:
        passed (bool): True if all checks passed.
        checks (dict): Individual check results (bool per check).
        errors (list[str]): Human-readable failure reasons (empty on success).
    """
    ds = get_deployment_status()
    errors = []
    service_running = ds["running"] and ds["state"] == "RUNNING"
    workers_active = ds["worker_count"] > 0 and len(ds["active_workers"]) > 0
    no_startup_errors = True
    if not service_running:
        no_startup_errors = False
        errors.append(f"Service not running: state={ds['state']}")
    if not workers_active:
        no_startup_errors = False
        errors.append(f"No active workers: worker_count={ds['worker_count']}")
    if ds["consecutive_rollbacks"] > 0:
        no_startup_errors = False
        errors.append(f"Consecutive rollbacks: {ds['consecutive_rollbacks']}")
    metrics = ds["metrics"]
    if metrics is not None:
        if metrics["error_rate"] > ERROR_RATE_THRESHOLD:
            no_startup_errors = False
            errors.append(
                f"Error rate above threshold:"
                f" {metrics['error_rate']:.2%}"
                f" > {ERROR_RATE_THRESHOLD:.0%}"
            )
        if metrics["restarts_last_hour"] > MAX_RESTARTS_PER_HOUR:
            no_startup_errors = False
            errors.append(
                f"Restarts above threshold:"
                f" {metrics['restarts_last_hour']}"
                f" > {MAX_RESTARTS_PER_HOUR}"
            )
    elif ds["running"]:
        no_startup_errors = False
        errors.append("Monitor metrics unavailable while service running")
    return {
        "passed": service_running and workers_active and no_startup_errors,
        "checks": {
            "service_running": service_running,
            "workers_active": workers_active,
            "no_startup_errors": no_startup_errors,
        },
        "errors": errors,
    }
def get_state() -> str:
    """Return the current lifecycle state."""
    with _lock:
        return _state
def set_behavior_delay_enabled(enabled):
    """Enable or disable behavioral delay wrapping for workers."""
    global _behavior_delay_enabled
    with _lock:
        _behavior_delay_enabled = bool(enabled)
def set_stagger_enabled(enabled: bool) -> None:
    """Enable or disable the 12–25s stagger between worker launches.

    Independent from :func:`set_behavior_delay_enabled` (RT-STAGGER-FLAG):
    tests disabling behavior wrapping must still be able to exercise the
    stagger gap, and vice-versa.  Default is ``True`` in production and
    ``False`` after :func:`reset` (same test-hygiene semantics as the
    behavior-delay flag).
    """
    global _stagger_enabled
    with _lock:
        _stagger_enabled = bool(enabled)
def get_trace_id():
    """Return the current trace_id, or None if not started."""
    with _trace_lock: return _trace_id


def get_worker_browser_profile(worker_id: str):  # -> Optional[str]
    """Return registered browser profile id for the worker, if any."""
    return cdp.get_browser_profile(worker_id)


def reset():
    """Reset all runtime state. Intended for testing.

    Raises RuntimeError if called while the runtime is actively running
    in production mode (behavior delay enabled). Call stop() first.
    """
    global _state, _loop_thread, _workers, _worker_states, _worker_counter, _consecutive_rollbacks, _pending_restarts, _trace_id, _behavior_delay_enabled, _stagger_enabled, _loop_error_count, _restart_delay, _consecutive_billing_failures, _billing_throttled_until, _log_sink_error_count, _last_worker_launch_ts
    with _lock:
        if _state == "RUNNING" and _behavior_delay_enabled:
            raise RuntimeError(
                "reset() called while runtime is actively running in production mode; "
                "call stop() first."
            )
    stop(timeout=2)
    with _lock:
        _state = "INIT"; _loop_thread = None; _workers = {}; _worker_states = {}; _worker_counter = 0
        _consecutive_rollbacks = 0; _pending_restarts = 0; _stop_requests.clear()
        _behavior_delay_enabled = False
        _stagger_enabled = False
        _loop_error_count = 0; _restart_delay = 0
        _consecutive_billing_failures = 0; _billing_throttled_until = 0.0
    # Clear _log_sink_error_count under its dedicated lock (consistent with
    # _log_event() write path and get_status() read path).
    with _log_sink_error_lock:
        _log_sink_error_count = 0
    with _stagger_lock:
        _last_worker_launch_ts = 0.0
    with _trace_lock:
        _trace_id = None
    _stop_event.clear()
    behavior.reset()
    rollout.reset()
    monitor.reset()
    _autoscaler_module.reset()
    fsm.reset_registry()
    metrics_exporter.reset()
    log_sink.reset()
    alerting.reset()
