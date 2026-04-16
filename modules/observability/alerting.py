"""Alerting rules — evaluate metric thresholds and dispatch alert messages (Ext-2).

Thread-safe via threading.Lock. Stdlib only. No cross-module imports.
Default alert backend: structured WARNING log via Python logging.
Custom backends can be registered via register_alert_handler() / unregister_alert_handler().
"""
import logging
import threading
from modules.common.thresholds import (
    ERROR_RATE_THRESHOLD,
    RESTART_RATE_THRESHOLD,
    SUCCESS_RATE_DROP_THRESHOLD,
)

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_alert_handlers: list = []  # list of callable(message: str) -> None
_alert_count: int = 0
_handler_failure_count: int = 0
_log_alert_enabled = True


def evaluate_alerts(metrics: dict) -> list[str]:
    """Evaluate metric thresholds and return a list of alert message strings.

    Returns an empty list if no thresholds are exceeded.
    Never raises — exceptions are caught and logged as warnings.
    """
    alerts = []
    try:
        error_rate = metrics.get("error_rate")
        if error_rate is not None and error_rate > ERROR_RATE_THRESHOLD:
            alerts.append(
                f"error_rate={error_rate:.1%} exceeds threshold {ERROR_RATE_THRESHOLD:.0%}"
            )
        restarts = metrics.get("restarts_last_hour")
        if restarts is not None and restarts > RESTART_RATE_THRESHOLD:
            alerts.append(
                f"restarts_last_hour={restarts} exceeds threshold {RESTART_RATE_THRESHOLD}"
            )
        baseline = metrics.get("baseline_success_rate")
        success_rate = metrics.get("success_rate")
        if (
            baseline is not None
            and success_rate is not None
            and success_rate < baseline - SUCCESS_RATE_DROP_THRESHOLD
        ):
            alerts.append(
                f"success_rate dropped {baseline - success_rate:.1%} from baseline {baseline:.1%}"
            )
    except Exception as exc:
        _logger.warning("alerting: evaluate_alerts failed: %s", exc)
    return alerts


def send_alert(message: str) -> None:
    """Dispatch an alert message to all registered backends.

    Default backend: WARNING log via Python logging.
    Custom backends registered via register_alert_handler() are called in order.
    Exceptions from individual handlers are caught and logged as warnings.
    Never raises.
    """
    global _alert_count, _handler_failure_count
    try:
        with _lock:
            handlers = list(_alert_handlers)
            log_enabled = _log_alert_enabled
            _alert_count += 1
        if log_enabled:
            _logger.warning("ALERT: %s", message)
        failures = 0
        for fn in handlers:
            try:
                fn(message)
            except Exception as exc:
                failures += 1
                _logger.warning("alerting: handler %r raised: %s", fn, exc)
        if failures:
            with _lock:
                _handler_failure_count += failures
    except Exception as exc:
        _logger.warning("alerting: send_alert failed: %s", exc)


def register_alert_handler(fn) -> None:
    """Register a custom alert handler callable(message: str) -> None."""
    with _lock:
        _alert_handlers.append(fn)


def unregister_alert_handler(fn) -> bool:
    """Remove a previously registered alert handler. Returns True if found."""
    with _lock:
        try:
            _alert_handlers.remove(fn)
            return True
        except ValueError:
            return False


def set_log_alert_enabled(enabled: bool) -> None:
    """Enable or disable the default log-based alert backend."""
    global _log_alert_enabled
    with _lock:
        _log_alert_enabled = enabled


def get_status() -> dict:
    """Return alerting status snapshot."""
    with _lock:
        return {
            "handler_count": len(_alert_handlers),
            "alert_count": _alert_count,
            "handler_failure_count": _handler_failure_count,
            "log_alert_enabled": _log_alert_enabled,
        }


def reset() -> None:
    """Reset all alerting state. Intended for testing."""
    global _alert_handlers, _alert_count, _handler_failure_count, _log_alert_enabled
    with _lock:
        _alert_handlers = []
        _alert_count = 0
        _handler_failure_count = 0
        _log_alert_enabled = True
