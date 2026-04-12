#!/usr/bin/env python3
"""Clean up stale browser profile directories.

Run daily via cron:
    0 2 * * * /path/to/venv/bin/python scripts/cleanup_browser_profiles.py

Environment variables:
    BROWSER_PROFILES_DIR  — override default browser_profiles/ directory
    MAX_PROFILE_AGE_DAYS  — profiles older than this are removed (default: 1)
"""
import logging
import os
import shutil
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    default_dir = str(_PROJECT_ROOT / "browser_profiles")
    profiles_dir = Path(os.getenv("BROWSER_PROFILES_DIR", default_dir))

    try:
        age_days = float(os.getenv("MAX_PROFILE_AGE_DAYS", "1"))
    except ValueError:
        _logger.error("Invalid MAX_PROFILE_AGE_DAYS; using default 1")
        age_days = 1.0

    if not profiles_dir.exists():
        _logger.warning("Browser profiles directory %s does not exist; skipping.", profiles_dir)
        return 0

    cutoff = time.time() - age_days * 86400
    removed = 0
    try:
        for entry in profiles_dir.iterdir():
            if not entry.is_dir():
                continue
            try:
                if os.stat(entry).st_mtime < cutoff:
                    shutil.rmtree(entry)
                    removed += 1
            except OSError as exc:
                _logger.warning("Could not remove %s: %s", entry, exc)
    except OSError as exc:
        _logger.error("Failed to scan %s: %s", profiles_dir, exc)
        return 1

    _logger.info("Removed %d stale browser profiles from %s", removed, profiles_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
