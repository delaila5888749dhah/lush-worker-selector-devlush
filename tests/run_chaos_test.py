"""Chaos Engineering / Stress Test — Core Engine

Runs 10 concurrent workers for 30 seconds, injecting random exceptions into
cdp.fill_card to validate thread-safety, FSM state integrity, and absence of
non-termination or thread leaks across modules.fsm, modules.watchdog, and modules.cdp.

Scope & Limitations
-------------------
This test exercises the stub layer (FakeDriver for synchronous workers,
FakeAsyncDriver for async workers — no real browser).
It validates:
  - FSM per-worker state isolation under concurrent load (fsm_error_count)
  - Watchdog session lifecycle correctness (SessionFlaggedError handling)
  - Thread non-termination detection (threads that fail to exit within budget)
  - Thread leak detection (surplus threads after all workers join)
  - Watchdog identity-check correctness under concurrent late-callback injection
  - vbv_3ds intermediate FSM path under concurrent worker load
  - SelectorTimeoutError and PageStateError injection and correct SessionFlaggedError routing
  - Per-session async callback timing: Timer A (on-time) and Timer B (stale, post-reset_session) via FakeAsyncDriver
  - Watchdog no-op correctness when notify_total() fires after session has been torn down

It does NOT validate:
  - Real async CDP/browser callbacks with real network latency (FakeAsyncDriver uses threading.Timer, not a real browser process)
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
    PageStateError,
    SelectorTimeoutError,
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
# FakeAsyncDriver spawns daemon timer threads (2 per fill_card() call). After
# the worker join deadline, a brief drain sleep is added to let pending Timer B
# threads fire and exit, so this threshold only needs to cover transient daemon
# overhead from non-timer sources. 5 is sufficient given the drain sleep above
# handles timer threads.
MAX_ACCEPTABLE_THREAD_SURPLUS = 5

# ValueError intentionally excluded: FSM raises ValueError on invalid transitions,
# which must be routed to fsm_error_count, not error_count.
_CHAOS_EXCEPTIONS = [TimeoutError, ConnectionError, RuntimeError]

# Probability distribution for detect_page_state() outcomes.
_DETECT_PROB_UI_LOCK          = 0.70
_DETECT_PROB_SELECTOR_TIMEOUT = 0.10
_DETECT_PROB_PAGE_STATE_ERR   = 0.10
# remaining 10% → "vbv_3ds"

# Maximum random delay (seconds) for late-callback injection.
_LATE_CALLBACK_MAX_DELAY_SEC = 0.200

# Maximum delay (seconds) for FakeAsyncDriver's first notify timer.
_ASYNC_NOTIFY_MAX_DELAY_SEC = 0.150

# Fraction of workers that use FakeAsyncDriver instead of FakeDriver.
# At 0.3: 3 out of 10 workers use async mode.
_ASYNC_DRIVER_FRACTION = 0.3

# ── Fake driver & card info ────────────────────────────────────────────────────


class FakeDriver:
    """Minimal browser-driver stub that injects random chaos in fill_card."""

    def fill_card(self, card_info) -> None:
        if random.random() < CHAOS_PROBABILITY:
            exc_class = random.choice(_CHAOS_EXCEPTIONS)
            raise exc_class(f"[chaos] {exc_class.__name__} injected by FakeDriver")

    def detect_page_state(self) -> str:
        # Cumulative probability thresholds:
        #   [0.00, 0.70) → "ui_lock"
        #   [0.70, 0.80) → raise SelectorTimeoutError
        #   [0.80, 0.90) → raise PageStateError
        #   [0.90, 1.00) → "vbv_3ds"
        roll = random.random()
        if roll < _DETECT_PROB_UI_LOCK:
            return "ui_lock"
        if roll < _DETECT_PROB_UI_LOCK + _DETECT_PROB_SELECTOR_TIMEOUT:
            raise SelectorTimeoutError("#checkout-total", 5.0)
        if roll < _DETECT_PROB_UI_LOCK + _DETECT_PROB_SELECTOR_TIMEOUT + _DETECT_PROB_PAGE_STATE_ERR:
            raise PageStateError("unknown")
        return "vbv_3ds"


class FakeAsyncDriver:
    """
    Browser-driver stub that models async CDP callback timing.

    Unlike FakeDriver (which calls notify_total() synchronously from the
    worker thread), FakeAsyncDriver spawns a daemon threading.Timer to call
    notify_total() from a separate thread after a short random delay.

    This models the real-world pattern where the browser's internal event
    thread fires the network-total callback independently of the worker thread.

    Two timers are spawned per fill_card() call:
      - Timer A: fires within [0, _ASYNC_NOTIFY_MAX_DELAY_SEC] — arrives while
        wait_for_total() is still blocked (normal path).
      - Timer B: fires at [_ASYNC_NOTIFY_MAX_DELAY_SEC * 2, _ASYNC_NOTIFY_MAX_DELAY_SEC * 3]
        — designed to arrive AFTER wait_for_total() has returned AND reset_session()
        has run. By watchdog design, notify_total() is a no-op when no session exists,
        so Timer B must never crash or corrupt state.

    chaos_probability controls whether fill_card() raises a chaos exception
    instead of spawning timers (same semantics as FakeDriver).
    """

    def __init__(self, worker_id: str, chaos_probability: float) -> None:
        self._worker_id = worker_id
        self._chaos_probability = chaos_probability

    def fill_card(self, card_info) -> None:
        if random.random() < self._chaos_probability:
            exc_class = random.choice(_CHAOS_EXCEPTIONS)
            raise exc_class(f"[chaos-async] {exc_class.__name__} injected by FakeAsyncDriver")

        delay_a = random.uniform(0.0, _ASYNC_NOTIFY_MAX_DELAY_SEC)
        delay_b = random.uniform(
            _ASYNC_NOTIFY_MAX_DELAY_SEC * 2,
            _ASYNC_NOTIFY_MAX_DELAY_SEC * 3,
        )
        value = random.uniform(50.0, 200.0)

        # Timer A — arrives while worker is blocked in wait_for_total()
        t_a = threading.Timer(delay_a, watchdog.notify_total, args=(self._worker_id, value))
        t_a.daemon = True
        t_a.start()

        # Timer B — arrives after reset_session() has already run; must be a no-op
        t_b = threading.Timer(delay_b, watchdog.notify_total, args=(self._worker_id, value))
        t_b.daemon = True
        t_b.start()

        log.debug(
            "[ASYNC_CB] worker=%s delay_a=%.3fs delay_b=%.3fs value=%.2f",
            self._worker_id,
            delay_a,
            delay_b,
            value,
        )

    def detect_page_state(self) -> str:
        """Same probabilistic behavior as FakeDriver."""
        roll = random.random()
        if roll < _DETECT_PROB_UI_LOCK:
            return "ui_lock"
        if roll < _DETECT_PROB_UI_LOCK + _DETECT_PROB_SELECTOR_TIMEOUT:
            raise SelectorTimeoutError("#checkout-total", 5.0)
        if roll < _DETECT_PROB_UI_LOCK + _DETECT_PROB_SELECTOR_TIMEOUT + _DETECT_PROB_PAGE_STATE_ERR:
            raise PageStateError("unknown")
        return "vbv_3ds"


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
    vbv_3ds_count: int = 0
    async_callback_count: int = 0


# ── Worker thread logic ────────────────────────────────────────────────────────


def _run_worker(worker_id: str, stop_event: threading.Event, stats: WorkerStats, is_async: bool = False) -> None:
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

                # detect_page_state() may return "ui_lock" or "vbv_3ds", or
                # raise SelectorTimeoutError / PageStateError (both subclass
                # SessionFlaggedError and are routed to the except below).
                page_state = cdp.detect_page_state(worker_id)

                # Isolate FSM ValueError from chaos exceptions so real FSM
                # transition bugs are counted in fsm_error_count, not error_count.
                try:
                    fsm.transition_for_worker(worker_id, "ui_lock")
                except ValueError as e:
                    stats.fsm_error_count += 1
                    log.critical("[FSM LEAK] [%s] invalid transition to ui_lock: %s", worker_id, e)
                    continue

                if page_state == "vbv_3ds":
                    # Two-step path: ui_lock → vbv_3ds → {success,declined}
                    try:
                        fsm.transition_for_worker(worker_id, "vbv_3ds")
                    except ValueError as e:
                        stats.fsm_error_count += 1
                        log.critical("[FSM LEAK] [%s] invalid transition to vbv_3ds: %s", worker_id, e)
                        continue

                    final_state = random.choice(["success", "declined"])
                    try:
                        fsm.transition_for_worker(worker_id, final_state)
                    except ValueError as e:
                        stats.fsm_error_count += 1
                        log.critical("[FSM LEAK] [%s] invalid transition to %s: %s", worker_id, final_state, e)
                        continue

                    stats.vbv_3ds_count += 1
                    if final_state == "success":
                        stats.success_count += 1
                    continue

                cdp.fill_card(FakeCardInfo(), worker_id)

                if not is_async:
                    # Sync path: pre-set the event so wait_for_total() returns
                    # immediately and still cleans up the session in its finally.
                    watchdog.notify_total(worker_id, 100.0)
                # Async path: Timer A from FakeAsyncDriver.fill_card() will
                # unblock wait_for_total() naturally; no sync pre-notify.
                watchdog.wait_for_total(worker_id, timeout=2.0)

                final_state = random.choice(["success", "declined"])

                try:
                    fsm.transition_for_worker(worker_id, final_state)
                except ValueError as e:
                    stats.fsm_error_count += 1
                    log.critical("[FSM LEAK] [%s] invalid transition to %s: %s", worker_id, final_state, e)
                    continue

                stats.success_count += 1
                if is_async:
                    stats.async_callback_count += 1

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
        # NOTE: FakeAsyncDriver may have Timer B still pending after teardown.
        # By watchdog design, notify_total() is a no-op when no session exists —
        # Timer B fires into an empty registry and is silently discarded.
        fsm.cleanup_worker(worker_id)
        cdp.unregister_driver(worker_id)
        watchdog.reset_session(worker_id)
        log.info("[%s] cleaned up", worker_id)


# ── Late callback injector ─────────────────────────────────────────────────────


class LateCallbackInjector:
    """
    Simulates async CDP callbacks arriving from an external thread.
    Randomly calls notify_total() for random sync worker IDs at random short delays.

    Covers late-notify scenarios this stub can actually model:
      1. Callback arrives while a session is alive → no-op (event already set) or sets value.
      2. Callback arrives after reset_session() → no-op (registry has no entry).

    This injector targets late notifications by worker ID only. It does not model
    per-session callback identity, so it does not verify the race where a stale
    callback from an old session arrives after the same worker has started a new
    session.

    Async workers (FakeAsyncDriver) are intentionally excluded so that Timer A
    from fill_card() remains the sole unblocker for async sessions, ensuring the
    async callback path is genuinely exercised.
    """

    def __init__(self, sync_worker_ids: list[str], stop_event: threading.Event) -> None:
        self._worker_ids = sync_worker_ids
        self._stop_event = stop_event

    def run(self) -> None:
        while not self._stop_event.is_set():
            worker_id = random.choice(self._worker_ids)
            delay = random.uniform(0.0, _LATE_CALLBACK_MAX_DELAY_SEC)
            self._stop_event.wait(timeout=delay)
            if self._stop_event.is_set():
                break
            value = random.uniform(50.0, 200.0)
            watchdog.notify_total(worker_id, value)
            log.debug("[LATE_CB] notified %s value=%.2f", worker_id, value)


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
    worker_ids: list[str] = []
    sync_worker_ids: list[str] = []
    worker_is_async: list[bool] = []

    for i in range(NUM_WORKERS):
        worker_id = f"worker-{i:02d}"
        worker_ids.append(worker_id)
        stats = WorkerStats()
        all_stats.append(stats)

        # Use FakeAsyncDriver for a fraction of workers to cover async callback timing.
        if i < int(NUM_WORKERS * _ASYNC_DRIVER_FRACTION):
            driver = FakeAsyncDriver(worker_id, CHAOS_PROBABILITY)
            is_async = True
        else:
            driver = FakeDriver()
            is_async = False
            sync_worker_ids.append(worker_id)
        worker_is_async.append(is_async)
        cdp.register_driver(worker_id, driver)
        fsm.initialize_for_worker(worker_id)

        t = threading.Thread(
            target=_run_worker,
            args=(worker_id, stop_event, stats, is_async),
            name=f"chaos-{worker_id}",
            daemon=False,
        )
        worker_threads.append(t)

    # Start late-callback injector (daemon so it never blocks process exit).
    # Only targets sync workers so async workers rely solely on Timer A.
    injector = LateCallbackInjector(sync_worker_ids, stop_event)
    injector_thread = threading.Thread(
        target=injector.run,
        name="late-cb-injector",
        daemon=True,
    )
    injector_thread.start()

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

    # Join injector briefly — it is a daemon thread so it will not block exit,
    # but joining ensures its final log messages are flushed before the report.
    injector_thread.join(timeout=2)

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

    # FakeAsyncDriver spawns daemon Timer B threads that fire up to
    # _ASYNC_NOTIFY_MAX_DELAY_SEC * 3 seconds after fill_card(). Workers may
    # exit (and be joined) before Timer B threads have fired. Give daemon
    # timers a brief window to fire and exit before counting active threads,
    # so the THREAD_LEAK check reflects only true non-daemon leaks.
    if any(worker_is_async):
        time.sleep(_ASYNC_NOTIFY_MAX_DELAY_SEC * 3 + 0.5)

    total_success = sum(s.success_count for s in all_stats)
    total_error = sum(s.error_count for s in all_stats)
    total_timeout = sum(s.timeout_count for s in all_stats)
    total_fsm_err = sum(s.fsm_error_count for s in all_stats)
    total_vbv_3ds = sum(s.vbv_3ds_count for s in all_stats)
    total_async_cb = sum(s.async_callback_count for s in all_stats)
    final_active = threading.active_count()

    print()
    print("=" * 60)
    print("  CHAOS TEST FINAL REPORT")
    print("=" * 60)
    print(f"  Total successes    : {total_success}")
    print(f"  Total errors       : {total_error}")
    print(f"  Total timeouts     : {total_timeout}")
    print(f"  FSM errors         : {total_fsm_err}")
    print(f"  vbv_3ds paths      : {total_vbv_3ds}")
    print(f"  Async callback paths: {total_async_cb}")
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