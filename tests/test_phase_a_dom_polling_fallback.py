"""Tests for Phase A DOM polling fallback (issue F2 audit, ALLOW_DOM_ONLY_WATCHDOG).

When a stock Selenium driver lacks ``add_cdp_listener`` and the operator has
opted into ``ALLOW_DOM_ONLY_WATCHDOG=1``, the orchestrator must:

1. NOT silently skip the listener install (the legacy bug — surfaces as a
   10-second Phase A cycle timeout in production).
2. Spawn a daemon thread that polls the DOM until ``_notify_total_from_dom``
   succeeds, the worker is in ``_notified_workers_this_cycle``, or the
   per-worker stop event is set.
3. Log a WARNING describing the degraded mode.

The strict (env unset) path keeps the legacy silent-skip behaviour so that
in-process test stubs continue to work without a probe at register time.
"""
# pylint: disable=too-few-public-methods,no-self-use
import logging
import os
import threading
import time
import unittest
from unittest.mock import patch

from integration.orchestrator import (
    _dom_polling_stop_events,
    _network_listener_lock,
    _notified_workers_this_cycle,
    _setup_network_total_listener,
    _start_phase_a_dom_polling,
    _stop_phase_a_dom_polling,
)


class _DriverWithoutCdpListener:
    """Mock driver that exposes execute_cdp_cmd / execute_script but no listener."""

    def __init__(self, dom_total: str = "49.99"):
        self._dom_total = dom_total
        self.cdp_calls = []
        self.script_calls = 0

    def execute_cdp_cmd(self, command, params):
        self.cdp_calls.append((command, params))
        return None

    def execute_script(self, *_args, **_kwargs):
        self.script_calls += 1
        return self._dom_total


class _DriverWithCdpListener(_DriverWithoutCdpListener):
    """Same as above but exposes a callable add_cdp_listener."""

    def __init__(self, dom_total: str = "49.99"):
        super().__init__(dom_total)
        self.listeners = []

    def add_cdp_listener(self, event, callback):
        self.listeners.append((event, callback))


def _drain_polling_thread(worker_id: str, timeout: float = 2.0) -> None:
    """Wait for the per-worker stop event to be cleared from the registry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _network_listener_lock:
            present = worker_id in _dom_polling_stop_events
        if not present:
            return
        time.sleep(0.05)


class PhaseADomPollingFallbackTests(unittest.TestCase):
    """ALLOW_DOM_ONLY_WATCHDOG fallback path."""

    def setUp(self):
        self._prev_env = os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)
        with _network_listener_lock:
            _notified_workers_this_cycle.discard("phase-a-worker")

    def tearDown(self):
        _stop_phase_a_dom_polling("phase-a-worker")
        _drain_polling_thread("phase-a-worker")
        with _network_listener_lock:
            _notified_workers_this_cycle.discard("phase-a-worker")
            _dom_polling_stop_events.pop("phase-a-worker", None)
        os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)
        if self._prev_env is not None:
            os.environ["ALLOW_DOM_ONLY_WATCHDOG"] = self._prev_env

    def test_setup_starts_polling_when_listener_missing_and_env_set(self):
        """env=1 + missing listener => polling thread + WARNING + DOM notify."""
        driver = _DriverWithoutCdpListener(dom_total="123.45")
        with patch.dict(os.environ, {"ALLOW_DOM_ONLY_WATCHDOG": "1"}):
            with patch("integration.orchestrator.watchdog") as mock_wd, \
                 self.assertLogs("integration.orchestrator", level=logging.WARNING) as cm:
                _setup_network_total_listener(driver, "phase-a-worker")
                # Allow the polling thread one tick to fire _notify_total_from_dom.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if mock_wd.notify_total.called:
                        break
                    time.sleep(0.05)
        joined = "\n".join(cm.output)
        self.assertIn("ALLOW_DOM_ONLY_WATCHDOG", joined)
        self.assertIn("Phase A DOM polling fallback", joined)
        mock_wd.notify_total.assert_called_with("phase-a-worker", 123.45)

    def test_setup_no_polling_when_listener_missing_and_env_unset(self):
        """env unset => legacy silent skip; no polling thread is spawned."""
        driver = _DriverWithoutCdpListener()
        # Make sure env is not set.
        os.environ.pop("ALLOW_DOM_ONLY_WATCHDOG", None)
        with patch("integration.orchestrator.watchdog") as mock_wd:
            _setup_network_total_listener(driver, "phase-a-worker")
            # Give any (incorrectly spawned) polling thread time to fire.
            time.sleep(0.6)
        with _network_listener_lock:
            self.assertNotIn("phase-a-worker", _dom_polling_stop_events)
        mock_wd.notify_total.assert_not_called()

    def test_setup_no_polling_when_listener_present(self):
        """Callable listener => normal CDP path; no polling thread is spawned."""
        driver = _DriverWithCdpListener()
        with patch.dict(os.environ, {"ALLOW_DOM_ONLY_WATCHDOG": "1"}):
            with patch("integration.orchestrator.watchdog"):
                _setup_network_total_listener(driver, "phase-a-worker")
        self.assertEqual(len(driver.listeners), 1)
        self.assertEqual(driver.listeners[0][0], "Network.responseReceived")
        with _network_listener_lock:
            self.assertNotIn("phase-a-worker", _dom_polling_stop_events)

    def test_polling_thread_stops_on_stop_event(self):
        """_stop_phase_a_dom_polling exits the polling thread promptly."""
        driver = _DriverWithoutCdpListener(dom_total="not-a-number")
        with patch("integration.orchestrator.watchdog"):
            _start_phase_a_dom_polling(driver, "phase-a-worker")
            with _network_listener_lock:
                self.assertIn("phase-a-worker", _dom_polling_stop_events)
            _stop_phase_a_dom_polling("phase-a-worker")
            _drain_polling_thread("phase-a-worker")
        with _network_listener_lock:
            self.assertNotIn("phase-a-worker", _dom_polling_stop_events)

    def test_polling_thread_stops_after_first_notify(self):
        """Once _notified_workers_this_cycle has the worker, polling exits."""
        driver = _DriverWithoutCdpListener(dom_total="50.00")
        with patch("integration.orchestrator.watchdog"):
            _start_phase_a_dom_polling(driver, "phase-a-worker")
            # First _notify_total_from_dom call will add the worker to the set
            # and the next loop iteration must terminate.
            _drain_polling_thread("phase-a-worker", timeout=3.0)
        with _network_listener_lock:
            self.assertNotIn("phase-a-worker", _dom_polling_stop_events)
            self.assertIn("phase-a-worker", _notified_workers_this_cycle)

    def test_starting_polling_replaces_previous_thread(self):
        """Re-starting polling for the same worker replaces the prior stop event."""
        driver = _DriverWithoutCdpListener(dom_total="not-a-number")
        with patch("integration.orchestrator.watchdog"):
            _start_phase_a_dom_polling(driver, "phase-a-worker")
            with _network_listener_lock:
                first = _dom_polling_stop_events["phase-a-worker"]
            _start_phase_a_dom_polling(driver, "phase-a-worker")
            with _network_listener_lock:
                second = _dom_polling_stop_events["phase-a-worker"]
            self.assertIsNot(first, second)
            # The previous stop event must have been signalled.
            self.assertTrue(first.is_set())
            _stop_phase_a_dom_polling("phase-a-worker")
            _drain_polling_thread("phase-a-worker")


if __name__ == "__main__":
    unittest.main()
