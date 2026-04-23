"""Production monitor — tracks worker health metrics for rollout decisions.

Collects success rate, error rate, memory usage, and worker restart counts.
Thread-safe via threading.Lock.  No cross-module imports.
"""

import sys
import threading
import time
import logging
from modules.common.thresholds import (
    ERROR_RATE_THRESHOLD,
    SUCCESS_RATE_DROP_THRESHOLD,
    MAX_RESTARTS_PER_HOUR,
)

_lock = threading.Lock()
_logger = logging.getLogger(__name__)

# Counters
_success_count = 0
_error_count = 0
# Per-persona breakdown (optional tag — backward compatible)
_error_counts_by_persona: dict[str, int] = {}
_success_counts_by_persona: dict[str, int] = {}

# Worker restart tracking: list of timestamps (epoch seconds)
_restart_timestamps = []

# UI-lock retry metrics — counters for UI-lock auto-recovery (#... [MINOR]).
# Incremented by the orchestrator's UI-lock retry loop (integration/orchestrator.py).
_ui_lock_retry_count = 0
_ui_lock_recovered_count = 0
_ui_lock_exhausted_count = 0
# Count of active-poll VBV / 3DS iframe detections (Blueprint §6 Fork 3).
# Incremented internally by :class:`TransientMonitor`; surfaced to callers as
# the ``vbv_detections`` key of :func:`get_metrics` (no new public function
# is added, preserving the spec/interface.md contract — A4).
_vbv_detections = 0

# Snapshot of metrics from the previous rollout step (used for delta checks)
_baseline_success_rate = None

# Memory threshold in bytes (2 GB)
_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024


def record_success(persona_type: str | None = None) -> None:
    """Record a successful task completion.

    Args:
        persona_type: Optional persona type tag for per-persona breakdown.
            Passing None (default) preserves backward compatibility.
    """
    global _success_count
    with _lock:
        _success_count += 1
        if persona_type:
            _success_counts_by_persona[persona_type] = (
                _success_counts_by_persona.get(persona_type, 0) + 1
            )


def record_error(persona_type: str | None = None) -> None:
    """Record a task error.

    Args:
        persona_type: Optional persona type tag for per-persona breakdown.
            Passing None (default) preserves backward compatibility.
    """
    global _error_count
    with _lock:
        _error_count += 1
        if persona_type:
            _error_counts_by_persona[persona_type] = (
                _error_counts_by_persona.get(persona_type, 0) + 1
            )


def _record_vbv_detection() -> None:
    """Record a VBV/3DS iframe detection event (module-private).

    Incremented each time :class:`TransientMonitor` observes a late-appearing
    3D-Secure challenge iframe.  Exposed to callers via the
    ``vbv_detections`` key of :func:`get_metrics`; no new public function is
    added to the module surface, preserving the ``spec/interface.md``
    contract (A4).
    """
    global _vbv_detections
    with _lock:
        _vbv_detections += 1


def _get_vbv_detections() -> int:
    """Return the total number of VBV/3DS iframe detections (module-private)."""
    with _lock:
        return _vbv_detections


def record_restart():
    """Record a worker restart event with current timestamp."""
    with _lock:
        now = time.time()
        _restart_timestamps.append(now)
        cutoff = now - 3600
        _restart_timestamps[:] = [ts for ts in _restart_timestamps if ts >= cutoff]


def record_ui_lock_retry() -> None:
    """Record a UI-lock focus-shift retry attempt."""
    global _ui_lock_retry_count
    with _lock:
        _ui_lock_retry_count += 1


def record_ui_lock_recovered() -> None:
    """Record a successful UI-lock recovery (page state cleared after retry)."""
    global _ui_lock_recovered_count
    with _lock:
        _ui_lock_recovered_count += 1


def record_ui_lock_exhausted() -> None:
    """Record a UI-lock retry budget exhaustion (lock persists past the cap)."""
    global _ui_lock_exhausted_count
    with _lock:
        _ui_lock_exhausted_count += 1


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


