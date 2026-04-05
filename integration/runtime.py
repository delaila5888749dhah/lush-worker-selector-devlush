"""Runtime orchestrator for worker scaling and monitoring."""
import logging
import threading
import time
import uuid
from modules.behavior import main as behavior
from modules.monitor import main as monitor
from modules.rollout import main as rollout
_logger = logging.getLogger(__name__)
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
_workers: dict = {}
_worker_states: dict = {}
_worker_counter = 0
_loop_thread = None
_trace_id = None
_trace_lock = threading.Lock()
_NO_TRACE = "no-trace"
_DEFAULT_LOOP_INTERVAL = 10
_MIN_LOOP_INTERVAL = 0.1
_WORKER_TIMEOUT = 30
_MAX_CONSECUTIVE_ROLLBACKS = 3
_consecutive_rollbacks = 0
_pending_restarts = 0
_stop_requests = set()
def _should_stop_worker(worker_id):
    return worker_id not in _workers or worker_id in _stop_requests or _state == "STOPPING"
def _log_event(worker_id, state, action, metrics=None):
    with _trace_lock:
        tid = _trace_id or _NO_TRACE
    _logger.info("%s | %s | %s | %s | %s | %s", time.strftime("%Y-%m-%dT%H:%M:%S"), worker_id, tid, state, action, metrics or "")
def _safe_sleep(interval):
    try: time.sleep(interval)
    except (TypeError, ValueError): time.sleep(_MIN_LOOP_INTERVAL)
def _ensure_rollout_configured():
    with rollout._lock:
        check_fn = rollout._check_rollback_fn; save_fn = rollout._save_baseline_fn
    if check_fn is None and save_fn is None:
        rollout.configure(monitor.check_rollback_needed, monitor.save_baseline)
def _transition_worker_state_locked(worker_id, new_state):
    """Transition worker state while holding _lock. Raises ValueError on invalid transition."""
    current = _worker_states.get(worker_id)
    if current is None:
        raise ValueError(f"Worker {worker_id} has no tracked state")
    if new_state not in _VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid worker state transition: {current} -> {new_state} for {worker_id}")
    _worker_states[worker_id] = new_state
def _worker_fn(worker_id, task_fn):
    global _pending_restarts
    try:
        _log_event(worker_id, "running", "start")
        while True:
            with _lock:
                if _should_stop_worker(worker_id):
                    break
                _transition_worker_state_locked(worker_id, "IN_CYCLE")
            try:
                task_fn(worker_id)
                try:
                    monitor.record_success()
                except Exception:
                    _logger.warning("monitor.record_success() failed for %s", worker_id, exc_info=True)
            except Exception as exc:
                try:
                    monitor.record_error()
                except Exception:
                    _logger.warning("monitor.record_error() failed for %s", worker_id, exc_info=True)
                with _lock:
                    if worker_id in _workers and worker_id not in _stop_requests: _pending_restarts += 1
                _log_event(worker_id, "error", "task_failed", {"error": str(exc)})
                break
            with _lock:
                current_state = _worker_states.get(worker_id)
                if current_state is not None:
                    if current_state in ("SAFE_POINT", "CRITICAL_SECTION"):
                        _transition_worker_state_locked(worker_id, "IN_CYCLE")
                        current_state = "IN_CYCLE"
                    if current_state == "IN_CYCLE":
                        _transition_worker_state_locked(worker_id, "IDLE")
    except Exception as exc:
        _logger.error("Unexpected error in worker %s: %s", worker_id, exc, exc_info=True)
    finally:
        with _lock:
            _stop_requests.discard(worker_id); _workers.pop(worker_id, None)
            _worker_states.pop(worker_id, None)
        _log_event(worker_id, "stopped", "stop")
def start_worker(task_fn):
    """Start a new worker thread running *task_fn*. Returns the worker id."""
    global _worker_counter
    with _lock:
        _worker_counter += 1
        wid = f"worker-{_worker_counter}"
        t = threading.Thread(target=_worker_fn, args=(wid, task_fn), daemon=True)
        _workers[wid] = t
        _worker_states[wid] = "IDLE"
    try:
        t.start()
    except (RuntimeError, OSError):
        with _lock:
            _workers.pop(wid, None)
            _worker_states.pop(wid, None)
        raise
    return wid
