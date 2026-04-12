"""Health check module — HTTP endpoint for external health probes (Ext-3)."""
import http.server
import json
import logging
import threading

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_server_thread = None
_server_instance = None
DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
_UNKNOWN = {"status": "unknown", "running": False, "state": "unknown",
            "worker_count": 0, "consecutive_rollbacks": 0}


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


def start_server(host=DEFAULT_HOST, port=DEFAULT_PORT, status_fn=None) -> bool:
    """Start health check server in daemon thread. Returns True if started."""
    global _server_thread, _server_instance
    with _lock:
        if _server_thread is not None and _server_thread.is_alive():
            return False
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                ok = self.path == "/health"
                body = json.dumps(get_health(status_fn) if ok else {"error": "not found"}).encode()
                self.send_response(200 if ok else 404)
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
    """Stop health check server. Returns True if stopped cleanly."""
    global _server_thread, _server_instance
    with _lock:
        if _server_instance is None or _server_thread is None or not _server_thread.is_alive():
            return False
        inst = _server_instance
        inst.shutdown()
    _server_thread.join(timeout=timeout)
    inst.server_close()
    with _lock:
        alive = _server_thread.is_alive()
        _server_thread = None
        _server_instance = None
    return not alive


def is_running() -> bool:
    """Return True if the health check server is currently running."""
    with _lock:
        return _server_thread is not None and _server_thread.is_alive()


def reset() -> None:
    """Reset server state. Intended for testing."""
    global _server_thread, _server_instance
    stop_server()
    with _lock:
        _server_thread = None
        _server_instance = None
