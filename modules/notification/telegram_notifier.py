"""Telegram Bot notifier for success/alert events (PR-4 hardened).

Features
--------
T-G2  Token-bucket rate limiter (default 5 msg/sec, capacity 2x rate).
T-G3  Background async sender thread — public ``send_*`` functions are
      non-blocking and only enqueue payloads.
T-G4  Exponential retry (1s / 2s / 4s) with JSONL persistent fallback
      on final failure, file-locked for concurrent safety.
T-G5  ``TELEGRAM_VERBOSE=1`` to emit non-success observability events.
T-G6  Rich success caption including proxy zip, billing state and
      cycle duration.
T-G7  Optional ``TELEGRAM_ALERT_CHAT_ID`` with fallback to default
      ``TELEGRAM_CHAT_ID``.

Env
---
TELEGRAM_ENABLED           gate (1/true/yes → enabled)
TELEGRAM_BOT_TOKEN         bot token
TELEGRAM_CHAT_ID           default chat id
TELEGRAM_ALERT_CHAT_ID     optional alert chat id (fallback to default)
TELEGRAM_RATE_LIMIT        msgs/sec (float, default 5)
TELEGRAM_VERBOSE           1/true/yes to enable verbose notifications
TELEGRAM_PENDING_FILE      JSONL path for failed payloads
                           (default: ``telegram_pending.jsonl``)
"""
import json
import logging
import os
import queue as _queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from modules.notification.card_masker import mask_card_number

_logger = logging.getLogger(__name__)
_API = "https://api.telegram.org"
_TRUTHY = {"1", "true", "yes"}


# ─────────────────────────────────────────────────────────────────────────────
# T-G2 — Token-bucket rate limiter
# ─────────────────────────────────────────────────────────────────────────────
class TokenBucket:
    """Thread-safe token bucket for outbound rate limiting.

    ``acquire`` blocks (via short sleeps) up to ``timeout`` seconds waiting
    for a token and returns ``True`` on success or ``False`` on timeout.
    """

    def __init__(self, rate_per_sec: float, capacity: int):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.rate = float(rate_per_sec)
        self.capacity = int(capacity)
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

    def acquire(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                self._refill_locked()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)


def _env_rate() -> float:
    try:
        return max(0.1, float(os.environ.get("TELEGRAM_RATE_LIMIT", "5")))
    except ValueError:
        return 5.0


_TG_RATE_LIMIT = _env_rate()
_TG_BUCKET = TokenBucket(
    rate_per_sec=_TG_RATE_LIMIT, capacity=max(1, int(_TG_RATE_LIMIT * 2))
)


# ─────────────────────────────────────────────────────────────────────────────
# Credentials / env helpers
# ─────────────────────────────────────────────────────────────────────────────
def _enabled() -> bool:
    return os.environ.get("TELEGRAM_ENABLED", "").strip().lower() in _TRUTHY


def _verbose() -> bool:
    return os.environ.get("TELEGRAM_VERBOSE", "").strip().lower() in _TRUTHY


