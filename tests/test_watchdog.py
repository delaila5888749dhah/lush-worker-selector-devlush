import threading
import time
import unittest

from modules.watchdog.main import (
    notify_total,
    reset,
    reset_session,
    enable_network_monitor,
    wait_for_total,
)
from modules.common.exceptions import SessionFlaggedError

_WID = "worker-test"


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        reset()

    def tearDown(self):
        reset()

    def test_enable_network_monitor_allows_wait(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 42.0)
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 42.0)

    def test_wait_for_total_without_enable_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=1)

    def test_wait_for_total_timeout_raises_session_flagged_error(self):
        enable_network_monitor(_WID)
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(_WID, timeout=0.05)

    def test_notify_total_before_wait(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 99.99)
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 99.99)

    def test_notify_total_from_another_thread(self):
        enable_network_monitor(_WID)

        def signal():
            notify_total(_WID, 55.0)

        t = threading.Thread(target=signal)
        t.start()
        result = wait_for_total(_WID, timeout=2)
        t.join()
        self.assertEqual(result, 55.0)

    def test_wait_disables_monitor_on_success(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 10.0)
        wait_for_total(_WID, timeout=1)
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.05)

    def test_wait_disables_monitor_on_timeout(self):
        enable_network_monitor(_WID)
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(_WID, timeout=0.05)
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.05)

    def test_enable_resets_previous_state(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 100.0)
        enable_network_monitor(_WID)
        notify_total(_WID, 200.0)
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 200.0)

    def test_reset_clears_state(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 50.0)
        reset()
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.05)

    def test_different_workers_are_isolated(self):
        wid_a = "worker-a"
        wid_b = "worker-b"
        enable_network_monitor(wid_a)
        enable_network_monitor(wid_b)
        notify_total(wid_a, 111.0)
        notify_total(wid_b, 222.0)
        result_a = wait_for_total(wid_a, timeout=1)
        result_b = wait_for_total(wid_b, timeout=1)
        self.assertEqual(result_a, 111.0)
        self.assertEqual(result_b, 222.0)

    def test_notify_total_noop_for_unknown_worker(self):
        # Should not raise and should not create a session
        notify_total("nonexistent-worker", 42.0)
        with self.assertRaises(RuntimeError):
            wait_for_total("nonexistent-worker", timeout=0.01)

    def test_concurrent_enable_does_not_delete_new_session(self):
        """TOCTOU regression: finally must not delete a replacement session.

        Verifies that when enable_network_monitor() replaces the registry entry
        while wait_for_total() is blocked on session A's event, the identity-
        check cleanup in finally only removes session A, leaving session B
        intact and usable.
        """
        from modules.watchdog.main import _watchdog_registry, _registry_lock

        # --- Phase 1: create session A; instrument its wait to signal entry ---
        enable_network_monitor(_WID)  # session A

        wait_entered = threading.Event()

        with _registry_lock:
            session_a = _watchdog_registry[_WID]

        original_wait = session_a.event.wait

        def instrumented_wait(timeout=None):
            wait_entered.set()
            return original_wait(timeout=timeout)

        session_a.event.wait = instrumented_wait

        errors = []

        def blocked_wait():
            try:
                wait_for_total(_WID, timeout=2.0)
            except SessionFlaggedError:
                pass  # expected if session A times out
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=blocked_wait)
        t.start()

        # Deterministic: wait until thread is actually inside event.wait()
        self.assertTrue(
            wait_entered.wait(timeout=2),
            "blocked_wait thread did not enter event.wait() in time",
        )

        # --- Phase 2: replace session A with session B, pre-signal B ---
        enable_network_monitor(_WID)  # session B replaces session A
        notify_total(_WID, 77.0)  # signal session B

        # Unblock session A so the thread finishes quickly
        session_a.event.set()

        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "blocked_wait thread did not finish in time")
        self.assertEqual(errors, [], f"unexpected error in blocked_wait: {errors}")

        # --- Phase 3: session B must still be alive in the registry ---
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 77.0)

    def test_event_wait_is_outside_registry_lock(self):
        """Prove event.wait() is called without holding _registry_lock.

        If wait_for_total() held _registry_lock during event.wait(), a
        concurrent notify_total() would deadlock trying to acquire it.
        This test verifies the lock is acquirable while the waiter sleeps.
        """
        from modules.watchdog.main import _watchdog_registry, _registry_lock

        enable_network_monitor(_WID)
        wait_entered = threading.Event()

        with _registry_lock:
            session = _watchdog_registry[_WID]

        original_wait = session.event.wait

        def instrumented_wait(timeout=None):
            wait_entered.set()
            return original_wait(timeout=timeout)

        session.event.wait = instrumented_wait

        def do_wait():
            try:
                wait_for_total(_WID, timeout=2.0)
            except SessionFlaggedError:
                pass

        t = threading.Thread(target=do_wait)
        t.start()

        self.assertTrue(wait_entered.wait(timeout=2), "Thread did not reach event.wait()")

        # The lock must be free while the waiter is sleeping in event.wait()
        acquired = _registry_lock.acquire(blocking=True, timeout=0.5)
        self.assertTrue(acquired, "_registry_lock was held during event.wait() — deadlock risk")
        if acquired:
            _registry_lock.release()

        notify_total(_WID, 1.0)
        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "Waiter thread did not finish after notify — possible hang")

    def test_notify_after_reset_session_is_noop(self):
        """notify_total() after reset_session() is a side-effect-free no-op."""
        enable_network_monitor(_WID)
        reset_session(_WID)
        # Late notify must not create a session or raise
        notify_total(_WID, 42.0)
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.01)

    def test_stale_notify_from_thread_after_reset_is_harmless(self):
        """Late notify_total() from another thread after reset leaves no trace."""
        enable_network_monitor(_WID)
        notify_ready = threading.Event()
        reset_done = threading.Event()

        def late_notify():
            notify_ready.set()
            reset_done.wait(timeout=2)
            # session is already gone at this point
            notify_total(_WID, 99.9)

        t = threading.Thread(target=late_notify)
        t.start()

        notify_ready.wait(timeout=1)
        reset_session(_WID)
        reset_done.set()

        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "Late-notify thread did not finish — possible hang")

        # No session must remain; notify must have been a no-op
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.01)

    def test_recreate_after_reset_gives_fresh_isolated_session(self):
        """enable_network_monitor() after reset_session() always gives a fresh session.

        A pre-signalled old session must not bleed into the new one.
        """
        enable_network_monitor(_WID)
        notify_total(_WID, 1.0)   # pre-signal session A
        reset_session(_WID)       # discard session A before it is consumed

        enable_network_monitor(_WID)  # fresh session B
        notify_total(_WID, 2.0)
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 2.0)

    def test_worker_isolation_notify_does_not_cross_workers(self):
        """notify_total() for worker A must never signal worker B's session."""
        wid_a = "worker-iso-a"
        wid_b = "worker-iso-b"
        enable_network_monitor(wid_a)
        enable_network_monitor(wid_b)

        notify_total(wid_a, 10.0)
        # wid_b must still be waiting (no spurious signal)
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(wid_b, timeout=0.05)

        result_a = wait_for_total(wid_a, timeout=1)
        self.assertEqual(result_a, 10.0)
