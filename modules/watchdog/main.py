import threading

from modules.common.exceptions import SessionFlaggedError

_lock = threading.Lock()
_monitor_enabled = False
_total_event = threading.Event()
_total_value = None


def enable_network_monitor():
    global _monitor_enabled, _total_value
    with _lock:
        _monitor_enabled = True
        _total_value = None
        _total_event.clear()


def wait_for_total(timeout):
    global _monitor_enabled
    try:
        with _lock:
            if not _monitor_enabled:
                raise RuntimeError("Network monitor is not enabled")

        received = _total_event.wait(timeout=timeout)

        with _lock:
            if not received:
                raise SessionFlaggedError(
                    f"Timeout ({timeout}s) waiting for total amount"
                )
            return _total_value
    finally:
        _reset_monitor()


def _notify_total(value):
    global _total_value
    with _lock:
        _total_value = value
        _total_event.set()


def _reset_monitor():
    global _monitor_enabled, _total_value
    with _lock:
        _monitor_enabled = False
        _total_value = None
        _total_event.clear()