def _credentials(channel: str = "default"):
    """Return (token, chat_id) or (None, None) when incomplete.

    channel == "alert" uses ``TELEGRAM_ALERT_CHAT_ID`` when set, otherwise
    falls back to ``TELEGRAM_CHAT_ID`` (T-G7).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if channel == "alert":
        chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
        if not chat_id:
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    else:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat_id) if (token and chat_id) else (None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level HTTPS POST
# ─────────────────────────────────────────────────────────────────────────────
def _post(url: str, data: bytes, headers=None, timeout: int = 10) -> bool:
    if not url.lower().startswith("https://"):
        _logger.warning("telegram_notifier: refusing non-HTTPS URL %r", url)
        return False
    try:
        req = urllib.request.Request(
            url, data=data, method="POST", headers=headers or {},
        )
        with urllib.request.urlopen(req, timeout=timeout):  # nosec B310  # noqa: S310
            pass
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _logger.warning(
            "telegram_notifier: POST %s failed: %s",
            url.rsplit("/", 1)[-1], exc,
        )
        return False


def _build_message_payload(token: str, chat_id: str, text: str) -> dict:
    return {
        "url": f"{_API}/bot{token}/sendMessage",
        "data": urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text}
        ).encode(),
        "headers": None,
    }


def _build_photo_payload(
    token: str, chat_id: str, photo: bytes, caption: str,
) -> dict:
    boundary = "----TelegramBoundary"
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
        f"filename=\"screenshot.png\"\r\nContent-Type: image/png\r\n\r\n"
    ).encode()
    body = head + photo + f"\r\n--{boundary}--\r\n".encode()
    return {
        "url": f"{_API}/bot{token}/sendPhoto",
        "data": body,
        "headers": {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# T-G4 — Retry with backoff + JSONL persistence
# ─────────────────────────────────────────────────────────────────────────────
_BACKOFFS = (1.0, 2.0, 4.0)


def _pending_file_path() -> str:
    return os.environ.get("TELEGRAM_PENDING_FILE", "telegram_pending.jsonl")


_pending_lock = threading.Lock()


def _persist_failed(payload: dict) -> None:
    """Append a failed payload to the JSONL pending file under a process lock."""
    try:
        record = {
            "ts": time.time(),
            "payload": {
                "url": payload.get("url"),
                # Avoid persisting raw bytes for photo payloads; they are not
                # usable for manual retry without out-of-band context.  Record
                # payload size so operators can spot oversized bodies.
                "data_len": len(payload.get("data") or b""),
                "headers": payload.get("headers"),
            },
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _pending_lock:
            with open(_pending_file_path(), "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as exc:
        _logger.warning("telegram_notifier: persist failed: %s", exc)


def _send_with_retry(payload: dict, max_attempts: int = 3) -> bool:
    attempts = max(1, min(max_attempts, len(_BACKOFFS)))
    for attempt in range(attempts):
        if _TG_STOP.is_set():
            # Graceful shutdown: do not continue retrying; persist and exit.
            _persist_failed(payload)
            return False
        if _TG_BUCKET.acquire():
            ok = _post(
                payload["url"], payload["data"], payload.get("headers"),
            )
            if ok:
                return True
        if attempt < attempts - 1:
            # Break sleep into short ticks so shutdown is responsive.
            deadline = time.monotonic() + _BACKOFFS[attempt]
            while time.monotonic() < deadline:
                if _TG_STOP.is_set():
                    _persist_failed(payload)
                    return False
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    _persist_failed(payload)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# T-G3 — Background async sender
# ─────────────────────────────────────────────────────────────────────────────
_TG_QUEUE: "_queue.Queue[dict]" = _queue.Queue(maxsize=1000)
_TG_SENDER_THREAD: threading.Thread | None = None
_TG_STOP = threading.Event()
_TG_THREAD_LOCK = threading.Lock()


def _sender_loop() -> None:
    while not _TG_STOP.is_set():
        try:
            payload = _TG_QUEUE.get(timeout=1.0)
        except _queue.Empty:
            continue
        try:
            _send_with_retry(payload)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("telegram_notifier: sender loop error: %s", exc)


def start_telegram_sender() -> None:
    """Idempotently start the background sender thread."""
    global _TG_SENDER_THREAD
    with _TG_THREAD_LOCK:
        if _TG_SENDER_THREAD is not None and _TG_SENDER_THREAD.is_alive():
            return
        _TG_STOP.clear()
        _TG_SENDER_THREAD = threading.Thread(
            target=_sender_loop, daemon=True, name="telegram-sender",
        )
        _TG_SENDER_THREAD.start()


def stop_telegram_sender(timeout: float = 5.0) -> None:
    """Signal the background sender thread to exit and wait briefly."""
    global _TG_SENDER_THREAD
    with _TG_THREAD_LOCK:
        thr = _TG_SENDER_THREAD
        _TG_STOP.set()
    if thr is not None:
        thr.join(timeout=timeout)
    with _TG_THREAD_LOCK:
        _TG_SENDER_THREAD = None


def _enqueue(payload: dict) -> bool:
    """Enqueue a payload; drop with warning when queue is full."""
    try:
        _TG_QUEUE.put_nowait(payload)
        # Lazy start so enqueuers don't need to worry about lifecycle order.
        start_telegram_sender()
        return True
    except _queue.Full:
        _logger.warning(
            "telegram_notifier: queue full (maxsize=%d); dropping message",
            _TG_QUEUE.maxsize,
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# T-G6 — Rich caption
# ─────────────────────────────────────────────────────────────────────────────
def build_success_caption(worker_id, task, total, ctx=None) -> str:
    """Return the full success caption. ``ctx`` is optional (CycleContext)."""
    card = getattr(getattr(task, "primary_card", None), "card_number", "") or ""
    recipient = getattr(task, "recipient_email", "") or ""
    billing = getattr(ctx, "billing_profile", None) if ctx is not None else None
    city = getattr(billing, "city", None)
    state = getattr(billing, "state", None)
    zip_code = getattr(billing, "zip_code", None)
    proxy_zip = getattr(ctx, "zip_code", None) if ctx is not None else None
    duration_fn = getattr(ctx, "duration_seconds", None) if ctx is not None else None
    try:
        duration = float(duration_fn()) if callable(duration_fn) else None
    except Exception:  # noqa: BLE001
        duration = None
    lines = [
        f"✅ SUCCESS — Worker {worker_id}",
        f"💰 Amount: ${total}",
        f"📧 Recipient: {recipient}",
        f"💳 Card: {mask_card_number(card)}",
    ]
    if billing is not None:
        lines.append(f"📍 Billing: {city}, {state} {zip_code}")
    lines.append(f"🌐 Proxy ZIP: {proxy_zip or 'N/A'}")
    if duration is not None:
        lines.append(f"⏱ Cycle: {duration:.1f}s")
    lines.append(f"🕐 {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def send_success_notification(
    worker_id: str, task, total, screenshot_bytes, ctx=None,
) -> bool:
    """Enqueue a success notification. **Non-blocking** — returns immediately."""
    if not _enabled():
        return False
    token, chat_id = _credentials("default")
    if token is None:
        _logger.warning(
            "telegram_notifier: TELEGRAM_BOT_TOKEN/CHAT_ID not set.",
        )
        return False
    try:
        caption = build_success_caption(worker_id, task, total, ctx=ctx)
        if screenshot_bytes is not None:
            payload = _build_photo_payload(
                token, chat_id, screenshot_bytes, caption,
            )
        else:
            payload = _build_message_payload(token, chat_id, caption)
        return _enqueue(payload)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("telegram_notifier: unexpected error: %s", exc)
        return False


def send_verbose_notification(message: str) -> bool:
    """T-G5 — Emit a verbose observability ping if ``TELEGRAM_VERBOSE`` is set."""
    if not _verbose() or not _enabled():
        return False
    token, chat_id = _credentials("default")
    if token is None:
        return False
    try:
        payload = _build_message_payload(token, chat_id, f"ℹ️ {message}")
        return _enqueue(payload)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("telegram_notifier: verbose enqueue failed: %s", exc)
        return False


def _send_alert_to_telegram(message: str) -> None:
    """Alert channel (T-G7) — honours ``TELEGRAM_ALERT_CHAT_ID`` when set."""
    if not _enabled():
        return
    token, chat_id = _credentials("alert")
    if token is None:
        return
    try:
        payload = _build_message_payload(token, chat_id, f"⚠️ ALERT: {message}")
        _enqueue(payload)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("telegram_notifier: alert forward failed: %s", exc)


def _flush_for_tests(timeout: float = 2.0) -> bool:
    """Test-only helper: block until the send queue is drained.

    Returns ``True`` when the queue is empty before ``timeout``, else ``False``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _TG_QUEUE.empty():
            # Give the sender a brief window to finish the in-flight item
            # (item was .get() out of the queue but not yet processed).
            time.sleep(0.05)
            if _TG_QUEUE.empty():
                return True
        time.sleep(0.01)
    return _TG_QUEUE.empty()


