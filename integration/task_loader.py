"""FileTaskLoader: thread-safe pipe-delimited task reader.

Format: recipient_email|amount|card|exp_m|exp_y|cvv[|card2|m|y|cvv2|...]
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import List, Optional

from modules.common.types import CardInfo, WorkerTask

_logger = logging.getLogger(__name__)
_DEFAULT_TASK_FILE = "tasks/input.txt"
_CARD_BLOCK = 4  # card_number, exp_month, exp_year, cvv
_PREFIX = 2  # recipient_email, amount

# Card/exp/cvv validators (Blueprint §1 input format).
# _RE_CARD: 15 or 16 contiguous digits (spaces/dashes stripped before match).
_RE_CARD = re.compile(r"^\d{15,16}$")
# _RE_EXP_MONTH: 1-9, 01-09, or 10-12 (month of year).
_RE_EXP_MONTH = re.compile(r"^(0?[1-9]|1[0-2])$")
# _RE_EXP_YEAR: two-digit YY or four-digit YYYY.
_RE_EXP_YEAR = re.compile(r"^\d{2}$|^\d{4}$")
# _RE_CVV: 3 or 4 digits (3 for most brands, 4 for AmEx).
_RE_CVV = re.compile(r"^\d{3,4}$")
# _RE_EMAIL: minimal email shape — local@domain.tld with no whitespace/@ in parts.
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _make_card(fields: List[str]) -> CardInfo:
    """Validate and build a :class:`CardInfo` from ``fields``.

    Raises :class:`ValueError` with a *privacy-safe* message (never includes
    raw card digits) if any field fails regex validation.
    """
    card_number, exp_m, exp_y, cvv = fields[0], fields[1], fields[2], fields[3]
    card_clean = card_number.replace(" ", "").replace("-", "").strip()
    if not _RE_CARD.match(card_clean):
        raise ValueError(
            f"Invalid card number length/format: {len(card_clean)} digits "
            "(expected 15 or 16)"
        )
    exp_m_s = exp_m.strip()
    if not _RE_EXP_MONTH.match(exp_m_s):
        raise ValueError("Invalid exp_month format (expected 01-12)")
    exp_y_s = exp_y.strip()
    if not _RE_EXP_YEAR.match(exp_y_s):
        raise ValueError("Invalid exp_year format (expected YY or YYYY)")
    cvv_s = cvv.strip()
    if not _RE_CVV.match(cvv_s):
        raise ValueError("Invalid CVV: expected 3-4 digits")
    return CardInfo(
        card_number=card_clean,
        exp_month=exp_m_s.zfill(2),
        exp_year=exp_y_s,
        cvv=cvv_s,
        card_name="",
    )


class FileTaskLoader:
    """Thread-safe loader; multiple workers may call ``get_task`` concurrently."""

    def __init__(self, file_path: Optional[str] = None) -> None:
        self._file_path = file_path or os.environ.get("TASK_INPUT_FILE", _DEFAULT_TASK_FILE)
        self._lock = threading.Lock()
        self._lines: List[str] = []
        self._index = 0
        self._loaded = False

    def _load(self) -> None:
        try:
            with open(self._file_path, encoding="utf-8") as fh:
                raw = fh.readlines()
        except OSError as exc:
            _logger.error("FileTaskLoader: cannot open %r: %s", self._file_path, exc)
            self._lines = []
            return
        self._lines = [s for s in (ln.strip() for ln in raw) if s and not s.startswith("#")]
        _logger.info("FileTaskLoader: loaded %d task(s) from %r", len(self._lines), self._file_path)

    def _parse_line(self, line: str, line_no: int = 0) -> Optional[WorkerTask]:
        parts = line.split("|")
        if len(parts) < _PREFIX + _CARD_BLOCK:
            _logger.warning(
                "FileTaskLoader: malformed line %d (expected at least %d fields, got %d)",
                line_no, _PREFIX + _CARD_BLOCK, len(parts),
            )
            return None
        recipient = parts[0].strip()
        try:
            amount = int(parts[1].strip())
        except ValueError:
            _logger.warning("FileTaskLoader: bad amount on line %d", line_no)
            return None
        if amount <= 0 or not recipient or not _RE_EMAIL.match(recipient):
            _logger.warning(
                "FileTaskLoader: invalid email/amount on line %d", line_no
            )
            return None
        cards = parts[2:]
        try:
            primary = _make_card(cards[:_CARD_BLOCK])
            extras: List[CardInfo] = []
            i = _CARD_BLOCK
            while i + _CARD_BLOCK <= len(cards):
                extras.append(_make_card(cards[i:i + _CARD_BLOCK]))
                i += _CARD_BLOCK
        except ValueError as exc:
            # Privacy: *exc* message is crafted in _make_card to exclude raw
            # card digits.  Log line number only, never the raw line (which
            # would echo the PAN).
            _logger.warning(
                "FileTaskLoader: skipping line %d due to card validation: %s",
                line_no, exc,
            )
            return None
        try:
            return WorkerTask(recipient_email=recipient, amount=amount,
                              primary_card=primary, order_queue=tuple(extras))
        except (ValueError, TypeError) as exc:
            _logger.warning(
                "FileTaskLoader: WorkerTask build failed on line %d: %s",
                line_no, exc,
            )
            return None

    def get_task(self, worker_id: str) -> Optional[WorkerTask]:  # noqa: ARG002
        """Return the next WorkerTask, or None when exhausted. Thread-safe."""
        with self._lock:
            if not self._loaded:
                self._load()
                self._loaded = True
            while self._index < len(self._lines):
                line = self._lines[self._index]
                self._index += 1
                task = self._parse_line(line, line_no=self._index)
                if task is not None:
                    return task
        return None