def get_error_rates_by_persona() -> dict[str, float]:
    """Return per-persona error rates as {persona_type: rate}.

    Only includes persona types for which at least one event has been recorded.
    Returns an empty dict if no tagged events have been recorded.
    """
    with _lock:
        all_types = set(list(_error_counts_by_persona) + list(_success_counts_by_persona))
        result: dict[str, float] = {}
        for pt in all_types:
            errors = _error_counts_by_persona.get(pt, 0)
            successes = _success_counts_by_persona.get(pt, 0)
            total = errors + successes
            result[pt] = (errors / total) if total > 0 else 0.0
        return result


def get_memory_usage_bytes() -> int:
    """Return current process RSS memory in bytes.

    Tries psutil first (cross-platform), then /proc/self/status (Linux),
    then resource module (macOS). Returns 0 with a warning if all fail.
    """
    # Method 1: psutil (most accurate, cross-platform)
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        pass
    except Exception as e:
        _logger.debug("psutil memory check failed: %s", e)

    # Method 2: Linux /proc/self/status
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, IndexError, ValueError) as e:
            _logger.debug("Failed to read VmRSS from /proc/self/status: %s", e)

    # Method 3: macOS resource module
    if sys.platform == "darwin":
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except Exception as e:
            _logger.debug("resource.getrusage failed: %s", e)

    _logger.warning(
        "Cannot determine memory usage on platform %r; "
        "memory rollback threshold will not trigger. "
        "Install psutil for cross-platform support.",
        sys.platform,
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
    """Return a consistent snapshot of all current metrics as a dict.

    All fields — including memory usage — are collected while ``_lock`` is
    held so that the returned dict represents a single coherent point in
    time.  ``get_memory_usage_bytes()`` does not acquire ``_lock`` itself,
    so holding the lock during the call is safe (no deadlock risk).

    When memory cannot be determined on the current platform,
    ``memory_usage_bytes`` is 0.  Callers that need to distinguish
    "zero RSS" from "unavailable" should treat 0 as the degraded sentinel.
    """
    with _lock:
        mem = get_memory_usage_bytes()
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
            "memory_usage_bytes": mem,
            "restarts_last_hour": restarts_hour,
            "baseline_success_rate": baseline,
            "error_counts_by_persona": dict(_error_counts_by_persona),
            "success_counts_by_persona": dict(_success_counts_by_persona),
            "ui_lock_retry_count": _ui_lock_retry_count,
            "ui_lock_recovered_count": _ui_lock_recovered_count,
            "ui_lock_exhausted_count": _ui_lock_exhausted_count,
            "vbv_detections": _vbv_detections,
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
        if drop > SUCCESS_RATE_DROP_THRESHOLD:
            reasons.append(
                f"success rate dropped {drop:.1%} from baseline {baseline:.1%}"
            )

    # Check absolute error rate
    if metrics["error_rate"] > ERROR_RATE_THRESHOLD:
        reasons.append(f"error rate {metrics['error_rate']:.1%} exceeds 5%")

    # Check memory.  When the reading is unavailable (degraded/unsupported
    # platform) get_memory_usage_bytes() returns 0, which is always less
    # than _MEMORY_LIMIT_BYTES so the threshold is safely skipped — no
    # false positive, no silent suppression of a real breach.
    if metrics["memory_usage_bytes"] > _MEMORY_LIMIT_BYTES:
        mb = metrics["memory_usage_bytes"] / (1024 * 1024)
        reasons.append(f"memory usage {mb:.0f} MB exceeds 2048 MB")

    # Check restart frequency
    if metrics["restarts_last_hour"] > MAX_RESTARTS_PER_HOUR:
        reasons.append(
            f"worker restarts {metrics['restarts_last_hour']} in last hour exceeds 3"
        )

    return reasons


def reset():
    """Reset all metrics.  Intended for testing."""
    global _success_count, _error_count, _restart_timestamps, _baseline_success_rate
    global _ui_lock_retry_count, _ui_lock_recovered_count, _ui_lock_exhausted_count
    global _vbv_detections
    with _lock:
        _success_count = 0
        _error_count = 0
        _restart_timestamps = []
        _baseline_success_rate = None
        _vbv_detections = 0
        _error_counts_by_persona.clear()
        _success_counts_by_persona.clear()
        _ui_lock_retry_count = 0
        _ui_lock_recovered_count = 0
        _ui_lock_exhausted_count = 0


class TransientMonitor:
    """Active poller for late-appearing VBV / 3DS iframes (Blueprint §6 Fork 3).

    The passive page-state scan in ``modules.cdp.driver`` can miss a 3D-Secure
    iframe that renders seconds after the payment submit returns, because it
    only samples once per state check.  ``TransientMonitor`` closes that gap
    by polling an injected ``detector`` callable at a fixed cadence
    (default ~500 ms) on a background daemon thread.  On the first positive
    detection it increments the monitor-internal ``vbv_detections`` counter
    (surfaced via :func:`get_metrics`), optionally invokes an ``on_detect``
    callback, and exits its loop.

    Contracts (Blueprint §8.3 — CRITICAL_SECTION / module-isolation):

    * **Thread-safe cancel** — :meth:`cancel` is idempotent and may be called
      from any thread; it sets a :class:`threading.Event` that the worker
      thread checks every tick and waits on between polls, so shutdown is
      bounded by a single ``interval``.
    * **No cross-module imports** — the class owns no DOM / CDP knowledge;
      the caller injects ``detector`` (e.g. a bound method that runs
      ``driver.find_elements(SEL_VBV_IFRAME)``).  This preserves the §5
      module-isolation invariant enforced by ``check_import_scope``.
    * **Non-blocking** — runs on a daemon thread so it can never block
      process shutdown, and never interferes with watchdog deadlines.
    """

    def __init__(self, detector, interval: float = 0.5, on_detect=None):
        if not callable(detector):
            raise TypeError("detector must be callable")
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._detector = detector
        self._interval = float(interval)
        self._on_detect = on_detect
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._detections = 0

    def start(self) -> None:
        """Start polling on a background daemon thread.

        Calling :meth:`start` while a previous poll loop is still running is
        a no-op — the monitor is single-shot per start.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._cancel.clear()
            t = threading.Thread(
                target=self._run,
                name="TransientMonitor",
                daemon=True,
            )
            self._thread = t
            t.start()

    def cancel(self, timeout: float | None = None) -> None:
        """Signal the poll loop to stop and optionally join the worker thread.

        Safe to call from any thread and safe to call multiple times.
        ``timeout`` is forwarded to :meth:`threading.Thread.join`; pass
        ``None`` to wait indefinitely or a float to bound the wait.
        """
        self._cancel.set()
        with self._lock:
            t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def is_running(self) -> bool:
        """Return True while the background poll loop is active."""
        with self._lock:
            t = self._thread
        return t is not None and t.is_alive()

    @property
    def detections(self) -> int:
        """Number of positive detections observed by this instance."""
        with self._lock:
            return self._detections

    def _run(self) -> None:
        while not self._cancel.is_set():
            try:
                hit = bool(self._detector())
            except Exception as e:  # pragma: no cover - defensive
                _logger.debug("TransientMonitor detector raised: %s", e)
                hit = False
            if hit:
                with self._lock:
                    self._detections += 1
                _record_vbv_detection()
                if self._on_detect is not None:
                    try:
                        self._on_detect()
                    except Exception as e:  # pragma: no cover - defensive
                        _logger.exception(
                            "TransientMonitor on_detect callback raised: %s", e
                        )
                return
            # cancel.wait returns True when the event is set — exits promptly.
            if self._cancel.wait(self._interval):
                return
