"""Persistent JSONL queue for failed Telegram payloads (PR-4).

The Telegram notifier (``modules.notification.telegram_notifier``) already
appends to ``telegram_pending.jsonl`` when a payload exhausts its retry
budget.  This module exposes helpers to **read, drain and re-queue** those
entries for manual retry.

Concurrency contract:
* Within a single process, file operations are serialised by the module
  ``threading.Lock``; writers (``enqueue_failed``) use append-mode so
  partially-written records cannot be observed.
* Across multiple processes, ``enqueue_failed`` remains safe as long as
  each JSON record fits on one line (O_APPEND atomicity for small writes
  on POSIX).  ``drain`` is **single-process only** — concurrent drains
  from multiple processes can race on the final ``remove`` and lose
  records; callers are expected to run drain as an operator task.
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
            # Truncate atomically; within a single process this is correct.
            # Across multiple processes, a concurrent ``enqueue_failed`` can
            # still race with this ``remove``; the persistent queue contract
            # is therefore single-process for drain operations.
            os.remove(target)
        except OSError as exc:
            _logger.warning("notification.queue: drain failed: %s", exc)
    return records
