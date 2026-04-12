"""Metrics export — pluggable adapter layer for runtime metrics (Ext-1).

Default backend: structured JSON log via Python logging.
Custom backends can be registered via register_exporter() / unregister_exporter().
Thread-safe via threading.Lock. Stdlib only. No cross-module imports.
"""
import json
import logging
import threading
import time

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_exporters: list = []  # list of callable(metrics: dict) -> None
_export_count: int = 0
_log_export_enabled = True  # default log-based exporter enabled


def register_exporter(fn) -> None:
    """Register a custom exporter callable(metrics: dict) -> None."""
    with _lock:
        _exporters.append(fn)


def unregister_exporter(fn) -> bool:
    """Remove a previously registered exporter. Returns True if found."""
    with _lock:
        try:
            _exporters.remove(fn)
            return True
        except ValueError:
            return False


def set_log_export_enabled(enabled: bool) -> None:
    """Enable or disable the default log-based exporter."""
    global _log_export_enabled
    with _lock:
        _log_export_enabled = enabled

def export_metrics(metrics: dict) -> None:
    """Export a metrics snapshot to all registered backends.

    Exceptions from individual exporters are caught and logged as warnings.
    The default log backend emits a JSON line at DEBUG level.
    Args:
        metrics: dict from monitor.get_metrics().
    """
    global _export_count
    with _lock:
        exporters = list(_exporters)
        log_enabled = _log_export_enabled
        _export_count += 1
    if log_enabled:
        try:
            _logger.debug(json.dumps({"event": "metrics_export", "ts": time.time(), **metrics}))
        except Exception as exc:
            _logger.warning("metrics_exporter: log backend failed: %s", exc)
    for fn in exporters:
        try:
            fn(metrics)
        except Exception as exc:
            _logger.warning("metrics_exporter: exporter %r raised: %s", fn, exc)


def get_status() -> dict:
    """Return exporter status snapshot."""
    with _lock:
        return {"exporter_count": len(_exporters), "export_count": _export_count, "log_export_enabled": _log_export_enabled}


def reset() -> None:
    """Reset all exporter state. Intended for testing."""
    global _exporters, _export_count, _log_export_enabled
    with _lock:
        _exporters = []
        _export_count = 0
        _log_export_enabled = True


