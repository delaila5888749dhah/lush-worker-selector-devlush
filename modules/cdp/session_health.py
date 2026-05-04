"""CDP session-health helpers."""

from __future__ import annotations


def classify_session_loss(exc_or_text) -> str | None:
    """Return normalized session-loss reason, or None for unknown errors."""
    text = str(exc_or_text).lower()
    if "invalid session id" in text:
        return "invalid_session_id"
    if "browser has closed the connection" in text:
        return "browser_connection_closed"
    if "disconnected: not connected to devtools" in text:
        return "devtools_disconnected"
    if "target frame detached" in text:
        return "target_frame_detached"
    if "chrome-error://" in text or "err_connection_closed" in text:
        return "gateway_connection_closed"
    return None


def session_alive(driver) -> bool:
    """Lightweight CDP probe to verify the browser session is still attached."""
    raw = getattr(driver, "_driver", driver)
    try:
        raw.execute_cdp_cmd(
            "Runtime.evaluate",
            {"expression": "1", "returnByValue": True},
        )
        return True
    except Exception:  # noqa: BLE001  # pylint: disable=broad-except
        return False
