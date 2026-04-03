"""Production monitor — tracks worker health metrics for rollout decisions.

Collects success rate, error rate, memory usage, and worker restart counts.
Thread-safe via threading.Lock.  No cross-module imports.
"""

import threading
import time
import logging

_lock = threading.Lock()

# Counters
_success_count = 0
_error_count = 0

# Worker restart tracking: list of timestamps (epoch seconds)
_restart_timestamps = []

# Snapshot of metrics from the previous rollout step (used for delta checks)
_baseline_success_rate = None

# Memory threshold in bytes (2 GB)
_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

# Rollback thresholds
_SUCCESS_RATE_DROP_THRESHOLD = 0.10  # 10%
_ERROR_RATE_THRESHOLD = 0.05  # 5%
_MAX_RESTARTS_PER_HOUR = 3


def record_success():
    """Record a successful task completion."""
    global _success_count
    with _lock:
        _success_count += 1


def record_error():
    """Record a task error."""
    global _error_count
    with _lock:
        _error_count += 1


def record_restart():
    """Record a worker restart event with current timestamp."""
    with _lock:
        now = time.time()
        _restart_timestamps.append(now)
        cutoff = now - 3600
        _restart_timestamps[:] = [ts for ts in _restart_timestamps if ts >= cutoff]


def get_success_rate():
    """Return the current success rate as a float in [0.0, 1.0].

    Returns 1.0 when no tasks have been processed yet.
    """
    with _lock:
        total = _success_count + _error_count
        if total == 0:
            return 1.0
        return _success_count / total


def get_error_rate():
    """Return the current error rate as a float in [0.0, 1.0].

    Returns 0.0 when no tasks have been processed yet.
    """
    with _lock:
        total = _success_count + _error_count
        if total == 0:
            return 0.0
        return _error_count / total


def get_memory_usage_bytes():
    """Return the current process RSS memory usage in bytes.

    Uses /proc/self/status on Linux.  Returns 0 on unsupported platforms.
    """
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # Value is in kB
                    return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        logging.getLogger(__name__).debug(
            "Failed to read VmRSS from /proc/self/status; returning 0", exc_info=True
        )
    return 0


def get_restarts_last_hour():
    """Return the number of worker restarts in the last 60 minutes."""
    cutoff = time.time() - 3600
    with _lock:
        return sum(1 for ts in _restart_timestamps if ts >= cutoff)


def save_baseline():
    """Snapshot the current success rate as the baseline for the next step."""
    global _baseline_success_rate
    with _lock:
        total = _success_count + _error_count
        if total == 0:
            _baseline_success_rate = 1.0
        else:
            _baseline_success_rate = _success_count / total


def get_baseline_success_rate():
    """Return the saved baseline success rate (or None if not yet saved)."""
    with _lock:
        return _baseline_success_rate


def get_metrics():
    """Return a snapshot of all current metrics as a dict."""
    with _lock:
        success_count = _success_count
        error_count = _error_count
        total = success_count + error_count
        success_rate = success_count / total if total > 0 else 1.0
        error_rate = error_count / total if total > 0 else 0.0
        cutoff = time.time() - 3600
        restarts_hour = sum(1 for ts in _restart_timestamps if ts >= cutoff)
        baseline = _baseline_success_rate

    return {
        "success_count": success_count,
        "error_count": error_count,
        "success_rate": success_rate,
        "error_rate": error_rate,
        "memory_usage_bytes": get_memory_usage_bytes(),
        "restarts_last_hour": restarts_hour,
        "baseline_success_rate": baseline,
    }


def check_rollback_needed():
    """Evaluate rollback conditions.

    Returns a list of triggered condition descriptions.  An empty list means
    all metrics are within acceptable thresholds.

    Rollback triggers:
      - Success rate dropped >10% from baseline
      - Error rate >5%
      - Memory >2 GB
      - Worker restarts >3 per hour
    """
    reasons = []
    metrics = get_metrics()

    # Check success-rate drop against baseline
    baseline = metrics["baseline_success_rate"]
    if baseline is not None:
        drop = baseline - metrics["success_rate"]
        if drop > _SUCCESS_RATE_DROP_THRESHOLD:
            reasons.append(
                f"success rate dropped {drop:.1%} from baseline {baseline:.1%}"
            )

    # Check absolute error rate
    if metrics["error_rate"] > _ERROR_RATE_THRESHOLD:
        reasons.append(f"error rate {metrics['error_rate']:.1%} exceeds 5%")

    # Check memory
    if metrics["memory_usage_bytes"] > _MEMORY_LIMIT_BYTES:
        mb = metrics["memory_usage_bytes"] / (1024 * 1024)
        reasons.append(f"memory usage {mb:.0f} MB exceeds 2048 MB")

    # Check restart frequency
    if metrics["restarts_last_hour"] > _MAX_RESTARTS_PER_HOUR:
        reasons.append(
            f"worker restarts {metrics['restarts_last_hour']} in last hour exceeds 3"
        )

    return reasons


def reset():
    """Reset all metrics.  Intended for testing."""
    global _success_count, _error_count, _restart_timestamps, _baseline_success_rate
    with _lock:
        _success_count = 0
        _error_count = 0
        _restart_timestamps = []
        _baseline_success_rate = None
