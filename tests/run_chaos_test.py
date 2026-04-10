"""Chaos Engineering / Stress Test — Core Engine

Runs 10 concurrent workers for 30 seconds, injecting random exceptions into
cdp.fill_card to validate thread-safety, FSM state integrity, and absence of
non-termination or thread leaks across modules.fsm, modules.watchdog, and modules.cdp.

Scope & Limitations
-------------------
This test exercises the *synchronous stub* layer only (FakeDriver, no real browser).
It validates:
  - FSM per-worker state isolation under concurrent load (fsm_error_count)
  - Watchdog session lifecycle correctness (SessionFlaggedError handling)
  - Thread non-termination detection (threads that fail to exit within budget)
  - Thread leak detection (surplus threads after all workers join)

It does NOT validate:
  - Real async CDP callbacks arriving after worker teardown (no late callbacks in stubs)
  - Network-level races or browser process lifecycle

Exit code priority (highest wins):
  3 — NON_TERMINATION  (thread failed to exit within join budget; may indicate deadlock,
                        starvation, hung syscall, or scheduler delay — label is intentionally
                        broad; investigate logs for root cause)
  2 — FSM_STATE_LEAK   (InvalidStateError / InvalidTransitionError / invalid ValueError
                        from a real FSM transition)
  1 — THREAD_LEAK      (surplus threads still alive after join, above allowed threshold)
  0 — PASSED

Usage:
    python tests/run_chaos_test.py
"""

import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass

# Ensure repo root is on sys.path so module imports work when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.cdp.main as cdp
import modules.fsm as fsm
import modules.watchdog.main as watchdog
from modules.common.exceptions import (
    InvalidStateError,
    InvalidTransitionError,
    SessionFlaggedError,
)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

def _parse_env_int(name: str, default: int, min_val: int = 1) -> int:
    raw = os.environ.get(name, str(default))
    try:
        val = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default
    if val < min_val:
        log.warning("%s=%d below minimum %d, clamping", name, val, min_val)
        return min_val
    return val

