"""FileTaskLoader — thread-safe task reader from a pipe-delimited input file.

Input format (one task per line):
    recipient_email|amount|card_number|exp_month|exp_year|cvv[|card2_number|exp2_month|exp2_year|cvv2|...]

Lines starting with ``#`` and empty lines are skipped.
Malformed lines are logged as warnings and skipped.

Env vars:
    TASK_INPUT_FILE  — path to the input file (default: ``tasks/input.txt``)
"""
from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

from modules.common.types import CardInfo, WorkerTask

_logger = logging.getLogger(__name__)

_DEFAULT_TASK_FILE = "tasks/input.txt"
# Number of fields in one card block: card_number, exp_month, exp_year, cvv
_CARD_BLOCK_SIZE = 4
# Fields before the first card block: recipient_email, amount
_PREFIX_FIELDS = 2


class FileTaskLoader:
    """Thread-safe loader that reads WorkerTask objects from a flat text file.

    Multiple workers can call ``get_task()`` concurrently; a ``threading.Lock``
    ensures each line is delivered to exactly one worker.
    """

    def __init__(self, file_path: Optional[str] = None) -> None:
        self._file_path = file_path or os.environ.get(
            "TASK_INPUT_FILE", _DEFAULT_TASK_FILE
        )
        self._lock = threading.Lock()
        self._lines: List[str] = []
        self._index: int = 0
        self._loaded = False

    def _load(self) -> None:
        """Load and filter lines from the input file (called once, lazily)."""
        try:
            with open(self._file_path, encoding="utf-8") as fh:
                raw = fh.readlines()
        except OSError as exc:
            _logger.error(
                "FileTaskLoader: cannot open task file %r: %s", self._file_path, exc
            )
            self._lines = []
            return

        kept = []
        for line in raw:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            kept.append(stripped)
        self._lines = kept
        _logger.info(
            "FileTaskLoader: loaded %d task(s) from %r", len(kept), self._file_path
        )

    def _parse_line(self, line: str) -> Optional[WorkerTask]:
        """Parse a single pipe-delimited line into a WorkerTask.

        Returns None and logs a warning on malformed input.
        """
        parts = line.split("|")

        # Minimum: email + amount + one card block (card_number, exp_month, exp_year, cvv) = 6 fields
        min_fields = _PREFIX_FIELDS + _CARD_BLOCK_SIZE
        if len(parts) < min_fields:
            _logger.warning(
                "FileTaskLoader: malformed line (expected ≥6 fields, got %d): %r",
                len(parts),
                line,
            )
            return None

        recipient_email = parts[0].strip()
        amount_raw = parts[1].strip()

        try:
            amount = int(amount_raw)
        except ValueError:
            _logger.warning(
                "FileTaskLoader: malformed amount %r in line: %r", amount_raw, line
            )
            return None

        if amount <= 0:
            _logger.warning(
                "FileTaskLoader: non-positive amount %d in line: %r", amount, line
            )
            return None

        if not recipient_email:
            _logger.warning(
                "FileTaskLoader: empty recipient_email in line: %r", line
            )
            return None

        # Parse primary card: parts[2..5]
        card_fields = parts[2:]
        if len(card_fields) < 4:
            _logger.warning(
                "FileTaskLoader: incomplete primary card fields in line: %r", line
            )
            return None

        primary_card = CardInfo(
            card_number=card_fields[0].strip(),
            exp_month=card_fields[1].strip(),
            exp_year=card_fields[2].strip(),
            cvv=card_fields[3].strip(),
            card_name="",  # orchestrator/billing injects card_name from profile
        )

        # Parse additional cards for order_queue: groups of 4 fields each
        extra_fields = card_fields[4:]
        order_queue: List[CardInfo] = []
        while len(extra_fields) >= 4:
            card = CardInfo(
                card_number=extra_fields[0].strip(),
                exp_month=extra_fields[1].strip(),
                exp_year=extra_fields[2].strip(),
                cvv=extra_fields[3].strip(),
                card_name="",
            )
            order_queue.append(card)
            extra_fields = extra_fields[4:]

        if extra_fields:
            _logger.warning(
                "FileTaskLoader: %d trailing field(s) after last complete card block "
                "in line (ignored): %r",
                len(extra_fields),
                line,
            )

        try:
            return WorkerTask(
                recipient_email=recipient_email,
                amount=amount,
                primary_card=primary_card,
                order_queue=tuple(order_queue),
            )
        except (ValueError, TypeError) as exc:
            _logger.warning(
                "FileTaskLoader: WorkerTask construction failed for line %r: %s",
                line,
                exc,
            )
            return None

    def get_task(self, worker_id: str) -> Optional[WorkerTask]:  # noqa: ARG002
        """Return the next WorkerTask from the file, or None when exhausted.

        Thread-safe: multiple workers can call concurrently.

        Args:
            worker_id: Identifier of the calling worker (used for logging only).

        Returns:
            A ``WorkerTask`` instance, or ``None`` if no tasks remain.
        """
        with self._lock:
            if not self._loaded:
                self._load()
                self._loaded = True

            while self._index < len(self._lines):
                line = self._lines[self._index]
                self._index += 1
                task = self._parse_line(line)
                if task is not None:
                    _logger.debug(
                        "FileTaskLoader: dispatching task to worker=%s "
                        "(line_index=%d, email=%s)",
                        worker_id,
                        self._index,
                        task.recipient_email,
                    )
                    return task

        _logger.info(
            "FileTaskLoader: no more tasks for worker=%s (file exhausted).", worker_id
        )
        return None
