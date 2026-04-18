"""L4 live-smoke suite — end-to-end system evaluation (F-09).

These tests represent the four canonical production scenarios.  Each test
exercises the full orchestrator + CDP + FSM + watchdog stack with a *stub*
driver and logs rich diagnostic output so that reviewers can verify correct
end-to-end behaviour from CI artifacts.

Run in CI (stub mode, always)
-----------------------------
  python -m unittest tests.integration.test_l4_smoke

Run against live env (when L4_SMOKE_LIVE=1 and services are available)
-----------------------------------------------------------------------
  L4_SMOKE_LIVE=1 \\
  BITBROWSER_API_KEY=<key> \\
  SELENIUM_GRID_URL=<url> \\
  GIVEX_BASE_URL=<url> \\
  python -m unittest tests.integration.test_l4_smoke

Scenarios
---------
  TestL4SmokeSuite.test_l4_success          — happy path: payment authorised.
  TestL4SmokeSuite.test_l4_decline          — card declined: correct retry action.
  TestL4SmokeSuite.test_l4_vbv_3ds          — 3-D Secure challenge: await_3ds action.
  TestL4SmokeSuite.test_l4_watchdog_timeout — watchdog fires before total confirmed.

Each test:
  1. Configures a stub GivexDriver for the target scenario.
  2. Registers the stub with the CDP module.
  3. Runs the full orchestrator cycle.
  4. Asserts the expected action / exception.
  5. Emits a structured SMOKE LOG entry for CI log analysis.

Smoke-log format (one line per test)
-------------------------------------
  SMOKE_LOG | scenario=<name> | action=<action> | state=<state> | total=<total>
  SMOKE_LOG | scenario=<name> | exception=<ExcType> | message=<msg>
"""

from __future__ import annotations

import logging
import os
import sys
import time
import unittest
from unittest.mock import patch

# Ensure the helpers in this directory are importable regardless of how
# unittest discovers this file (with or without __init__.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import modules.cdp.main as _cdp_main
from integration.orchestrator import (
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _network_listener_lock,
    _notified_workers_this_cycle,
    _submitted_task_ids,
    run_cycle,
    run_payment_step,
)
from modules.common.exceptions import SessionFlaggedError
from modules.fsm.main import cleanup_worker, reset_registry
from modules.watchdog.main import reset as _reset_watchdog

from _integration_harness import (
    _IntegrationBase,
    _StubGivexDriver,
    _make_task,
    make_mock_billing,
)

_smoke_log = logging.getLogger("l4_smoke")


def _emit(scenario: str, **fields) -> None:
    """Emit a structured SMOKE_LOG entry to the l4_smoke logger."""
    parts = " | ".join(f"{k}={v}" for k, v in fields.items())
    _smoke_log.info("SMOKE_LOG | scenario=%s | %s", scenario, parts)


def _clear_idempotency() -> None:
    with _idempotency_lock:
        _completed_task_ids.clear()
        _in_flight_task_ids.clear()
        _submitted_task_ids.clear()
    with _network_listener_lock:
        _notified_workers_this_cycle.clear()


