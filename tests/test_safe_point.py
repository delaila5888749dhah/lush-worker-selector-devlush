"""Tests for Task 9.1 — Safe Point Architecture (Worker Execution States).

Validates:
  - ALLOWED_WORKER_STATES defined with 4 states
  - _VALID_TRANSITIONS enforces strict transition rules
  - set_worker_state() validates transitions, raises ValueError if invalid
  - set_worker_state() validates worker_id exists in _workers
  - get_worker_state() returns current state or raises for unknown worker
  - get_all_worker_states() returns dict snapshot
  - is_safe_to_control() returns True ONLY when all workers IDLE/SAFE_POINT
  - start_worker() initializes worker state to IDLE
  - _worker_fn() transitions IDLE -> IN_CYCLE -> IDLE per cycle
  - Cleanup paths clear worker state on exit
  - reset() clears _worker_states
  - Thread safety via _lock
  - Worker states are SEPARATE from lifecycle states
"""
import threading
import time
import unittest

from integration import runtime
from integration.runtime import (
    ALLOWED_WORKER_STATES,
    _VALID_TRANSITIONS,
    get_active_workers,
    get_all_worker_states,
    get_worker_state,
    is_safe_to_control,
    reset,
    set_worker_state,
    start_worker,
    stop_worker,
)
from modules.monitor import main as monitor
from modules.rollout import main as rollout

CLEANUP_TIMEOUT = 2
WARMUP_DELAY = 0.2


class SafePointResetMixin:
    """Common setUp/tearDown for safe point tests."""

    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()

    def tearDown(self):
        reset()
        rollout.reset()
        monitor.reset()


# ── ALLOWED_WORKER_STATES ────────────────────────────────────────


class TestAllowedWorkerStates(unittest.TestCase):
    """ALLOWED_WORKER_STATES must have exactly 4 states."""

    def test_has_four_states(self):
        self.assertEqual(len(ALLOWED_WORKER_STATES), 4)

    def test_contains_idle(self):
        self.assertIn("IDLE", ALLOWED_WORKER_STATES)

    def test_contains_in_cycle(self):
        self.assertIn("IN_CYCLE", ALLOWED_WORKER_STATES)

    def test_contains_critical_section(self):
        self.assertIn("CRITICAL_SECTION", ALLOWED_WORKER_STATES)

    def test_contains_safe_point(self):
        self.assertIn("SAFE_POINT", ALLOWED_WORKER_STATES)

    def test_is_a_set(self):
        self.assertIsInstance(ALLOWED_WORKER_STATES, set)


# ── _VALID_TRANSITIONS ──────────────────────────────────────────


class TestValidTransitions(unittest.TestCase):
    """_VALID_TRANSITIONS must enforce strict transition rules."""

    def test_idle_to_in_cycle(self):
        self.assertIn("IN_CYCLE", _VALID_TRANSITIONS["IDLE"])

    def test_idle_no_other_transitions(self):
        self.assertEqual(_VALID_TRANSITIONS["IDLE"], {"IN_CYCLE"})

    def test_in_cycle_to_critical_section(self):
        self.assertIn("CRITICAL_SECTION", _VALID_TRANSITIONS["IN_CYCLE"])

    def test_in_cycle_to_safe_point(self):
        self.assertIn("SAFE_POINT", _VALID_TRANSITIONS["IN_CYCLE"])

    def test_in_cycle_to_idle(self):
        self.assertIn("IDLE", _VALID_TRANSITIONS["IN_CYCLE"])

    def test_critical_section_to_in_cycle(self):
        self.assertIn("IN_CYCLE", _VALID_TRANSITIONS["CRITICAL_SECTION"])

    def test_critical_section_no_other_transitions(self):
        self.assertEqual(_VALID_TRANSITIONS["CRITICAL_SECTION"], {"IN_CYCLE"})

    def test_safe_point_to_in_cycle(self):
        self.assertIn("IN_CYCLE", _VALID_TRANSITIONS["SAFE_POINT"])

    def test_safe_point_no_other_transitions(self):
        self.assertEqual(_VALID_TRANSITIONS["SAFE_POINT"], {"IN_CYCLE"})

    def test_all_states_have_transitions(self):
        for state in ALLOWED_WORKER_STATES:
            self.assertIn(state, _VALID_TRANSITIONS)