def stop_worker(worker_id, timeout=None):
    """Remove a worker from the active set and join its thread."""
    with _lock:
        thread = _workers.get(worker_id)
        if thread is None:
            return False
        _stop_requests.add(worker_id)
    if thread is threading.current_thread():
        raise RuntimeError("cannot join current thread")
    if thread.ident is None:
        # Thread registered but not yet started; is_alive() will be False below,
        # so cleanup proceeds normally. _worker_fn's finally block handles the
        # eventual start → immediate stop via _should_stop_worker.
        _logger.debug("join() on not-yet-started thread for %s; will self-cleanup via _worker_fn", worker_id)
    else:
        try:
            thread.join(timeout=_WORKER_TIMEOUT if timeout is None else timeout)
        except RuntimeError as exc:
            _logger.warning("RuntimeError joining worker %s: %s", worker_id, exc, exc_info=True)
            return False
    if thread.is_alive():
        _logger.warning("Worker %s did not stop within timeout", worker_id)
        with _lock:
            _workers.pop(worker_id, None)
            _worker_states.pop(worker_id, None)
            _stop_requests.discard(worker_id)
        return False
    with _lock:
        _stop_requests.discard(worker_id); _workers.pop(worker_id, None)
        _worker_states.pop(worker_id, None)
    _log_event(worker_id, "stopped", "stop_requested")
    return True
def get_active_workers():
    """Return a list of active worker ids."""
    with _lock: return list(_workers.keys())
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
def is_safe_to_control():
    """Return True only when all tracked workers are IDLE or SAFE_POINT.

    Missing state entries are treated as UNSAFE.
    Returns True when there are no workers (vacuous truth for empty set).
    """
    with _lock:
        for wid in _workers:
            ws = _worker_states.get(wid)
            if ws is None or ws not in ("IDLE", "SAFE_POINT"):
                return False
        return True
def _apply_scale(target_count, task_fn):
    global _pending_restarts
    with _lock: current_ids = list(_workers.keys())
    current_count = len(current_ids)
    if target_count > current_count:
        with _lock:
            restarted = min(_pending_restarts, target_count - current_count); _pending_restarts -= restarted
        for i in range(target_count - current_count):
            start_worker(task_fn)
            if i < restarted: monitor.record_restart()
        _log_event("runtime", "scaling", "scale_up", {"from": current_count, "to": target_count})
    elif target_count < current_count:
        for wid in current_ids[target_count:]:
            stop_worker(wid, timeout=5)
        with _lock: _pending_restarts = 0
        _log_event("runtime", "scaling", "scale_down", {"from": current_count, "to": target_count})
def _runtime_loop(task_fn, interval):
    global _consecutive_rollbacks
    while True:
        with _lock:
            if _state != "RUNNING":
                break
        try:
            try:
                metrics = monitor.get_metrics()
            except Exception as exc:
                _log_event("runtime", "warning", "monitor_unavailable", {"error": str(exc)}); _safe_sleep(interval); continue
            step_index = rollout.get_current_step_index()
            max_index = len(rollout.SCALE_STEPS) - 1
            decision, decision_reasons = behavior.evaluate(metrics, step_index, max_index)
            if decision == behavior.SCALE_DOWN:
                target = rollout.force_rollback(reason="; ".join(decision_reasons))
                action = "rollback"
            elif decision == behavior.SCALE_UP:
                target, action, _ = rollout.try_scale_up()
            else:
                target = rollout.get_current_workers()
                action = "hold"
            with _lock:
                if action == "rollback":
                    _consecutive_rollbacks += 1
                    if _consecutive_rollbacks >= _MAX_CONSECUTIVE_ROLLBACKS:
                        _log_event("runtime", "warning", "consecutive_rollbacks", {"count": _consecutive_rollbacks})
                elif action == "scaled_up":
                    _consecutive_rollbacks = 0
            _apply_scale(target, task_fn)
            _log_event("runtime", action, "loop_tick", {"target": target, "metrics": metrics, "decision": decision})
        except Exception as exc:
            _log_event("runtime", "error", "loop_error", {"error": str(exc)})
        _safe_sleep(interval)
