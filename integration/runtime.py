"""Runtime orchestrator for worker scaling and monitoring."""

import logging
import threading
import time
from modules.monitor import main as monitor
from modules.rollout import main as rollout
_logger = logging.getLogger(__name__)
_lock = threading.Lock()
# Active workers: {worker_id: threading.Thread}
_workers: dict = {}
_worker_counter = 0
_running = False
_loop_thread = None
_DEFAULT_LOOP_INTERVAL = 10
_WORKER_TIMEOUT = 30
_MAX_CONSECUTIVE_ROLLBACKS = 3
_consecutive_rollbacks = 0
_pending_restarts = 0

def _should_stop_worker(worker_id):
    return worker_id not in _workers or (not _running and _loop_thread is not None)

def _log_event(worker_id, state, action, metrics=None):
    """Log: timestamp | worker_id | state | action | metrics."""
    _logger.info("%s | %s | %s | %s | %s", time.strftime("%Y-%m-%dT%H:%M:%S"),
                 worker_id, state, action, metrics or "")
def _worker_fn(worker_id, task_fn):
    """Run *task_fn* in a loop until the worker is removed or runtime stops."""
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
                    if worker_id in _workers:
                        _pending_restarts += 1
                _log_event(worker_id, "error", "task_failed",
                           {"error": str(exc)})
                break
    finally:
        with _lock:
            _workers.pop(worker_id, None)
        _log_event(worker_id, "stopped", "stop")
def start_worker(task_fn):
    """Start a new worker thread running *task_fn*.  Returns the worker id."""
    global _worker_counter
    with _lock:
        _worker_counter += 1
        wid = f"worker-{_worker_counter}"
        t = threading.Thread(target=_worker_fn, args=(wid, task_fn),
                             daemon=True)
        _workers[wid] = t
    t.start()
    return wid
def stop_worker(worker_id, timeout=None):
    """Remove a worker from the active set and join its thread."""
    with _lock:
        thread = _workers.pop(worker_id, None)
    if thread is None:
        return False
    thread.join(timeout=timeout or _WORKER_TIMEOUT)
    _log_event(worker_id, "stopped", "stop_requested")
    return True
def get_active_workers():
    """Return a list of active worker ids."""
    with _lock:
        return list(_workers.keys())
def _apply_scale(target_count, task_fn):
    """Start or stop workers so that the active count matches *target_count*."""
    global _pending_restarts
    with _lock:
        current_ids = list(_workers.keys())
    current_count = len(current_ids)
    if target_count > current_count:
        restarted = 0
        with _lock:
            restarted = min(_pending_restarts, target_count - current_count)
            _pending_restarts -= restarted
        for i in range(target_count - current_count):
            start_worker(task_fn)
            if i < restarted:
                monitor.record_restart()
        _log_event("runtime", "scaling", "scale_up",
                   {"from": current_count, "to": target_count})
    elif target_count < current_count:
        for wid in current_ids[target_count:]:
            stop_worker(wid, timeout=5)
        with _lock:
            _pending_restarts = 0
        _log_event("runtime", "scaling", "scale_down",
                   {"from": current_count, "to": target_count})
def _runtime_loop(task_fn, interval):
    """Main loop: read metrics → call rollout → apply scale."""
    global _consecutive_rollbacks
    while True:
        with _lock:
            if not _running:
                break
        try:
            try:
                metrics = monitor.get_metrics()
            except Exception as exc:
                _log_event("runtime", "warning", "monitor_unavailable", {"error": str(exc)})
                time.sleep(interval)
                continue
            target, action, reasons = rollout.try_scale_up()
            with _lock:
                if action == "rollback":
                    _consecutive_rollbacks += 1
                    if _consecutive_rollbacks >= _MAX_CONSECUTIVE_ROLLBACKS:
                        _log_event("runtime", "warning",
                                   "consecutive_rollbacks",
                                   {"count": _consecutive_rollbacks})
                else:
                    _consecutive_rollbacks = 0
            _apply_scale(target, task_fn)
            _log_event("runtime", action, "loop_tick",
                       {"target": target, "metrics": metrics})
        except Exception as exc:
            _log_event("runtime", "error", "loop_error",
                       {"error": str(exc)})
        time.sleep(interval)
def start(task_fn, interval=None):
    """Start the runtime loop.  Returns True if started, False if already running."""
    global _running, _loop_thread
    interval = interval if interval is not None else _DEFAULT_LOOP_INTERVAL
    with _lock:
        if _running:
            return False
        _running = True
    _loop_thread = threading.Thread(target=_runtime_loop,
                                    args=(task_fn, interval), daemon=True)
    _loop_thread.start()
    _log_event("runtime", "started", "runtime_start")
    return True
def stop(timeout=None):
    """Stop the runtime loop and all active workers."""
    global _running, _loop_thread
    timeout = timeout if timeout is not None else _WORKER_TIMEOUT
    with _lock:
        if not _running:
            return False
        _running = False
    if _loop_thread is not None:
        _loop_thread.join(timeout=timeout)
        _loop_thread = None
    with _lock:
        wids = list(_workers.keys())
    for wid in wids:
        stop_worker(wid, timeout=timeout)
    _log_event("runtime", "stopped", "runtime_stop")
    return True
def is_running():
    """Return True if the runtime loop is active."""
    with _lock:
        return _running
def get_status():
    """Return a snapshot of the runtime state."""
    with _lock:
        return {
            "running": _running,
            "active_workers": list(_workers.keys()),
            "worker_count": len(_workers),
            "consecutive_rollbacks": _consecutive_rollbacks,
        }
def reset():
    """Reset all runtime state.  Intended for testing."""
    global _running, _loop_thread, _workers, _worker_counter
    global _consecutive_rollbacks, _pending_restarts
    stop(timeout=2)
    with _lock:
        _running = False
        _loop_thread = None
        _workers = {}
        _worker_counter = 0
        _consecutive_rollbacks = 0
        _pending_restarts = 0