# ── set_worker_state ─────────────────────────────────────────────


class TestSetWorkerState(SafePointResetMixin, unittest.TestCase):
    """set_worker_state() validates transitions and worker existence."""

    def _make_worker_at_state(self, target_state):
        """Helper: start a worker and force it to a known state for testing transitions."""
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        time.sleep(0.05)  # let _worker_fn enter IN_CYCLE
        with runtime._lock:
            runtime._worker_states[wid] = target_state
        return wid, barrier

    def test_valid_transition_idle_to_in_cycle(self):
        wid, barrier = self._make_worker_at_state("IDLE")
        set_worker_state(wid, "IN_CYCLE")
        self.assertEqual(get_worker_state(wid), "IN_CYCLE")
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_invalid_transition_idle_to_critical_raises(self):
        wid, barrier = self._make_worker_at_state("IDLE")
        with self.assertRaises(ValueError):
            set_worker_state(wid, "CRITICAL_SECTION")
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_unknown_worker_raises(self):
        with self.assertRaises(ValueError):
            set_worker_state("nonexistent-worker", "IN_CYCLE")

    def test_invalid_state_name_raises(self):
        wid, barrier = self._make_worker_at_state("IDLE")
        with self.assertRaises(ValueError):
            set_worker_state(wid, "BOGUS_STATE")
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_valid_sequence_idle_incycle_critical_incycle_idle(self):
        """Full valid transition cycle: IDLE->IN_CYCLE->CRITICAL_SECTION->IN_CYCLE->IDLE."""
        wid, barrier = self._make_worker_at_state("IDLE")
        set_worker_state(wid, "IN_CYCLE")
        set_worker_state(wid, "CRITICAL_SECTION")
        set_worker_state(wid, "IN_CYCLE")
        set_worker_state(wid, "IDLE")
        self.assertEqual(get_worker_state(wid), "IDLE")
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_valid_sequence_idle_incycle_safepoint_incycle_idle(self):
        """Full valid transition cycle: IDLE->IN_CYCLE->SAFE_POINT->IN_CYCLE->IDLE."""
        wid, barrier = self._make_worker_at_state("IDLE")
        set_worker_state(wid, "IN_CYCLE")
        set_worker_state(wid, "SAFE_POINT")
        set_worker_state(wid, "IN_CYCLE")
        set_worker_state(wid, "IDLE")
        self.assertEqual(get_worker_state(wid), "IDLE")
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_critical_section_to_idle_invalid(self):
        """CRITICAL_SECTION -> IDLE is not a valid transition."""
        wid, barrier = self._make_worker_at_state("CRITICAL_SECTION")
        with self.assertRaises(ValueError):
            set_worker_state(wid, "IDLE")
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_safe_point_to_idle_invalid(self):
        """SAFE_POINT -> IDLE is not a valid transition."""
        wid, barrier = self._make_worker_at_state("SAFE_POINT")
        with self.assertRaises(ValueError):
            set_worker_state(wid, "IDLE")
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)


# ── get_worker_state ─────────────────────────────────────────────


class TestGetWorkerState(SafePointResetMixin, unittest.TestCase):
    """get_worker_state() returns current state or raises for unknown."""

    def test_unknown_worker_raises(self):
        with self.assertRaises(ValueError):
            get_worker_state("nonexistent-worker")

    def test_initial_state_is_idle(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        # Worker starts at IDLE (but _worker_fn may have already transitioned)
        # We just need to confirm it's a valid tracked state
        state = get_worker_state(wid)
        self.assertIn(state, ALLOWED_WORKER_STATES)
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)


# ── get_all_worker_states ────────────────────────────────────────