def _parse_env_float(name: str, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    raw = os.environ.get(name, str(default))
    try:
        val = float(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %.2f", name, raw, default)
        return default
    if not (lo <= val <= hi):
        clamped = max(lo, min(hi, val))
        log.warning("%s=%.3f out of [%.1f, %.1f], clamping to %.3f", name, val, lo, hi, clamped)
        return clamped
    return val

NUM_WORKERS = 10
DURATION_SECONDS = _parse_env_int("CHAOS_DURATION", default=30, min_val=1)
CHAOS_PROBABILITY = _parse_env_float("CHAOS_PROBABILITY_ENV", default=0.40)
METRICS_INTERVAL = 5  # seconds between metrics prints

# Allow up to this many extra threads after all workers join (accounts for the
# main thread, the daemon metrics-reporter, and any brief OS/Python internals).
MAX_ACCEPTABLE_THREAD_SURPLUS = 3

# ValueError intentionally excluded: FSM raises ValueError on invalid transitions,
# which must be routed to fsm_error_count, not error_count.
_CHAOS_EXCEPTIONS = [TimeoutError, ConnectionError, RuntimeError]

# ── Fake driver & card info ────────────────────────────────────────────────────


class FakeDriver:
    """Minimal browser-driver stub that injects random chaos in fill_card."""

    def fill_card(self, card_info) -> None:
        if random.random() < CHAOS_PROBABILITY:
            exc_class = random.choice(_CHAOS_EXCEPTIONS)
            raise exc_class(f"[chaos] {exc_class.__name__} injected by FakeDriver")

    def detect_page_state(self) -> str:
        return "ui_lock"


@dataclass
class FakeCardInfo:
    number: str = "4111111111111111"
    expiry: str = "12/29"
    cvv: str = "123"


# ── Per-worker statistics ──────────────────────────────────────────────────────


@dataclass
class WorkerStats:
    # Single writer per object: each WorkerStats instance is owned exclusively
    # by one worker thread.  The metrics-reporter thread reads these fields but
    # never writes them, so no lock is required in CPython.
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    fsm_error_count: int = 0


# ── Worker thread logic ────────────────────────────────────────────────────────


def _run_worker(worker_id: str, stop_event: threading.Event, stats: WorkerStats) -> None:
    """Main loop for a single chaos worker."""
    try:
        while not stop_event.is_set():
            try:
                # Always start each iteration with a fresh watchdog session.
                # wait_for_total() tears down its own session entry in its
                # finally-block (including on SessionFlaggedError), so the
                # next iteration must never rely on a previous session.
                watchdog.reset_session(worker_id)
                watchdog.enable_network_monitor(worker_id)

                # Isolate FSM ValueError from chaos exceptions so real FSM
                # transition bugs are counted in fsm_error_count, not error_count.
                try:
                    fsm.transition_for_worker(worker_id, "ui_lock")
                except ValueError as e:
                    stats.fsm_error_count += 1
                    log.critical("[FSM LEAK] [%s] invalid transition to ui_lock: %s", worker_id, e)
                    continue

                cdp.fill_card(FakeCardInfo(), worker_id)

                # Notify before wait: event is pre-set so wait_for_total()
                # returns immediately and still cleans up the session in its finally.
                watchdog.notify_total(worker_id, 100.0)
                watchdog.wait_for_total(worker_id, timeout=2.0)

                final_state = random.choice(["success", "declined"])

                try:
                    fsm.transition_for_worker(worker_id, final_state)
                except ValueError as e:
                    stats.fsm_error_count += 1
                    log.critical("[FSM LEAK] [%s] invalid transition to %s: %s", worker_id, final_state, e)
                    continue

                stats.success_count += 1

            except (TimeoutError, ConnectionError, RuntimeError) as e:
                stats.error_count += 1
                log.error("[%s] chaos exception: %s", worker_id, e)
            except SessionFlaggedError as e:
                stats.timeout_count += 1
                log.error("[%s] session flagged (watchdog timeout): %s", worker_id, e)
            except (InvalidStateError, InvalidTransitionError) as e:
                stats.fsm_error_count += 1
                log.critical("[FSM LEAK] [%s] %s", worker_id, e)
            except Exception as e:  # noqa: BLE001
                stats.error_count += 1
                log.critical("[UNKNOWN] [%s] %s", worker_id, e)
            finally:
                # Always reset FSM to initial state for next iteration.
                # This runs even on `continue` (Python guarantees finally on continue).
                fsm.initialize_for_worker(worker_id)
    finally:
        # Outer teardown: runs even if an unexpected exception escapes the loop.
        # NOTE: with synchronous stubs there are no late async callbacks, so
        # teardown here is deterministic. With real CDP drivers, a late
        # notify_total() after unregister is a no-op by watchdog design.
        fsm.cleanup_worker(worker_id)
        cdp.unregister_driver(worker_id)
        watchdog.reset_session(worker_id)
        log.info("[%s] cleaned up", worker_id)


# ── Metrics reporter thread ────────────────────────────────────────────────────


def _metrics_reporter(
    stop_event: threading.Event,
    all_stats: list[WorkerStats],
    baseline_thread_count: int,
) -> None:
    """Periodically print aggregate stats to stdout."""
    while not stop_event.wait(timeout=METRICS_INTERVAL):
        total_success = sum(s.success_count for s in all_stats)
        total_error = sum(s.error_count for s in all_stats)
        total_timeout = sum(s.timeout_count for s in all_stats)
        total_fsm_err = sum(s.fsm_error_count for s in all_stats)
        active = threading.active_count()
        log.info(
            "[METRICS] success=%d error=%d timeout=%d fsm_error=%d "
            "active_threads=%d (baseline=%d)",
            total_success,
            total_error,
            total_timeout,
            total_fsm_err,
            active,
            baseline_thread_count,
        )


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> int:
    baseline_thread_count = threading.active_count()
    log.info(
        "Starting chaos test: workers=%d duration=%ds chaos_prob=%.0f%%",
        NUM_WORKERS,
        DURATION_SECONDS,
        CHAOS_PROBABILITY * 100,
    )

    stop_event = threading.Event()
    all_stats: list[WorkerStats] = []
    worker_threads: list[threading.Thread] = []

    for i in range(NUM_WORKERS):
        worker_id = f"worker-{i:02d}"
        stats = WorkerStats()
        all_stats.append(stats)

        # Register driver and FSM only — worker loop handles watchdog setup
        # at the start of each iteration, so no pre-thread watchdog setup needed.
        cdp.register_driver(worker_id, FakeDriver())
        fsm.initialize_for_worker(worker_id)

        t = threading.Thread(
            target=_run_worker,
            args=(worker_id, stop_event, stats),
            name=f"chaos-{worker_id}",
            daemon=False,
        )
        worker_threads.append(t)

    # Start metrics reporter (daemon so it never blocks process exit).
    reporter_stop = threading.Event()
    reporter = threading.Thread(
        target=_metrics_reporter,
        args=(reporter_stop, all_stats, baseline_thread_count),
        name="metrics-reporter",
        daemon=True,
    )
    reporter.start()

    for t in worker_threads:
        t.start()

    log.info("All %d workers launched — running for %ds …", NUM_WORKERS, DURATION_SECONDS)
    time.sleep(DURATION_SECONDS)
    stop_event.set()
    log.info("Stop event set — waiting for workers to finish …")

    # Shared 10s deadline across all threads.
    # Keeps total join time bounded to avoid running over the CI step timeout.
    worker_join_timeout = 10
    worker_join_deadline = time.monotonic() + worker_join_timeout
    for t in worker_threads:
        remaining = worker_join_deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(timeout=max(0.1, remaining))

    non_termination_detected = False
    for t in worker_threads:
        if t.is_alive():
            log.critical(
                "[NON_TERMINATION] thread %s still alive after %ss join budget "
                "(possible deadlock, starvation, or hung syscall — check logs above)",
                t.name,
                worker_join_timeout,
            )
            non_termination_detected = True

    # Join reporter briefly for clean thread accounting.
    reporter_stop.set()
    reporter.join(timeout=5)

    total_success = sum(s.success_count for s in all_stats)
    total_error = sum(s.error_count for s in all_stats)
    total_timeout = sum(s.timeout_count for s in all_stats)
    total_fsm_err = sum(s.fsm_error_count for s in all_stats)
    final_active = threading.active_count()

    print()
    print("=" * 60)
    print("  CHAOS TEST FINAL REPORT")
    print("=" * 60)
    print(f"  Total successes    : {total_success}")
    print(f"  Total errors       : {total_error}")
    print(f"  Total timeouts     : {total_timeout}")
    print(f"  FSM errors         : {total_fsm_err}")
    print(f"  Active threads     : {final_active}  (baseline={baseline_thread_count})")
    print("=" * 60)
    print("  Exit code priority : NON_TERMINATION(3) > FSM_LEAK(2) > THREAD_LEAK(1) > PASS(0)")
    print("=" * 60)

    # Exit code priority: highest severity wins when multiple issues coexist.
    exit_code = 0

    thread_surplus = final_active - baseline_thread_count
    if thread_surplus > MAX_ACCEPTABLE_THREAD_SURPLUS:
        print(
            f"❌ [FAIL] THREAD_LEAK — {thread_surplus} surplus threads still alive "
            f"(active={final_active}, baseline={baseline_thread_count})"
        )
        exit_code = 1

    if total_fsm_err > 0:
        print(f"❌ [FAIL] FSM_STATE_LEAK — {total_fsm_err} FSM error(s) detected")
        exit_code = 2

    if non_termination_detected:
        print(
            "❌ [FAIL] NON_TERMINATION — one or more threads failed to exit within "
            f"{worker_join_timeout}s budget (deadlock / starvation / hung syscall)"
        )
        exit_code = 3

    if exit_code == 0:
        print("✅ [PASS] No FSM state leaks detected")
        print("✅ [PASS] No non-termination detected")
        print("✅ [PASS] No thread leaks detected")
        print("✅ [PASS] Chaos test completed successfully")
    print("=" * 60)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())