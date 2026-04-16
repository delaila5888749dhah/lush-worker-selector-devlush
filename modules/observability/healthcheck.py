"""Health check module — HTTP endpoint for external health probes (Ext-3)."""
import http.server
import json
import logging
import threading

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_server_thread = None
_server_instance = None
_stopping = False
DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
_UNKNOWN = {"status": "unknown", "running": False, "state": "unknown",
            "worker_count": 0, "consecutive_rollbacks": 0, "errors": []}


def get_health(status_fn=None) -> dict:
    """Return health snapshot. status_fn: Callable()->dict injected by caller."""
    if status_fn is None:
        return {**_UNKNOWN, "errors": ["status_fn not configured"]}
    try:
        ds = status_fn()
        errors = []
        if not ds.get("running"):
            errors.append(f"runtime not running: state={ds.get('state')}")
        if ds.get("consecutive_rollbacks", 0) > 0:
            errors.append(f"consecutive_rollbacks={ds['consecutive_rollbacks']}")
        m = ds.get("metrics") or {}
        if m.get("error_rate", 0) > 0.05:
            errors.append(f"error_rate={m['error_rate']:.1%}")
        return {"status": "healthy" if not errors else "degraded",
                "running": ds.get("running", False), "state": ds.get("state", "unknown"),
                "worker_count": ds.get("worker_count", 0),
                "consecutive_rollbacks": ds.get("consecutive_rollbacks", 0), "errors": errors}
    except Exception as exc:
        _logger.warning("get_health() failed: %s", exc)
        return {**_UNKNOWN, "errors": [str(exc)]}


def build_response(path: str, status_fn) -> tuple:
    """Build the HTTP response for a given request path.

    Returns a (status_code: int, body: bytes) tuple.  Extracted at module
    level so that the response logic can be unit-tested without standing up
    a live HTTP server.
    """
    if path == "/health":
        body = json.dumps(get_health(status_fn)).encode()
        return 200, body
    body = json.dumps({"error": "not found"}).encode()
    return 404, body


def start_server(host=DEFAULT_HOST, port=DEFAULT_PORT, status_fn=None) -> bool:
    """Start health check server in daemon thread. Returns True if started."""
    global _server_thread, _server_instance
    with _lock:
        if _stopping:
            return False
        if _server_thread is not None and _server_thread.is_alive():
            return False
        # Cleanup stale state: thread exited after a timed-out stop attempt
        # but _server_instance was never closed.
        if (_server_thread is not None and not _server_thread.is_alive()
                and _server_instance is not None):
            stale_inst = _server_instance
            _server_thread = None
            _server_instance = None
        else:
            stale_inst = None
    if stale_inst is not None:
        try:
            stale_inst.server_close()
        except Exception as exc:
            _logger.debug("healthcheck: stale server_close() failed (ignored): %s", exc)
    with _lock:
        if _stopping:
            return False
        if _server_thread is not None and _server_thread.is_alive():
            return False
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = build_response(self.path, status_fn)
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, fmt, *args):
                pass
        _server_instance = http.server.ThreadingHTTPServer((host, port), _Handler)
        _server_thread = threading.Thread(target=_server_instance.serve_forever, daemon=True)
        _server_thread.start()
        return True


def stop_server(timeout=5.0) -> bool:
    """Stop health check server. Returns True if stopped cleanly.

    Thread-safety: references are captured under _lock and a _stopping flag
    blocks concurrent start_server() calls. shutdown() is initiated in a
    daemon thread so that join(timeout) can honour the caller's timeout;
    the subsequent server_close() runs outside _lock.  If join() times out,
    references are preserved so callers can retry.

    If a previous call timed out and the thread has since exited, the next
    call cleans up the stale instance and returns True.
    """
    global _server_thread, _server_instance, _stopping
    with _lock:
        # Cleanup path: thread exited after a previous timed-out stop attempt.
        if (_server_thread is not None and not _server_thread.is_alive()
                and _server_instance is not None):
            stale_inst = _server_instance
            _server_thread = None
            _server_instance = None
            _stopping = False
        else:
            stale_inst = None
    if stale_inst is not None:
        stale_inst.server_close()
        return True
    with _lock:
        if _server_instance is None or _server_thread is None or not _server_thread.is_alive():
            return False
        inst = _server_instance
        thread = _server_thread
        _stopping = True
    # Initiate shutdown in a daemon thread so join() can respect the timeout.
    threading.Thread(target=inst.shutdown, daemon=True).start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        with _lock:
            _stopping = False
        return False
    inst.server_close()
    with _lock:
        _server_thread = None
        _server_instance = None
        _stopping = False
    return True


def is_running() -> bool:
    """Return True if the health check server is currently running."""
    with _lock:
        return _server_thread is not None and _server_thread.is_alive()


def reset() -> None:
    """Reset server state. Intended for testing only.

    Calls stop_server() (which is itself thread-safe) then force-closes any
    remaining instance and clears all state under _lock. Not safe to call
    concurrently with start_server(); callers must ensure no concurrent
    server operations.
    """
    global _server_thread, _server_instance, _stopping
    stop_server(timeout=5.0)
    with _lock:
        inst = _server_instance
        _server_thread = None
        _server_instance = None
        _stopping = False
    if inst is not None:
        try:
            inst.server_close()
        except Exception as exc:
            _logger.debug("healthcheck: reset server_close() failed (ignored): %s", exc)
