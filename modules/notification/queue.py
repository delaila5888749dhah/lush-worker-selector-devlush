"""Persistent JSONL queue for failed Telegram payloads (PR-4).

The Telegram notifier (``modules.notification.telegram_notifier``) already
appends to ``telegram_pending.jsonl`` when a payload exhausts its retry
budget.  This module exposes helpers to **read, drain and re-queue** those
entries for manual retry.  File operations are protected by a
``threading.Lock`` so concurrent callers within the same process do not
interleave writes, and each call opens the file in atomic append / read
modes (O_APPEND semantics) so multiple processes remain correct as long
as records stay on a single line (JSONL invariant).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

_logger = logging.getLogger(__name__)
_DEFAULT_FILE = "telegram_pending.jsonl"
_lock = threading.Lock()


def _pending_path() -> str:
    return os.environ.get("TELEGRAM_PENDING_FILE", _DEFAULT_FILE)


def enqueue_failed(payload: dict, path: str | None = None) -> bool:
    """Append ``payload`` as a JSONL record to the persistent failure queue.

    Returns ``True`` on success.  Never raises — file-system errors are
    logged as warnings and suppressed.
    """
    target = path or _pending_path()
    record = {"ts": time.time(), "payload": payload}
    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    except (TypeError, ValueError) as exc:
        _logger.warning("notification.queue: serialise failed: %s", exc)
        return False
    try:
        with _lock:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except OSError as exc:
        _logger.warning("notification.queue: append failed: %s", exc)
        return False


def read_pending(path: str | None = None) -> list[dict]:
    """Return all records currently persisted to the failure queue."""
    target = path or _pending_path()
    records: list[dict] = []
    try:
        with _lock:
            if not os.path.exists(target):
                return records
            with open(target, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        _logger.warning(
                            "notification.queue: skipping malformed line",
                        )
    except OSError as exc:
        _logger.warning("notification.queue: read failed: %s", exc)
    return records


def drain(path: str | None = None) -> list[dict]:
    """Read and remove all pending records. Returns the records drained."""
    target = path or _pending_path()
    with _lock:
        records = []
        if not os.path.exists(target):
            return records
        try:
            with open(target, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        _logger.warning(
                            "notification.queue: skipping malformed line",
                        )
            # Truncate atomically; on non-POSIX systems this is still
            # best-effort but good enough for the single-process contract.
            os.remove(target)
        except OSError as exc:
            _logger.warning("notification.queue: drain failed: %s", exc)
    return records
