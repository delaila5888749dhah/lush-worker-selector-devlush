#!/usr/bin/env python3
"""Backup the billing pool .txt files to a timestamped directory.

Run daily via cron:
    0 3 * * * /path/to/venv/bin/python scripts/backup_billing_pool.py

Environment variables:
    BILLING_POOL_DIR    — source directory (default: billing_pool/)
    BILLING_BACKUP_DIR  — backup root directory (default: backups/billing_pool/)
    MAX_BACKUPS         — maximum number of backup snapshots to retain (default: 7)
"""
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    src = Path(os.getenv("BILLING_POOL_DIR", str(_PROJECT_ROOT / "billing_pool")))
    backup_root = Path(os.getenv("BILLING_BACKUP_DIR",
                                   str(_PROJECT_ROOT / "backups" / "billing_pool")))

    try:
        max_backups = int(os.getenv("MAX_BACKUPS", "7"))
    except ValueError:
        _logger.error("Invalid MAX_BACKUPS; using default 7")
        max_backups = 7

    if not src.exists():
        _logger.warning("Source directory %s does not exist; skipping backup.", src)
        return 0

    txt_files = list(src.glob("*.txt"))
    if not txt_files:
        _logger.warning("No .txt files found in %s; skipping backup.", src)
        return 0

    dest = backup_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _logger.error("Cannot create backup directory %s: %s", dest, exc)
        return 1

    total_size = 0
    count = 0
    for f in txt_files:
        try:
            shutil.copy2(f, dest / f.name)
            total_size += f.stat().st_size
            count += 1
        except OSError as exc:
            _logger.warning("Could not copy %s: %s", f, exc)

    _logger.info("Backed up %d files (%.1f KB) to %s", count, total_size / 1024, dest)

    try:
        backups = sorted((p for p in backup_root.iterdir() if p.is_dir()), key=lambda p: p.name)
        while len(backups) > max_backups:
            oldest = backups.pop(0)
            try:
                shutil.rmtree(oldest)
                _logger.info("Removed old backup %s", oldest.name)
            except OSError as exc:
                _logger.warning("Could not remove old backup %s: %s", oldest, exc)
    except OSError as exc:
        _logger.warning("Could not prune old backups: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
