"""CDP session-health helpers."""

from __future__ import annotations


def classify_session_loss(exc_or_text) -> str | None:
    """Return normalized session-loss reason, or None for unknown errors."""
    text = str(exc_or_text).lower()
    if "invalid session id" in text:
        return "invalid_session_id"
    # Selenium/ChromeDriver variants include prefixes such as
    # "invalid session id: session deleted as the browser has closed".
    if "browser has closed" in text:
        return "browser_connection_closed"
    # ChromeDriver and CDP surfaces vary the prefix before this stable suffix.
    if "not connected to devtools" in text:
        return "devtools_disconnected"
    if "target frame detached" in text:
        return "target_frame_detached"
    if "chrome-error://" in text or "err_connection_closed" in text:
        return "gateway_connection_closed"
    return None


def is_session_dead(driver, exc_or_text=None) -> bool:
    """Return True when *driver* or *exc_or_text* indicates a dead session."""
    if exc_or_text is not None and classify_session_loss(exc_or_text):
        return True
    try:
        raw = vars(driver).get("_driver", driver)
    except TypeError:
        raw = driver
    return getattr(raw, "session_id", object()) is None


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