def start(task_fn, interval=None):
    """Start the runtime loop. Returns True if started, False if already running."""
    global _state, _loop_thread, _trace_id
    interval = _DEFAULT_LOOP_INTERVAL if interval is None else interval
    try:
        if interval <= 0: interval = _MIN_LOOP_INTERVAL
    except TypeError:
        interval = _MIN_LOOP_INTERVAL
    _ensure_rollout_configured()
    with _lock:
        if _state not in ("INIT", "STOPPED"):
            return False
        _loop_thread = threading.Thread(target=_runtime_loop, args=(task_fn, interval), daemon=True)
        _state = "RUNNING"; _loop_thread.start()
    with _trace_lock:
        _trace_id = uuid.uuid4().hex[:12]
    _log_event("runtime", "started", "runtime_start")
    return True
def stop(timeout=None):
    """Stop the runtime loop and all active workers."""
    global _state, _loop_thread
    timeout = _WORKER_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    with _lock:
        if _state != "RUNNING":
            return False
        _state = "STOPPING"
        loop_thread = _loop_thread
    if loop_thread is not None:
        loop_thread.join(timeout=max(0, deadline - time.monotonic()))
    loop_stopped = loop_thread is None or not loop_thread.is_alive()
    with _lock:
        if loop_stopped:
            _loop_thread = None
        wids = list(_workers.keys())
    all_stopped = True
    for wid in wids:
        if not stop_worker(wid, timeout=max(0, deadline - time.monotonic())):
            all_stopped = False
    with _lock:
        _state = "STOPPED"
    if not loop_stopped or not all_stopped:
        _log_event("runtime", "stopped", "runtime_stop_partial")
        return False
    _log_event("runtime", "stopped", "runtime_stop")
    return True
def is_running():
    with _lock: return _state == "RUNNING"
def get_status():
    """Return a snapshot of the runtime state."""
    with _trace_lock:
        tid = _trace_id
    with _lock:
        return {"running": _state == "RUNNING", "state": _state, "active_workers": list(_workers.keys()), "worker_count": len(_workers), "consecutive_rollbacks": _consecutive_rollbacks, "trace_id": tid}
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
        "metrics": metrics,
    }
_ERROR_RATE_THRESHOLD = getattr(monitor, "_ERROR_RATE_THRESHOLD", 0.05)
_MAX_RESTARTS_PER_HOUR = getattr(monitor, "_MAX_RESTARTS_PER_HOUR", 3)
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
        if metrics["error_rate"] > _ERROR_RATE_THRESHOLD:
            no_startup_errors = False
            errors.append(f"Error rate above threshold: {metrics['error_rate']:.2%} > {_ERROR_RATE_THRESHOLD:.0%}")
        if metrics["restarts_last_hour"] > _MAX_RESTARTS_PER_HOUR:
            no_startup_errors = False
            errors.append(f"Restarts above threshold: {metrics['restarts_last_hour']} > {_MAX_RESTARTS_PER_HOUR}")
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
def get_state():
    """Return the current lifecycle state."""
    with _lock: return _state
def get_trace_id():
    """Return the current trace_id, or None if not started."""
    with _trace_lock: return _trace_id
def reset():
    """Reset all runtime state. Intended for testing."""
    global _state, _loop_thread, _workers, _worker_states, _worker_counter, _consecutive_rollbacks, _pending_restarts, _trace_id
    stop(timeout=2)
    with _lock:
        _state = "INIT"; _loop_thread = None; _workers = {}; _worker_states = {}; _worker_counter = 0
        _consecutive_rollbacks = 0; _pending_restarts = 0; _stop_requests.clear()
    with _trace_lock:
        _trace_id = None
    behavior.reset()
