"""Phase 6 Task 1 — billing audit event emitted exactly once per cycle.

Even when a cycle runs ``run_payment_step`` multiple times because of
card-swap retries (declined → next card), the ``billing_selection`` audit
event must fire *only* on the initial successful ``billing.select_profile``
call, not on subsequent retries that reuse the same profile.

Blueprint §12 line 693: "Mỗi lần billing.select_profile() trả về thành
công..." — one audit event per actual select_profile() call, not per
run_payment_step() invocation.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from integration import orchestrator
from integration.orchestrator import run_payment_step
from modules.common.types import BillingProfile


def _profile() -> BillingProfile:
    return BillingProfile(
        first_name="Alice",
        last_name="Smith",
        address="1 Main St",
        city="LA",
        state="CA",
        zip_code="90210",
        phone="5555551234",
        email="alice@example.com",
    )


class _FakeTask:
    task_id = "t-1"
    order_queue = ()


class BillingAuditSingleEmitTests(unittest.TestCase):
    """Unit-level tests for the audit-emit gate in ``run_payment_step``."""

    def _run_with_audit_capture(self, _profile_arg):
        """Run run_payment_step with heavy external dependencies stubbed and
        collect ``billing_selection`` audit messages."""
        captured = []
        select_calls = []

        def _record_select(*_a, **_kw):
            select_calls.append(1)
            return _profile()

        def _audit_info(fmt, *args, **_kw):
            msg = fmt % args if args else fmt
            if "billing_selection" in msg:
                captured.append(msg)

        with (
            patch("integration.orchestrator.billing.select_profile",
                  side_effect=_record_select),
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_wd,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._AUDIT_LOGGER") as mock_audit,
        ):
            mock_wd.wait_for_total.return_value = 10.0
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_audit.info.side_effect = _audit_info
            run_payment_step(
                _FakeTask(), zip_code="90210", worker_id="w1",
                _profile=_profile_arg,
            )
        return captured, select_calls

    def test_audit_emitted_when_profile_is_none(self):
        """No pre-selected profile → run_payment_step calls select and emits."""
        captured, selects = self._run_with_audit_capture(None)
        self.assertEqual(len(selects), 1)
        self.assertEqual(len(captured), 1)
        # Sanity: payload is JSON.
        payload = captured[0].split("billing_selection ", 1)[1]
        evt = json.loads(payload)
        self.assertEqual(evt["event_type"], "billing_selection")
        self.assertEqual(evt["worker_id"], "w1")

    def test_audit_not_emitted_when_profile_reused(self):
        """Pre-selected profile passed in → no select and no audit."""
        captured, selects = self._run_with_audit_capture(_profile())
        self.assertEqual(len(selects), 0)
        self.assertEqual(
            len(captured), 0,
            msg="Reused profile must not re-emit billing audit event",
        )


class BillingAuditCycleSwapRetryTests(unittest.TestCase):
    """Integration-ish: simulate a cycle with N swap retries and ensure the
    audit event is emitted exactly once."""

    def test_exactly_one_event_per_cycle_with_three_swap_retries(self):
        captured = []
        select_calls = []

        def _select(*_a, **_kw):
            select_calls.append(1)
            return _profile()

        def _audit_info(fmt, *args, **_kw):
            msg = fmt % args if args else fmt
            if "billing_selection" in msg:
                captured.append(msg)

        # Emulate run_payment_step calling run_payment_step 3 times for the
        # same cycle — the first without _profile (fresh select), then 2 more
        # with the previously-selected _profile (swap retry path).
        with (
            patch("integration.orchestrator.billing.select_profile",
                  side_effect=_select),
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_wd,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._AUDIT_LOGGER") as mock_audit,
        ):
            mock_wd.wait_for_total.return_value = 10.0
            mock_fsm.get_current_state_for_worker.return_value = None
            mock_audit.info.side_effect = _audit_info

            # Initial attempt: fresh select.
            run_payment_step(_FakeTask(), zip_code="90210", worker_id="w2")
            # Reuse profile for swap retries.
            reused = _profile()
            run_payment_step(
                _FakeTask(), zip_code="90210", worker_id="w2", _profile=reused,
            )
            run_payment_step(
                _FakeTask(), zip_code="90210", worker_id="w2", _profile=reused,
            )
            run_payment_step(
                _FakeTask(), zip_code="90210", worker_id="w2", _profile=reused,
            )

        self.assertEqual(len(select_calls), 1)
        self.assertEqual(
            len(captured), 1,
            msg=f"Expected exactly 1 billing_selection event, got {len(captured)}",
        )


class BillingAuditGrepAcceptanceTests(unittest.TestCase):
    """Acceptance: billing audit emit is centralized in one helper call-site."""

    def test_audit_call_sites_are_gated(self):
        import inspect
        helper_src = inspect.getsource(orchestrator._select_profile_with_audit)
        run_payment_src = inspect.getsource(orchestrator.run_payment_step)
        run_cycle_src = inspect.getsource(orchestrator.run_cycle)
        # Exactly one audited emit site exists in the helper.
        self.assertEqual(helper_src.count("_emit_billing_audit_event("), 1)
        # Callers reuse the helper instead of calling the emit directly.
        self.assertNotIn("_emit_billing_audit_event(", run_payment_src)
        self.assertNotIn("_emit_billing_audit_event(", run_cycle_src)


# ─────────────────────────────────────────────────────────────────────────────
# selection_method must reflect the *actual* outcome of profile selection
# (zip_match vs round_robin), not just whether a zip was requested.
# ─────────────────────────────────────────────────────────────────────────────


def _audit_profile(name: str, zip_code: str) -> "BillingProfile":
    return BillingProfile(
        first_name=name, last_name="User", address="1 Test St",
        city="Testcity", state="NY", zip_code=zip_code,
        phone="2125550001", email="test@example.com",
    )


def _set_audit_master_pool(profiles: list) -> None:
    """Populate the billing master pool directly so a real select_profile
    call can run end-to-end without disk I/O."""
    from modules.billing import main as billing_main
    with billing_main._MASTER_POOL_LOCK:  # pylint: disable=protected-access
        billing_main._MASTER_POOL = list(profiles)  # pylint: disable=protected-access
        billing_main._MASTER_POOL_LOADED = True  # pylint: disable=protected-access


class BillingAuditOutcomeMethodTests(unittest.TestCase):
    """selection_method must reflect the *actual* outcome — including the
    case where a zip was requested but no profile in the pool matched it
    (must label as round_robin, not zip_match)."""

    def setUp(self):
        from modules.billing import main as billing_main
        billing_main._reset_state()  # pylint: disable=protected-access

    def tearDown(self):
        from modules.billing import main as billing_main
        billing_main._reset_state()  # pylint: disable=protected-access

    def _capture_audit_event(self, zip_code: str, worker_id: str) -> dict:
        captured: list[str] = []

        def _audit_info(fmt, *args, **_kw):
            msg = fmt % args if args else fmt
            if "billing_selection" in msg:
                captured.append(msg)

        with patch("integration.orchestrator._AUDIT_LOGGER") as mock_audit:
            mock_audit.info.side_effect = _audit_info
            orchestrator._select_profile_with_audit(  # pylint: disable=protected-access
                zip_code=zip_code,
                worker_id=worker_id,
                task_id="t-method",
                include_worker_id=True,
            )

        self.assertEqual(
            len(captured), 1,
            msg=f"Expected exactly 1 billing_selection event, got {len(captured)}",
        )
        payload = captured[0].split("billing_selection ", 1)[1]
        return json.loads(payload)

    def test_audit_method_round_robin_when_zip_no_match(self):
        """Pool of profiles whose zips ≠ requested zip → audit must label
        the outcome as ``round_robin`` (the actual sequential fallback that
        executed), not ``zip_match`` (which is just the request intent)."""
        _set_audit_master_pool([
            _audit_profile("A", "11111"),
            _audit_profile("B", "22222"),
            _audit_profile("C", "33333"),
        ])
        evt = self._capture_audit_event(zip_code="99999", worker_id="w-rr")
        self.assertEqual(evt["selection_method"], "round_robin")
        self.assertEqual(evt["requested_zip"], "99999")

    def test_audit_method_zip_match_when_zip_hits(self):
        """Pool contains a profile with matching zip → audit must label the
        outcome as ``zip_match``."""
        _set_audit_master_pool([
            _audit_profile("A", "00001"),
            _audit_profile("Match", "10001"),
            _audit_profile("B", "00002"),
        ])
        evt = self._capture_audit_event(zip_code="10001", worker_id="w-zm")
        self.assertEqual(evt["selection_method"], "zip_match")
        self.assertEqual(evt["requested_zip"], "10001")


if __name__ == "__main__":
    unittest.main()
