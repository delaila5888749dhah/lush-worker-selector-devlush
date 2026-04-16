"""Canonical threshold constants — single source of truth.

All modules MUST import from here, not redefine locally.
"""
from __future__ import annotations

from typing import Final

ERROR_RATE_THRESHOLD: Final[float] = 0.05   # >5% → scale down / alert
SUCCESS_RATE_MIN: Final[float] = 0.70       # <70% → do not scale up
RESTART_RATE_THRESHOLD: Final[int] = 3      # >3/hr → scale down / alert
SUCCESS_RATE_DROP_THRESHOLD: Final[float] = 0.10  # >10% drop from baseline
MAX_RESTARTS_PER_HOUR: Final[int] = RESTART_RATE_THRESHOLD  # alias
