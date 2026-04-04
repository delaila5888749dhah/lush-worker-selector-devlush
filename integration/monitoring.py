"""Production monitoring setup and validation.

Provides :func:`setup_monitoring` to configure the Python logging stack for
production use and :func:`validate_monitoring` to verify the three monitoring
acceptance criteria at runtime:

1. Logging is active and emitting structured events.
2. ``trace_id`` is assigned and trackable.
3. ``monitor.get_metrics()`` returns non-``None`` data in normal operation.
"""

import logging
import re

from integration import runtime
from modules.monitor import main as monitor

_LOG_FORMAT = (
    "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
)

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{12}$")

_configured = False


def setup_monitoring(level=logging.INFO):
    """Configure the Python logging stack for production use.

    Adds a :class:`logging.StreamHandler` with a structured format to the root
    logger if not already configured.  Safe to call more than once — repeated
    calls are no-ops.

    Parameters
    ----------
    level : int
        Logging level for the root logger (default ``logging.INFO``).
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def validate_monitoring():
    """Validate production monitoring acceptance criteria.

    Returns a dict with:

    * ``passed`` (bool) — ``True`` when all three criteria are met.
    * ``checks`` (dict) — Individual check results keyed by name.
    * ``errors`` (list[str]) — Human-readable failure reasons.

    The three criteria checked are:

    1. **logging_active** — the ``integration.runtime`` logger has at least
       one handler reachable (directly or via a parent logger).
    2. **trace_id_valid** — ``get_trace_id()`` returns a 12-char hex string
       (only valid while the runtime is ``RUNNING``).
    3. **metrics_available** — ``monitor.get_metrics()`` returns a non-``None``
       dict containing the documented metric keys.
    """
    errors: list[str] = []

    # 1. Logging active
    logger = logging.getLogger("integration.runtime")
    logging_active = logger.hasHandlers()
    if not logging_active:
        errors.append("No logging handlers configured for integration.runtime")

    # 2. trace_id valid
    trace_id = runtime.get_trace_id()
    trace_id_valid = isinstance(trace_id, str) and bool(_TRACE_ID_RE.match(trace_id))
    if trace_id is None:
        errors.append("trace_id is None (runtime may not be started)")
    elif not trace_id_valid:
        errors.append(f"trace_id format invalid: {trace_id!r}")

    # 3. Metrics available
    metrics_available = False
    try:
        metrics = monitor.get_metrics()
        if metrics is not None:
            required_keys = {
                "success_count", "error_count", "success_rate",
                "error_rate", "memory_usage_bytes", "restarts_last_hour",
                "baseline_success_rate",
            }
            missing = required_keys - set(metrics.keys())
            if missing:
                errors.append(f"Metrics missing keys: {missing}")
            else:
                metrics_available = True
        else:
            errors.append("monitor.get_metrics() returned None")
    except Exception as exc:
        errors.append(f"monitor.get_metrics() raised: {exc}")

    return {
        "passed": logging_active and trace_id_valid and metrics_available,
        "checks": {
            "logging_active": logging_active,
            "trace_id_valid": trace_id_valid,
            "metrics_available": metrics_available,
        },
        "errors": errors,
    }


def reset():
    """Reset configuration state.  Intended for testing."""
    global _configured
    _configured = False
