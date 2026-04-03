"""Runtime orchestrator for worker scaling and monitoring."""
import logging
import threading
import time
from modules.monitor import main as monitor
from modules.rollout import main as rollout
_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_workers: dict = {}
_worker_counter = 0
_running = False
_loop_thread = None
_DEFAULT_LOOP_INTERVAL = 10
_MIN_LOOP_INTERVAL = 0.1
_WORKER_TIMEOUT = 30
_MAX_CONSECUTIVE_ROLLBACKS = 3
_consecutive_rollbacks = 0
_pending_restarts = 0
_stop_requests = set()
def _should_stop_worker(worker_id):
    return worker_id not in _workers or worker_id in _stop_requests or (not _running and _loop_thread is not None)
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
    _log_event(worker_id, "running", "start")
    try:
        while True:
            with _lock:
                if _should_stop_worker(worker_id):
                    break
            try:
                task_fn(worker_id)
                monitor.record_success()
            except Exception as exc:
                monitor.record_error()
                with _lock:
                    if worker_id in _workers and worker_id not in _stop_requests: _pending_restarts += 1
                _log_event(worker_id, "error", "task_failed", {"error": str(exc)})
                break
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
    t.start(); return wid
def stop_worker(worker_id, timeout=None):
    """Remove a worker from the active set and join its thread."""
    with _lock:
        thread = _workers.get(worker_id)
        if thread is None:
            return False
        _stop_requests.add(worker_id)
    thread.join(timeout=timeout or _WORKER_TIMEOUT)
    if thread.is_alive(): _logger.warning("Worker %s did not stop within timeout", worker_id); return False
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
            if not _running:
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
    global _running, _loop_thread
    interval = _DEFAULT_LOOP_INTERVAL if interval is None else interval
    try:
        if interval <= 0: interval = _MIN_LOOP_INTERVAL
    except TypeError:
        interval = _MIN_LOOP_INTERVAL
    with _lock:
        if _loop_thread is not None and _loop_thread.is_alive(): return False
        if _running:
            return False
        _ensure_rollout_configured()
        _loop_thread = threading.Thread(target=_runtime_loop, args=(task_fn, interval), daemon=True)
        _running = True; _loop_thread.start()
    _log_event("runtime", "started", "runtime_start")
    return True
def stop(timeout=None):
    """Stop the runtime loop and all active workers."""
    global _running, _loop_thread
    timeout = _WORKER_TIMEOUT if timeout is None else timeout
    with _lock:
        if not _running:
            return False
        _running = False
        loop_thread = _loop_thread
    if loop_thread is not None: loop_thread.join(timeout=timeout)
    if loop_thread is not None and loop_thread.is_alive(): return False
    with _lock:
        _loop_thread = None; wids = list(_workers.keys())
    all_stopped = True
    for wid in wids:
        if not stop_worker(wid, timeout=timeout):
            all_stopped = False
    if not all_stopped:
        return False
    _log_event("runtime", "stopped", "runtime_stop")
    return True
def is_running():
    with _lock: return _running
def get_status():
    """Return a snapshot of the runtime state."""
    with _lock:
        return {"running": _running, "active_workers": list(_workers.keys()), "worker_count": len(_workers), "consecutive_rollbacks": _consecutive_rollbacks}
def reset():
    """Reset all runtime state. Intended for testing."""
    global _running, _loop_thread, _workers, _worker_counter, _consecutive_rollbacks, _pending_restarts
    stop(timeout=2)
    with _lock:
        _running = False; _loop_thread = None; _workers = {}; _worker_counter = 0
        _consecutive_rollbacks = 0; _pending_restarts = 0; _stop_requests.clear()