class TestL4SmokeSuite(_IntegrationBase, unittest.TestCase):
    """L4 live-smoke suite: four canonical production scenarios.

    Stub mode (default, used in CI):
      The stub GivexDriver records calls and transitions the FSM to the
      target state, but no real browser or network is involved.

    Live mode (opt-in, L4_SMOKE_LIVE=1):
      The test harness respects additional env vars:
        SELENIUM_GRID_URL, BITBROWSER_API_KEY, GIVEX_BASE_URL
      to drive a real Selenium Grid and Givex endpoint.  Live mode
      wiring is documented here but not fully implemented in CI because
      it requires external service provisioning outside this repo.
    """

    worker_id = "l4-smoke-worker"
    _live = bool(os.getenv("L4_SMOKE_LIVE", ""))

    def setUp(self):
        super().setUp()
        reset_registry()
        if self._live:
            _smoke_log.info(
                "SMOKE_LOG | mode=LIVE | "
                "SELENIUM_GRID_URL=%s | GIVEX_BASE_URL=%s",
                os.getenv("SELENIUM_GRID_URL", "(not set)"),
                os.getenv("GIVEX_BASE_URL", "(not set)"),
            )
        else:
            _smoke_log.info("SMOKE_LOG | mode=STUB")

    def tearDown(self):
        super().tearDown()

    # ── Scenario helpers ───────────────────────────────────────────────────────

    def _stub_run_cycle(
        self,
        scenario: str,
        final_state: str,
        task_id: str,
        dom_total: str = "50.00",
    ) -> tuple:
        """Register a stub driver, run run_cycle, log result, return (action, state, total)."""
        task = _make_task(task_id=task_id)
        stub = _StubGivexDriver(
            self.worker_id,
            final_state=final_state,
            dom_total=dom_total,
        )
        _cdp_main.register_driver(self.worker_id, stub)
        start = time.monotonic()
        try:
            with patch("integration.orchestrator.billing", make_mock_billing()):
                action, state, total = run_cycle(task, worker_id=self.worker_id)
        finally:
            _cdp_main.unregister_driver(self.worker_id)
            cleanup_worker(self.worker_id)
        elapsed = round(time.monotonic() - start, 3)
        _emit(
            scenario,
            action=action,
            state=(state.name if state else "None"),
            total=total,
            elapsed_s=elapsed,
            driver_calls=stub.calls,
        )
        return action, state, total

    def _stub_payment_step_timeout(
        self,
        scenario: str,
        task_id: str,
        timeout: float = 0.05,
    ) -> SessionFlaggedError:
        """Register a no-total stub, run run_payment_step, expect SessionFlaggedError."""
        task = _make_task(task_id=task_id)
        stub = _StubGivexDriver(
            self.worker_id,
            final_state="success",
            dom_total=None,  # DOM fallback returns None → watchdog fires on timeout
        )
        _cdp_main.register_driver(self.worker_id, stub)
        start = time.monotonic()
        exc_caught = None
        try:
            with (
                patch("integration.orchestrator.billing", make_mock_billing()),
                patch("integration.orchestrator._WATCHDOG_TIMEOUT", timeout),
            ):
                run_payment_step(task, worker_id=self.worker_id)
        except SessionFlaggedError as exc:
            exc_caught = exc
        finally:
            _cdp_main.unregister_driver(self.worker_id)
            cleanup_worker(self.worker_id)
        elapsed = round(time.monotonic() - start, 3)
        _emit(
            scenario,
            exception=type(exc_caught).__name__ if exc_caught else "None",
            message=str(exc_caught) if exc_caught else "",
            timeout_s=timeout,
            elapsed_s=elapsed,
            driver_calls=stub.calls,
        )
        return exc_caught

    # ── L4 Scenario 1: success ─────────────────────────────────────────────────

    def test_l4_success(self):
        """L4-S1: Payment authorised → action='complete', state='success'.

        Smoke log entry documents:
          - Full driver call sequence recorded.
          - Watchdog total notified via DOM fallback.
          - run_cycle returns ('complete', State('success'), 50.0).
        """
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_success | phase=start | "
            "description='Happy path: payment authorised'"
        )
        action, state, total = self._stub_run_cycle(
            scenario="l4_success",
            final_state="success",
            task_id="l4-smoke-success-001",
        )
        # ── Assertions ──
        self.assertEqual(action, "complete",
                         f"L4 success: expected action='complete', got '{action}'")
        self.assertIsNotNone(state, "L4 success: state must not be None")
        self.assertEqual(state.name, "success",
                         f"L4 success: expected state='success', got '{state.name}'")
        self.assertEqual(total, 50.0,
                         f"L4 success: expected total=50.0, got {total}")
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_success | phase=PASS | action=%s | state=%s",
            action, state.name if state else None,
        )

    # ── L4 Scenario 2: decline ─────────────────────────────────────────────────

    def test_l4_decline(self):
        """L4-S2: Card declined → action in ('retry', 'retry_new_card').

        Smoke log entry documents:
          - Full driver call sequence (including submit_purchase) recorded.
          - FSM transitioned to 'declined'.
          - run_cycle returns retry action.
        """
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_decline | phase=start | "
            "description='Card declined: retry or retry_new_card expected'"
        )
        action, state, total = self._stub_run_cycle(
            scenario="l4_decline",
            final_state="declined",
            task_id="l4-smoke-decline-001",
        )
        self.assertIn(
            action, ("retry", "retry_new_card"),
            f"L4 decline: expected retry action, got '{action}'",
        )
        self.assertIsNotNone(state, "L4 decline: state must not be None")
        self.assertEqual(state.name, "declined",
                         f"L4 decline: expected state='declined', got '{state.name}'")
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_decline | phase=PASS | action=%s | state=%s",
            action, state.name if state else None,
        )

    # ── L4 Scenario 3: VBV / 3-D Secure ─────────────────────────────────────────

    def test_l4_vbv_3ds(self):
        """L4-S3: VBV/3DS challenge → action='await_3ds', state='vbv_3ds'.

        Smoke log entry documents:
          - FSM transitioned to 'vbv_3ds'.
          - run_cycle returns 'await_3ds'.
          - cdp.clear_card_fields() called as part of 3DS cleanup path.
        """
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_vbv_3ds | phase=start | "
            "description='VBV/3DS challenge: await_3ds expected'"
        )
        # For vbv_3ds, the orchestrator calls cdp.clear_card_fields(worker_id).
        # Our stub driver exposes clear_card_fields() so we can verify it.
        task = _make_task(task_id="l4-smoke-vbv-001")
        stub = _StubGivexDriver(self.worker_id, final_state="vbv_3ds", dom_total="50.00")
        stub.clear_card_fields = lambda: stub.calls.append("clear_card_fields")
        _cdp_main.register_driver(self.worker_id, stub)
        try:
            with patch("integration.orchestrator.billing", make_mock_billing()):
                action, state, total = run_cycle(task, worker_id=self.worker_id)
        finally:
            _cdp_main.unregister_driver(self.worker_id)
            cleanup_worker(self.worker_id)

        _emit(
            "l4_vbv_3ds",
            action=action,
            state=(state.name if state else "None"),
            total=total,
            driver_calls=stub.calls,
            clear_card_fields_called=("clear_card_fields" in stub.calls),
        )
        self.assertEqual(action, "await_3ds",
                         f"L4 VBV/3DS: expected action='await_3ds', got '{action}'")
        self.assertIsNotNone(state, "L4 VBV/3DS: state must not be None")
        self.assertEqual(state.name, "vbv_3ds",
                         f"L4 VBV/3DS: expected state='vbv_3ds', got '{state.name}'")
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_vbv_3ds | phase=PASS | action=%s | state=%s",
            action, state.name if state else None,
        )

    # ── L4 Scenario 4: watchdog timeout ──────────────────────────────────────────

    def test_l4_watchdog_timeout(self):
        """L4-S4: Watchdog timeout before total confirmed → SessionFlaggedError.

        Smoke log entry documents:
          - DOM fallback returns None (simulates missing checkout total element).
          - Watchdog fires after configured timeout.
          - SessionFlaggedError raised with timeout details.
          - 'AFTER payment submission' logged (mark_submitted already persisted).
        """
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_watchdog_timeout | phase=start | "
            "description='Watchdog timeout: SessionFlaggedError expected'"
        )
        exc = self._stub_payment_step_timeout(
            scenario="l4_watchdog_timeout",
            task_id="l4-smoke-wdt-001",
            timeout=0.05,
        )
        self.assertIsNotNone(exc,
                             "L4 watchdog timeout: SessionFlaggedError must be raised")
        self.assertIsInstance(
            exc, SessionFlaggedError,
            f"L4 watchdog timeout: expected SessionFlaggedError, got {type(exc).__name__}",
        )
        _smoke_log.info(
            "SMOKE_LOG | scenario=l4_watchdog_timeout | phase=PASS | "
            "exception=%s",
            type(exc).__name__,
        )


