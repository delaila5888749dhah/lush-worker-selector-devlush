"""Structured log sink — thread-safe structured JSON log emission (Ext-4).

Default behavior
----------------
``emit()`` serialises the event dict to JSON and logs it at ``DEBUG`` level
via the standard ``logging`` module. In most production deployments the
root logger level is ``INFO`` or higher, so **events are silently dropped
by default**.

Production usage
----------------
Call ``register_sink(fn)`` to forward events to your monitoring/alerting
pipeline (e.g. Datadog, Sentry, a structured log aggregator). Multiple
sinks can be registered; each receives a shallow copy-safe ``dict``.

Example::

    from modules.observability.log_sink import register_sink

    def forward_to_datadog(event: dict) -> None:
        statsd.event(event.get("type", "unknown"), str(event))

    register_sink(forward_to_datadog)

Sink failures are caught and logged at WARNING; they do not propagate.
"""
import copy
import json
import logging
import threading

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_sinks: list = []
_emit_count: int = 0
_log_sink_enabled = True


def register_sink(fn) -> None:
    """Register a custom sink callable(event: dict) -> None."""
    with _lock:
        _sinks.append(fn)


def unregister_sink(fn) -> bool:
    """Remove a previously registered sink. Returns True if found."""
    with _lock:
        try:
            _sinks.remove(fn)
            return True
        except ValueError:
            return False


def set_log_sink_enabled(enabled: bool) -> None:
    """Enable or disable the default log-based sink."""
    global _log_sink_enabled
    with _lock:
        _log_sink_enabled = enabled


def emit(event: dict) -> None:
    """Emit a structured log event to default backend and registered sinks."""
    global _emit_count
    try:
        with _lock:
            sinks = list(_sinks)
            enabled = _log_sink_enabled
            _emit_count += 1
        if enabled:
            try:
                if _logger.isEnabledFor(logging.DEBUG):
                    _logger.debug(json.dumps(event))
            except Exception as exc:
                _logger.warning("log_sink: default backend failed: %s", exc)
        for fn in sinks:
            try:
                fn(copy.deepcopy(event))
            except Exception as exc:
                _logger.warning("log_sink: sink %r raised: %s", fn, exc)
    except Exception as exc:
        _logger.warning("log_sink: unexpected emit failure: %s", exc)


def get_status() -> dict:
    """Return sink status snapshot."""
    with _lock:
        return {
            "sink_count": len(_sinks),
            "emit_count": _emit_count,
            "log_sink_enabled": _log_sink_enabled,
        }


def reset() -> None:
    """Reset all sink state. Intended for testing."""
    global _sinks, _emit_count, _log_sink_enabled
    with _lock:
        _sinks = []
        _emit_count = 0
        _log_sink_enabled = True
