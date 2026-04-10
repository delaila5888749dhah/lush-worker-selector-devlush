"""Chaos Engineering / Stress Test — Core Engine

Runs 10 concurrent workers for 30 seconds, injecting random exceptions into
cdp.fill_card to validate thread-safety, FSM state integrity, and absence of
deadlocks or thread leaks across modules.fsm, modules.watchdog, and modules.cdp.

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

NUM_WORKERS = 10
DURATION_SECONDS = int(os.environ.get("CHAOS_DURATION", "30"))
CHAOS_PROBABILITY = float(os.environ.get("CHAOS_PROBABILITY_ENV", "0.40"))
METRICS_INTERVAL = 5  # seconds between metrics prints
# Allow up to this many extra threads after all workers join (the main thread
# itself + any OS/Python internal threads that may linger briefly).
MAX_ACCEPTABLE_THREAD_SURPLUS = 3

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
                # Fix #1: always start each iteration with a fresh watchdog
                # session.  wait_for_total() tears down its own session entry
                # (including on SessionFlaggedError), so the next iteration
                # must not rely on any previous monitor still being present.
                watchdog.reset_session(worker_id)
                watchdog.enable_network_monitor(worker_id)

                try:
                    fsm.transition_for_worker(worker_id, "ui_lock")
                except ValueError as e:
                    stats.fsm_error_count += 1
                    log.critical("[FSM LEAK] [%s] invalid transition to ui_lock: %s", worker_id, e)
                    continue

                cdp.fill_card(FakeCardInfo(), worker_id)

                # Notify before wait so the event is already set.
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
                # CRITICAL: always reset FSM back to initial state for next iteration.
                fsm.initialize_for_worker(worker_id)
    finally:
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

    # Set up workers and launch threads.
    for i in range(NUM_WORKERS):
        worker_id = f"worker-{i:02d}"
        stats = WorkerStats()
        all_stats.append(stats)

        # Per-worker setup (before thread launch).
        cdp.register_driver(worker_id, FakeDriver())
        fsm.initialize_for_worker(worker_id)
        watchdog.enable_network_monitor(worker_id)
        watchdog.notify_total(worker_id, 100.0)

        t = threading.Thread(
            target=_run_worker,
            args=(worker_id, stop_event, stats),
            name=f"chaos-{worker_id}",
            daemon=False,
        )
        worker_threads.append(t)

    # Start metrics reporter.
    reporter_stop = threading.Event()
    reporter = threading.Thread(
        target=_metrics_reporter,
        args=(reporter_stop, all_stats, baseline_thread_count),
        name="metrics-reporter",
        daemon=True,
    )
    reporter.start()

    # Start all worker threads.
    for t in worker_threads:
        t.start()

    log.info("All %d workers launched — running for %ds …", NUM_WORKERS, DURATION_SECONDS)
    time.sleep(DURATION_SECONDS)
    stop_event.set()
    log.info("Stop event set — waiting for workers to finish …")

    deadlock_detected = False
    worker_join_timeout = 10
    worker_join_deadline = time.monotonic() + worker_join_timeout
    for t in worker_threads:
        remaining = worker_join_deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(timeout=max(0.1, remaining))

    for t in worker_threads:
        if t.is_alive():
            log.critical(
                "[DEADLOCK] thread %s did not terminate within %ss overall join timeout",
                t.name,
                worker_join_timeout,
            )
            deadlock_detected = True

    # Stop metrics reporter.
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
    print(f"  Total successes  : {total_success}")
    print(f"  Total errors     : {total_error}")
    print(f"  Total timeouts   : {total_timeout}")
    print(f"  FSM errors       : {total_fsm_err}")
    print(f"  Active threads   : {final_active}  (baseline={baseline_thread_count})")
    print("=" * 60)

    exit_code = 0

    if total_fsm_err > 0:
        print(f"❌ [FAIL] FSM STATE LEAK — {total_fsm_err} FSM error(s) detected")
        exit_code = 2

    if deadlock_detected:
        print("❌ [FAIL] DEADLOCK — one or more worker threads failed to terminate")
        exit_code = 3

    thread_surplus = final_active - baseline_thread_count
    if thread_surplus > MAX_ACCEPTABLE_THREAD_SURPLUS:
        print(
            f"❌ [FAIL] THREAD LEAK — {thread_surplus} extra threads still alive \
            (active={final_active}, baseline={baseline_thread_count})"
        )
        if exit_code == 0:
            exit_code = 1

    if exit_code == 0:
        print("✅ [PASS] No FSM state leaks detected")
        print("✅ [PASS] No deadlocks detected")
        print("✅ [PASS] No thread leaks detected")
        print("✅ [PASS] Chaos test completed successfully")
    print("=" * 60)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())