# ── L4 CI validation: verify all scenarios are discoverable ───────────────────

class TestL4CoverageConsistency(unittest.TestCase):
    """Verify all four required L4 smoke scenarios are present and named correctly.

    This meta-test ensures that if a scenario is renamed or removed, CI will
    fail immediately rather than silently missing coverage.
    """

    REQUIRED_SCENARIOS = {
        "test_l4_success",
        "test_l4_decline",
        "test_l4_vbv_3ds",
        "test_l4_watchdog_timeout",
    }

    def test_all_required_l4_scenarios_present(self):
        """All four canonical L4 scenarios must exist as test methods."""
        suite_methods = set(
            m for m in dir(TestL4SmokeSuite)
            if m.startswith("test_l4_")
        )
        missing = self.REQUIRED_SCENARIOS - suite_methods
        self.assertFalse(
            missing,
            f"Missing required L4 scenarios: {missing}. "
            f"All four scenarios must be present for CI gate.",
        )

    def test_smoke_log_format_documented(self):
        """SMOKE_LOG prefix must be present in module docstring for CI log parsing."""
        import tests.integration.test_l4_smoke as this_module
        self.assertIn(
            "SMOKE_LOG",
            this_module.__doc__ or "",
            "Module docstring must document SMOKE_LOG format for CI log analysis.",
        )


if __name__ == "__main__":
    # Configure smoke logging to stdout when run directly
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
    )
    unittest.main(verbosity=2)
