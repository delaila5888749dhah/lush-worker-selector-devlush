"""Tests for Phase 2 orchestrator hardening (Issues #189, #193, #197).

Covers:
- test_vbv_challenge_returns_cancelled           — happy path
- test_vbv_challenge_returns_iframe_missing      — NoSuchElementException → 'iframe_missing'
- test_vbv_challenge_returns_cdp_fail            — WebDriverException → 'cdp_fail'
- test_vbv_challenge_returns_error               — unexpected exception → 'error'
- test_no_reload_invariant_warns_on_url_change   — URL change detected → log warning + refill
- test_no_reload_invariant_passes_on_same_url    — URL unchanged → no warning
- test_notify_success_passes_ctx                 — ctx propagated to send_success_notification
- test_build_success_caption_with_ctx            — billing + duration in caption
- test_build_success_caption_without_ctx_backward_compat — ctx=None still works
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver
from modules.common.types import BillingProfile, CardInfo, CycleContext, State, WorkerTask
import integration.orchestrator as orchestrator
from integration.orchestrator import handle_outcome
from modules.notification.telegram_notifier import build_success_caption


def _make_task():
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="07",
        exp_year="27",
        cvv="123",
        card_name="Tester",
    )
    next_card = CardInfo(
        card_number="4000000000000002",
        exp_month="07",
        exp_year="27",
        cvv="123",
        card_name="Tester",
    )
    return WorkerTask(
        recipient_email="recipient@example.com",
        amount=50,
        primary_card=card,
        order_queue=(next_card,),
    )


def _make_billing_profile():
    return BillingProfile(
        first_name="Test",
        last_name="User",
        address="123 Main St",
        city="Springfield",
        state="IL",
        zip_code="62701",
        phone="5555555555",
        email="test@example.com",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Issue #189 — handle_vbv_challenge exception classification
# ─────────────────────────────────────────────────────────────────────────────

class TestVbvChallengeExceptionClassification(unittest.TestCase):
    """handle_vbv_challenge returns classified string instead of bool."""

    def _make_gd(self):
        return GivexDriver(MagicMock())

    def test_vbv_challenge_returns_cancelled(self):
        """Happy path: all steps succeed → 'cancelled'."""
        gd = self._make_gd()
        with patch("modules.cdp.driver.vbv_dynamic_wait"), \
             patch("modules.cdp.driver.cdp_click_iframe_element"), \
             patch("modules.cdp.driver.handle_something_wrong_popup"):
            result = gd.handle_vbv_challenge()
        self.assertEqual(result, "cancelled")

    def test_vbv_challenge_returns_iframe_missing(self):
        """NoSuchElementException → 'iframe_missing' (benign)."""
        from selenium.common.exceptions import NoSuchElementException
        gd = self._make_gd()
        with patch("modules.cdp.driver.vbv_dynamic_wait",
                   side_effect=NoSuchElementException("no iframe")):
            result = gd.handle_vbv_challenge()
        self.assertEqual(result, "iframe_missing")

    def test_vbv_challenge_returns_iframe_missing_on_stale(self):
        """StaleElementReferenceException → 'iframe_missing' (benign)."""
        from selenium.common.exceptions import StaleElementReferenceException
        gd = self._make_gd()
        with patch("modules.cdp.driver.cdp_click_iframe_element",
                   side_effect=StaleElementReferenceException("stale")), \
             patch("modules.cdp.driver.vbv_dynamic_wait"):
            result = gd.handle_vbv_challenge()
        self.assertEqual(result, "iframe_missing")

    def test_vbv_challenge_returns_cdp_fail(self):
        """WebDriverException → 'cdp_fail' (caller may retry)."""
        from selenium.common.exceptions import WebDriverException
        gd = self._make_gd()
        with patch("modules.cdp.driver.vbv_dynamic_wait",
                   side_effect=WebDriverException("cdp error")):
            result = gd.handle_vbv_challenge()
        self.assertEqual(result, "cdp_fail")

    def test_vbv_challenge_returns_error_on_unexpected(self):
        """Unexpected exception → 'error' (caller decides)."""
        gd = self._make_gd()
        with patch("modules.cdp.driver.vbv_dynamic_wait",
                   side_effect=ValueError("unexpected")):
            result = gd.handle_vbv_challenge()
        self.assertEqual(result, "error")


class TestVbvChallengeOrchestratorCaller(unittest.TestCase):
    """Orchestrator handle_outcome dispatches correctly on new string returns."""

    def test_cancelled_proceeds_to_vbv_cancelled_branch(self):
        """result='cancelled' → handle_outcome with vbv_cancelled state."""
        task = _make_task()
        ctx = CycleContext(cycle_id="c-cancel", worker_id="w1", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "cancelled"
        driver.detect_page_state.return_value = "declined"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded", return_value=False):
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")
        self.assertEqual(ctx.swap_count, 1)

    def test_iframe_missing_proceeds_as_if_cancelled(self):
        """result='iframe_missing' → benign, proceeds to vbv_cancelled branch."""
        task = _make_task()
        ctx = CycleContext(cycle_id="c-missing", worker_id="w2", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "iframe_missing"
        driver.detect_page_state.return_value = "declined"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded", return_value=False):
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")

    def test_error_falls_through_to_await_3ds(self):
        """result='error' → no retry, falls through to 'await_3ds'."""
        task = _make_task()
        ctx = CycleContext(cycle_id="c-error", worker_id="w3", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "error"

        with patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action, "await_3ds")

    def test_cdp_fail_retries_once_then_falls_through(self):
        """result='cdp_fail' → retry once; if still cdp_fail → 'await_3ds'."""
        task = _make_task()
        ctx = CycleContext(cycle_id="c-cdpfail", worker_id="w4", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "cdp_fail"

        with patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action, "await_3ds")
        self.assertEqual(driver.handle_vbv_challenge.call_count, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Issue #193 — No-reload invariant guard
# ─────────────────────────────────────────────────────────────────────────────

class TestNoReloadInvariantGuard(unittest.TestCase):
    """No-reload invariant: warn + refill when URL changes unexpectedly during VBV cancel."""

    def _make_ctx(self):
        task = _make_task()
        ctx = CycleContext(
            cycle_id="c-noreload",
            worker_id="w-noreload",
            task=task,
            billing_profile=_make_billing_profile(),
        )
        return ctx

    def test_no_reload_invariant_warns_on_url_change(self):
        """URL changes between snapshot and check → warning logged + refill triggered."""
        ctx = self._make_ctx()
        task = ctx.task
        driver = MagicMock()
        driver.current_url = "https://checkout.givex.com/payment"

        url_calls = {"n": 0}

        def current_url_getter(self_):
            url_calls["n"] += 1
            if url_calls["n"] == 1:
                return "https://checkout.givex.com/payment"
            return "https://checkout.givex.com/reload-detected"

        type(driver).current_url = property(current_url_getter)

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded",
                   return_value=False) as mock_reload, \
             patch("integration.orchestrator.refill_after_vbv_reload") as mock_refill, \
             self.assertLogs("integration.orchestrator", level="WARNING") as cm:
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_cancelled"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")
        # Refill was called due to URL change
        mock_refill.assert_called_once()
        # Warning was emitted
        self.assertTrue(any("No-reload invariant" in line for line in cm.output))

    def test_no_reload_invariant_passes_on_same_url(self):
        """URL unchanged → no warning, no refill (happy path)."""
        ctx = self._make_ctx()
        task = ctx.task
        driver = MagicMock()
        driver.current_url = "https://checkout.givex.com/payment"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded",
                   return_value=False), \
             patch("integration.orchestrator.refill_after_vbv_reload") as mock_refill:
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_cancelled"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")
        # No refill triggered when URL is stable
        mock_refill.assert_not_called()

    def test_no_reload_invariant_skipped_when_reloaded(self):
        """When is_payment_page_reloaded() returns True, the reload refill runs without
        the invariant check (URL change is expected in this branch)."""
        ctx = self._make_ctx()
        task = ctx.task
        driver = MagicMock()
        driver.current_url = "https://checkout.givex.com/payment"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded",
                   return_value=True), \
             patch("integration.orchestrator.refill_after_vbv_reload") as mock_refill:
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_cancelled"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")
        # Refill called via normal reload path
        mock_refill.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Issue #197 — _notify_success passes ctx
# ─────────────────────────────────────────────────────────────────────────────

class TestNotifySuccessPassesCtx(unittest.TestCase):
    """_notify_success passes ctx through to send_success_notification."""

    def _make_task(self):
        return _make_task()

    def test_notify_success_passes_ctx_to_send(self):
        """ctx is forwarded to send_success_notification as keyword argument."""
        task = self._make_task()
        ctx = CycleContext(
            cycle_id="c-notify",
            worker_id="w-notify",
            task=task,
            billing_profile=_make_billing_profile(),
        )
        with patch("integration.orchestrator.cdp._get_driver",
                   side_effect=RuntimeError("no driver")), \
             patch("modules.notification.telegram_notifier.send_success_notification") as mock_send:
            orchestrator._notify_success(task, "w-notify", "50.00", ctx=ctx)
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        self.assertIn("ctx", call_kwargs)
        self.assertIs(call_kwargs["ctx"], ctx)

    def test_notify_success_backward_compat_without_ctx(self):
        """Calling _notify_success without ctx (ctx=None) still works."""
        task = self._make_task()
        with patch("integration.orchestrator.cdp._get_driver",
                   side_effect=RuntimeError("no driver")), \
             patch("modules.notification.telegram_notifier.send_success_notification") as mock_send:
            orchestrator._notify_success(task, "w-compat", "25.00")
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        self.assertIsNone(call_kwargs.get("ctx"))


# ─────────────────────────────────────────────────────────────────────────────
# Issue #197 — build_success_caption ctx integration
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildSuccessCaptionCtx(unittest.TestCase):
    """build_success_caption includes billing and duration when ctx is provided."""

    def _make_ctx_with_billing(self):
        task = _make_task()
        ctx = CycleContext(
            cycle_id="c-caption",
            worker_id="w-caption",
            task=task,
            billing_profile=_make_billing_profile(),
        )
        return ctx

    def test_build_success_caption_with_ctx_includes_billing(self):
        """Caption includes billing city/state/zip when ctx has billing_profile."""
        ctx = self._make_ctx_with_billing()
        task = ctx.task
        caption = build_success_caption("w-caption", task, "50.00", ctx=ctx)
        self.assertIn("Springfield", caption)
        self.assertIn("IL", caption)
        self.assertIn("62701", caption)

    def test_build_success_caption_with_ctx_includes_duration(self):
        """Caption includes cycle duration when ctx has duration_seconds callable."""
        ctx = self._make_ctx_with_billing()
        ctx_mock = MagicMock()
        ctx_mock.billing_profile = _make_billing_profile()
        ctx_mock.zip_code = None
        ctx_mock.duration_seconds = lambda: 12.5
        task = _make_task()
        caption = build_success_caption("w-caption", task, "50.00", ctx=ctx_mock)
        self.assertIn("12.5s", caption)

    def test_build_success_caption_without_ctx_backward_compat(self):
        """ctx=None: caption still renders with no billing/duration lines."""
        task = _make_task()
        caption = build_success_caption("w-compat", task, "30.00", ctx=None)
        self.assertIn("SUCCESS", caption)
        self.assertIn("30.00", caption)
        # No billing lines since ctx is None
        self.assertNotIn("Billing:", caption)


if __name__ == "__main__":
    unittest.main()