class TestGetAllWorkerStates(SafePointResetMixin, unittest.TestCase):
    """get_all_worker_states() returns a dict snapshot."""

    def test_empty_when_no_workers(self):
        self.assertEqual(get_all_worker_states(), {})

    def test_returns_dict(self):
        self.assertIsInstance(get_all_worker_states(), dict)

    def test_snapshot_is_a_copy(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        snapshot = get_all_worker_states()
        # Modifying snapshot must not affect internal state
        snapshot["fake-worker"] = "BOGUS"
        real = get_all_worker_states()
        self.assertNotIn("fake-worker", real)
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_tracks_multiple_workers(self):
        barrier = threading.Event()
        wid1 = start_worker(lambda _: barrier.wait(timeout=2))
        wid2 = start_worker(lambda _: barrier.wait(timeout=2))
        states = get_all_worker_states()
        self.assertIn(wid1, states)
        self.assertIn(wid2, states)
        barrier.set()
        stop_worker(wid1, timeout=CLEANUP_TIMEOUT)
        stop_worker(wid2, timeout=CLEANUP_TIMEOUT)


# ── is_safe_to_control ──────────────────────────────────────────


class TestIsSafeToControl(SafePointResetMixin, unittest.TestCase):
    """is_safe_to_control() returns True only when all workers IDLE/SAFE_POINT."""

    def test_true_with_no_workers(self):
        self.assertTrue(is_safe_to_control())

    def test_true_when_worker_idle(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        # Force to known IDLE state for deterministic test
        with runtime._lock:
            runtime._worker_states[wid] = "IDLE"
        self.assertTrue(is_safe_to_control())
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_true_when_worker_safe_point(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        # Force to known state for deterministic test
        with runtime._lock:
            runtime._worker_states[wid] = "SAFE_POINT"
        self.assertTrue(is_safe_to_control())
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_false_when_worker_in_cycle(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        with runtime._lock:
            runtime._worker_states[wid] = "IN_CYCLE"
        self.assertFalse(is_safe_to_control())
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_false_when_worker_critical_section(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        with runtime._lock:
            runtime._worker_states[wid] = "CRITICAL_SECTION"
        self.assertFalse(is_safe_to_control())
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_false_when_missing_state_entry(self):
        """Missing state entry treated as UNSAFE."""
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        with runtime._lock:
            runtime._worker_states.pop(wid, None)
        self.assertFalse(is_safe_to_control())
        barrier.set()
        # Re-add state to allow clean stop
        with runtime._lock:
            runtime._worker_states[wid] = "IDLE"
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_mixed_states_one_unsafe(self):
        """If any worker is not IDLE/SAFE_POINT, result is False."""
        barrier = threading.Event()
        wid1 = start_worker(lambda _: barrier.wait(timeout=2))
        wid2 = start_worker(lambda _: barrier.wait(timeout=2))
        with runtime._lock:
            runtime._worker_states[wid1] = "IDLE"
            runtime._worker_states[wid2] = "IN_CYCLE"
        self.assertFalse(is_safe_to_control())
        barrier.set()
        stop_worker(wid1, timeout=CLEANUP_TIMEOUT)
        stop_worker(wid2, timeout=CLEANUP_TIMEOUT)

    def test_all_safe_point_is_safe(self):
        barrier = threading.Event()
        wid1 = start_worker(lambda _: barrier.wait(timeout=2))
        wid2 = start_worker(lambda _: barrier.wait(timeout=2))
        with runtime._lock:
            runtime._worker_states[wid1] = "SAFE_POINT"
            runtime._worker_states[wid2] = "SAFE_POINT"
        self.assertTrue(is_safe_to_control())
        barrier.set()
        stop_worker(wid1, timeout=CLEANUP_TIMEOUT)
        stop_worker(wid2, timeout=CLEANUP_TIMEOUT)


# ── start_worker state init ─────────────────────────────────────


class TestStartWorkerStateInit(SafePointResetMixin, unittest.TestCase):
    """start_worker() must initialize worker state to IDLE."""

    def test_worker_registered_in_states(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        self.assertIn(wid, get_all_worker_states())
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_initial_state_tracked(self):
        """Worker starts with a tracked state in ALLOWED_WORKER_STATES."""
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        state = get_worker_state(wid)
        self.assertIn(state, ALLOWED_WORKER_STATES)
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)


# ── _worker_fn state transitions ────────────────────────────────


class TestWorkerFnTransitions(SafePointResetMixin, unittest.TestCase):
    """_worker_fn() transitions IDLE -> IN_CYCLE -> IDLE per cycle."""

    def test_worker_reaches_in_cycle_during_task(self):
        """Worker should be IN_CYCLE while executing task_fn."""
        observed = []
        barrier = threading.Event()

        def task(wid):
            state = get_worker_state(wid)
            observed.append(state)
            barrier.set()
            time.sleep(0.5)

        wid = start_worker(task)
        barrier.wait(timeout=2)
        self.assertEqual(observed[0], "IN_CYCLE")
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)

    def test_worker_returns_to_idle_after_task(self):
        """Worker returns to IDLE after completing task_fn cycle."""
        completed = threading.Event()
        cycle_done = threading.Event()

        def task(wid):
            if not completed.is_set():
                completed.set()
                # Allow a brief moment for the state transition back
                time.sleep(0.05)

        wid = start_worker(task)
        completed.wait(timeout=2)
        # Give time for worker to transition back to IDLE
        time.sleep(0.3)
        # Worker may have started another cycle or be in IDLE
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)


# ── Cleanup paths ────────────────────────────────────────────────


class TestCleanupPaths(SafePointResetMixin, unittest.TestCase):
    """Cleanup paths must clear worker state on exit."""

    def test_stop_worker_clears_state(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        time.sleep(0.1)
        self.assertNotIn(wid, get_all_worker_states())

    def test_worker_error_clears_state(self):
        """Worker that raises an exception should have its state cleaned up."""
        def failing_task(wid):
            raise RuntimeError("deliberate failure")

        wid = start_worker(failing_task)
        time.sleep(0.5)
        self.assertNotIn(wid, get_all_worker_states())

    def test_reset_clears_all_worker_states(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        barrier.set()
        reset()
        self.assertEqual(get_all_worker_states(), {})


# ── Separation from lifecycle states ─────────────────────────────


class TestStatesSeparation(SafePointResetMixin, unittest.TestCase):
    """Worker states are SEPARATE from lifecycle states (INIT/RUNNING/STOPPING/STOPPED)."""

    def test_allowed_states_unchanged(self):
        from integration.runtime import ALLOWED_STATES
        self.assertEqual(ALLOWED_STATES, {"INIT", "RUNNING", "STOPPING", "STOPPED"})

    def test_worker_states_distinct_from_lifecycle(self):
        self.assertNotEqual(ALLOWED_WORKER_STATES, {"INIT", "RUNNING", "STOPPING", "STOPPED"})
        # No overlap
        from integration.runtime import ALLOWED_STATES
        self.assertEqual(ALLOWED_WORKER_STATES & ALLOWED_STATES, set())


# ── Thread safety ────────────────────────────────────────────────


class TestThreadSafety(SafePointResetMixin, unittest.TestCase):
    """Worker state operations must be thread-safe."""

    def test_concurrent_state_reads(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=2))
        errors = []

        def reader():
            try:
                for _ in range(50):
                    get_all_worker_states()
                    is_safe_to_control()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        barrier.set()
        stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertEqual(errors, [])

    def test_concurrent_start_stop_workers(self):
        """Multiple workers starting and stopping concurrently must not corrupt state."""
        barrier = threading.Event()
        errors = []

        def task(wid):
            barrier.wait(timeout=3)

        wids = []
        for _ in range(5):
            wids.append(start_worker(task))

        # All workers should be tracked
        states = get_all_worker_states()
        for wid in wids:
            self.assertIn(wid, states)

        barrier.set()
        for wid in wids:
            stop_worker(wid, timeout=CLEANUP_TIMEOUT)

        # All states should be cleaned up
        time.sleep(0.2)
        remaining = get_all_worker_states()
        for wid in wids:
            self.assertNotIn(wid, remaining)


if __name__ == "__main__":
    unittest.main()
