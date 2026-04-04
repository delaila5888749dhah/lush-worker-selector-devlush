"""Runtime orchestrator for worker scaling and monitoring."""
import logging
import threading
import time
from modules.monitor import main as monitor
from modules.rollout import main as rollout
_logger = logging.getLogger(__name__)
ALLOWED_STATES = {"INIT", "RUNNING", "STOPPING", "STOPPED"}
_lock = threading.Lock()
_state = "INIT"
_workers: dict = {}
_worker_counter = 0
_loop_thread = None
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
    _logger.info("%s | %s | %s | %s | %s", time.strftime("%Y-%m-%dT%H:%M:%S"), worker_id, state, action, metrics or "")
def _safe_sleep(interval):
    try: time.sleep(interval)
    except (TypeError, ValueError): time.sleep(_MIN_LOOP_INTERVAL)
def _ensure_rollout_configured():
    with rollout._lock:
        check_fn = rollout._check_rollback_fn; save_fn = rollout._save_baseline_fn
    if check_fn is None and save_fn is None:
        rollout.configure(monitor.check_rollback_needed, monitor.save_baseline)
def _worker_fn(worker_id, task_fn):
    global _pending_restarts
    try:
        _log_event(worker_id, "running", "start")
        while True:
            with _lock:
                if _should_stop_worker(worker_id):
                    break
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
    except Exception as exc:
        _logger.error("Unexpected error in worker %s: %s", worker_id, exc, exc_info=True)
    finally:
        with _lock:
            _stop_requests.discard(worker_id); _workers.pop(worker_id, None)
        _log_event(worker_id, "stopped", "stop")
def start_worker(task_fn):
    """Start a new worker thread running *task_fn*. Returns the worker id."""
    global _worker_counter
    with _lock:
        _worker_counter += 1
        wid = f"worker-{_worker_counter}"
        t = threading.Thread(target=_worker_fn, args=(wid, task_fn), daemon=True)
        _workers[wid] = t
    try:
        t.start()
    except (RuntimeError, OSError):
        with _lock:
            _workers.pop(wid, None)
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
            _stop_requests.discard(worker_id)
        return False
    with _lock:
        _stop_requests.discard(worker_id); _workers.pop(worker_id, None)
    _log_event(worker_id, "stopped", "stop_requested")
    return True
def get_active_workers():
    """Return a list of active worker ids."""
    with _lock: return list(_workers.keys())
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
            target, action, reasons = rollout.try_scale_up()
            with _lock:
                if action == "rollback":
                    _consecutive_rollbacks += 1
                    if _consecutive_rollbacks >= _MAX_CONSECUTIVE_ROLLBACKS:
                        _log_event("runtime", "warning", "consecutive_rollbacks", {"count": _consecutive_rollbacks})
                else:
                    _consecutive_rollbacks = 0
            _apply_scale(target, task_fn)
            _log_event("runtime", action, "loop_tick", {"target": target, "metrics": metrics})
        except Exception as exc:
            _log_event("runtime", "error", "loop_error", {"error": str(exc)})
        _safe_sleep(interval)
def start(task_fn, interval=None):
    """Start the runtime loop. Returns True if started, False if already running."""
    global _state, _loop_thread
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
    with _lock:
        return {"running": _state == "RUNNING", "state": _state, "active_workers": list(_workers.keys()), "worker_count": len(_workers), "consecutive_rollbacks": _consecutive_rollbacks}
def get_state():
    """Return the current lifecycle state."""
    with _lock: return _state
def reset():
    """Reset all runtime state. Intended for testing."""
    global _state, _loop_thread, _workers, _worker_counter, _consecutive_rollbacks, _pending_restarts
    stop(timeout=2)
    with _lock:
        _state = "INIT"; _loop_thread = None; _workers = {}; _worker_counter = 0
        _consecutive_rollbacks = 0; _pending_restarts = 0; _stop_requests.clear()
