"""Telegram Bot notifier for success events.

Env vars required:
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_CHAT_ID    — channel/group/user ID to receive notifications
    TELEGRAM_ENABLED    — '1'/'true'/'yes' to enable (default: off)
"""
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from modules.notification.card_masker import mask_card_number

_logger = logging.getLogger(__name__)

_ENABLED_VALUES = {"1", "true", "yes"}
_TELEGRAM_API_BASE = "https://api.telegram.org"


def _is_telegram_enabled() -> bool:
    return os.environ.get("TELEGRAM_ENABLED", "").strip().lower() in _ENABLED_VALUES


def _open_https(req: urllib.request.Request, timeout: int):
    """Open a Telegram API request, asserting the scheme is HTTPS.

    Hardens against accidental scheme abuse (file://, custom schemes) flagged
    by Bandit B310.  Raises ValueError if the URL is not HTTPS.
    """
    full_url = req.full_url
    if not full_url.lower().startswith("https://"):
        raise ValueError(f"telegram_notifier: refusing non-HTTPS URL {full_url!r}")
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — scheme validated above


def _send_message(token: str, chat_id: str, text: str, timeout: int = 10) -> bool:
    """Send a plain-text message via Telegram Bot API."""
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with _open_https(req, timeout=timeout):
            pass
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _logger.warning("telegram_notifier: sendMessage failed: %s", exc)
        return False


def _send_photo(
    token: str,
    chat_id: str,
    photo_bytes: bytes,
    caption: str,
    timeout: int = 10,
) -> bool:
    """Send a photo with caption via Telegram Bot API using multipart/form-data."""
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendPhoto"
    boundary = "----TelegramBoundary"

    body_parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"screenshot.png\"\r\nContent-Type: image/png\r\n\r\n",
    ]
    body = b"".join(p.encode() for p in body_parts)
    body += photo_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    try:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with _open_https(req, timeout=timeout):
            pass
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _logger.warning("telegram_notifier: sendPhoto failed: %s", exc)
        return False


def send_success_notification(
    worker_id: str,
    task,
    total,
    screenshot_bytes: bytes | None,
) -> bool:
    """Send a success notification to Telegram (Blueprint §6 Ngã rẽ 2).

    Never raises — all errors are caught and logged as warnings.

    Args:
        worker_id: Identifier of the worker that completed the cycle.
        task: WorkerTask instance.
        total: Confirmed purchase total.
        screenshot_bytes: Optional PNG bytes of the confirmation screenshot.

    Returns:
        True if the notification was dispatched successfully, False otherwise.
    """
    if not _is_telegram_enabled():
        _logger.debug(
            "telegram_notifier: TELEGRAM_ENABLED is off — skipping notification."
        )
        return False

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        _logger.warning(
            "telegram_notifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
        )
        return False

    try:
        card_number = getattr(getattr(task, "primary_card", None), "card_number", "")
        recipient = getattr(task, "recipient_email", "")
        message = (
            f"✅ SUCCESS — Worker {worker_id}\n"
            f"💰 Amount: ${total}\n"
            f"📧 Recipient: {recipient}\n"
            f"💳 Card: {mask_card_number(card_number)}\n"
            f"🕐 Time: {datetime.utcnow().isoformat()}"
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("telegram_notifier: failed to build message: %s", exc)
        return False

    try:
        if screenshot_bytes is not None:
            return _send_photo(token, chat_id, screenshot_bytes, message)
        return _send_message(token, chat_id, message)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("telegram_notifier: unexpected error: %s", exc)
        return False


def _send_alert_to_telegram(message: str) -> None:
    """Forward an alert message as a plain Telegram message (non-interference)."""
    if not _is_telegram_enabled():
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        _send_message(token, chat_id, f"⚠️ ALERT: {message}")
    except Exception as exc:  # noqa: BLE001
        _logger.warning("telegram_notifier: alert forward failed: %s", exc)


def register_as_alert_handler() -> None:
    """Register Telegram as an alert handler via the alerting module.

    The import is guarded so this module remains usable in environments where
    the optional ``modules.observability`` package is unavailable.
    """
    try:
        from modules.observability import alerting  # noqa: PLC0415
    except ImportError as exc:
        _logger.warning(
            "telegram_notifier: cannot register alert handler "
            "(modules.observability unavailable): %s",
            exc,
        )
        return
    alerting.register_alert_handler(_send_alert_to_telegram)