def _reset_for_tests() -> None:
    """Test-only helper: reset bucket + queue + sender-thread state."""
    global _TG_BUCKET
    # Signal stop then join — bounded retries ensure leaked worker threads
    # (e.g. from prior tests still inside _post or a retry sleep) fully
    # exit before the next test starts.
    for _ in range(4):
        stop_telegram_sender(timeout=3.0)
        with _TG_THREAD_LOCK:
            thr = _TG_SENDER_THREAD
        if thr is None or not thr.is_alive():
            break
    # Drain the queue.
    while True:
        try:
            _TG_QUEUE.get_nowait()
        except _queue.Empty:
            break
    # Recreate the bucket from the current env value so tests that mutate
    # TELEGRAM_RATE_LIMIT pick up the new rate.
    rate = _env_rate()
    _TG_BUCKET = TokenBucket(rate_per_sec=rate, capacity=max(1, int(rate * 2)))
    _TG_STOP.clear()


def register_as_alert_handler() -> None:
    """Register Telegram handler with ``modules.observability.alerting``."""
    try:
        from modules.observability import alerting  # noqa: PLC0415
    except ImportError as exc:
        _logger.warning("telegram_notifier: alerting unavailable: %s", exc)
        return
    alerting.register_alert_handler(_send_alert_to_telegram)
    start_telegram_sender()
