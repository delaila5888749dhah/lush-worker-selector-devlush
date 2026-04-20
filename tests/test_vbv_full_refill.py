import unittest
from unittest.mock import MagicMock, call, patch

from integration.orchestrator import handle_outcome, refill_after_vbv_reload
from modules.common.types import BillingProfile, CardInfo, CycleContext, State, WorkerTask


def _make_card(number: str) -> CardInfo:
    return CardInfo(
        card_number=number,
        exp_month="07",
        exp_year="27",
        cvv="123",
        card_name="Test User",
    )


def _make_billing() -> BillingProfile:
    return BillingProfile(
        first_name="Jane",
        last_name="Doe",
        address="123 Main St",
        city="Portland",
        state="OR",
        zip_code="97201",
        phone="5035550100",
        email="jane@example.com",
        country="US",
    )


def _make_task(primary_card: CardInfo, order_queue=None) -> WorkerTask:
    return WorkerTask(
        recipient_email="recipient@example.com",
        amount=50,
        primary_card=primary_card,
        order_queue=order_queue or (),
    )


class TestVbvFullRefill(unittest.TestCase):
    # ── Legacy / partial-reload path (ctx.task is None) ──────────────────────

    def test_billing_profile_unchanged_after_vbv_reload(self):
        billing = _make_billing()
        ctx = CycleContext(cycle_id="cycle-1", worker_id="worker-1", billing_profile=billing)
        driver = MagicMock()
        new_card = _make_card("4000000000000002")

        refill_after_vbv_reload(driver, ctx, new_card)

        self.assertIs(ctx.billing_profile, billing)

    def test_card_changed_after_swap(self):
        primary = _make_card("4111111111111111")
        next_card = _make_card("4000000000000002")
        task = WorkerTask(
            recipient_email="recipient@example.com",
            amount=50,
            primary_card=primary,
            order_queue=(next_card,),
        )
        ctx = CycleContext(cycle_id="cycle-2", worker_id="worker-2", task=task)

        with patch("integration.orchestrator.is_payment_page_reloaded", return_value=False), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = MagicMock()
            action = handle_outcome(State("vbv_cancelled"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")
        self.assertIs(action[1], next_card)
        self.assertIsNot(action[1], primary)

    def test_refill_calls_both_billing_and_card_fillers(self):
        """Legacy path (ctx.task=None): only fill_billing + fill_card_fields."""
        billing = _make_billing()
        ctx = CycleContext(cycle_id="cycle-3", worker_id="worker-3", billing_profile=billing)
        driver = MagicMock()
        new_card = _make_card("4000000000000002")

        refill_after_vbv_reload(driver, ctx, new_card)

        driver.fill_billing.assert_called_once_with(billing)
        driver.fill_card_fields.assert_called_once_with(new_card)

    def test_legacy_path_does_not_call_full_sequence(self):
        """Legacy path must not invoke preflight or navigation methods."""
        billing = _make_billing()
        ctx = CycleContext(cycle_id="cycle-4", worker_id="worker-4", billing_profile=billing)
        driver = MagicMock()
        new_card = _make_card("4000000000000002")

        refill_after_vbv_reload(driver, ctx, new_card)

        driver.preflight_geo_check.assert_not_called()
        driver.navigate_to_egift.assert_not_called()
        driver.fill_egift_form.assert_not_called()
        driver.add_to_cart_and_checkout.assert_not_called()
        driver.select_guest_checkout.assert_not_called()
        driver.fill_payment_and_billing.assert_not_called()

    # ── Full-path reload (ctx.task available) ────────────────────────────────

    def test_full_refill_calls_all_six_steps(self):
        """Full path: all 6 steps called when ctx.task is set."""
        billing = _make_billing()
        primary = _make_card("4111111111111111")
        new_card = _make_card("4000000000000002")
        task = _make_task(primary)
        ctx = CycleContext(
            cycle_id="cycle-5", worker_id="worker-5",
            billing_profile=billing, task=task,
        )
        driver = MagicMock()

        refill_after_vbv_reload(driver, ctx, new_card)

        driver.preflight_geo_check.assert_called_once()
        driver.navigate_to_egift.assert_called_once()
        driver.fill_egift_form.assert_called_once_with(task, billing)
        driver.add_to_cart_and_checkout.assert_called_once()
        driver.select_guest_checkout.assert_called_once_with(billing.email)
        driver.fill_payment_and_billing.assert_called_once_with(new_card, billing)

    def test_full_refill_uses_new_card_not_primary(self):
        """fill_payment_and_billing must receive new_card, not ctx.task.primary_card."""
        billing = _make_billing()
        primary = _make_card("4111111111111111")
        new_card = _make_card("5500005555555559")
        task = _make_task(primary)
        ctx = CycleContext(
            cycle_id="cycle-6", worker_id="worker-6",
            billing_profile=billing, task=task,
        )
        driver = MagicMock()

        refill_after_vbv_reload(driver, ctx, new_card)

        card_arg = driver.fill_payment_and_billing.call_args[0][0]
        self.assertIs(card_arg, new_card)
        self.assertIsNot(card_arg, primary)

    def test_full_refill_step_order(self):
        """Steps must execute in the correct order: preflight→navigate→eGift→cart→guest→pay."""
        billing = _make_billing()
        primary = _make_card("4111111111111111")
        new_card = _make_card("4000000000000002")
        task = _make_task(primary)
        ctx = CycleContext(
            cycle_id="cycle-7", worker_id="worker-7",
            billing_profile=billing, task=task,
        )
        manager = MagicMock()
        driver = manager.driver

        refill_after_vbv_reload(driver, ctx, new_card)

        expected = [
            call.driver.preflight_geo_check(),
            call.driver.navigate_to_egift(),
            call.driver.fill_egift_form(task, billing),
            call.driver.add_to_cart_and_checkout(),
            call.driver.select_guest_checkout(billing.email),
            call.driver.fill_payment_and_billing(new_card, billing),
        ]
        self.assertEqual(manager.mock_calls, expected)

    def test_full_refill_does_not_call_legacy_methods(self):
        """Full path must not call fill_billing / fill_card_fields."""
        billing = _make_billing()
        primary = _make_card("4111111111111111")
        new_card = _make_card("4000000000000002")
        task = _make_task(primary)
        ctx = CycleContext(
            cycle_id="cycle-8", worker_id="worker-8",
            billing_profile=billing, task=task,
        )
        driver = MagicMock()

        refill_after_vbv_reload(driver, ctx, new_card)

        driver.fill_billing.assert_not_called()
        driver.fill_card_fields.assert_not_called()

    def test_full_refill_skipped_when_billing_profile_missing(self):
        """Nothing is called when ctx.billing_profile is None."""
        primary = _make_card("4111111111111111")
        new_card = _make_card("4000000000000002")
        task = _make_task(primary)
        ctx = CycleContext(
            cycle_id="cycle-9", worker_id="worker-9",
            billing_profile=None, task=task,
        )
        driver = MagicMock()

        refill_after_vbv_reload(driver, ctx, new_card)

        driver.preflight_geo_check.assert_not_called()
        driver.fill_payment_and_billing.assert_not_called()

    def test_full_refill_exception_is_logged_not_raised(self):
        """Driver exceptions during full refill must be swallowed (logged only)."""
        billing = _make_billing()
        primary = _make_card("4111111111111111")
        new_card = _make_card("4000000000000002")
        task = _make_task(primary)
        ctx = CycleContext(
            cycle_id="cycle-10", worker_id="worker-10",
            billing_profile=billing, task=task,
        )
        driver = MagicMock()
        driver.preflight_geo_check.side_effect = RuntimeError("CDP timeout")

        # Should not propagate the exception.
        try:
            refill_after_vbv_reload(driver, ctx, new_card)
        except RuntimeError:
            self.fail("refill_after_vbv_reload must not propagate driver exceptions")

    def test_handle_outcome_triggers_full_refill_on_reload(self):
        """handle_outcome calls refill_after_vbv_reload when page is reloaded."""
        billing = _make_billing()
        primary = _make_card("4111111111111111")
        new_card = _make_card("4000000000000002")
        task = _make_task(primary, order_queue=(new_card,))
        ctx = CycleContext(
            cycle_id="cycle-11", worker_id="worker-11",
            billing_profile=billing, task=task,
        )
        mock_driver = MagicMock()

        with patch("integration.orchestrator.is_payment_page_reloaded", return_value=True), \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.refill_after_vbv_reload") as mock_refill:
            mock_cdp._get_driver.return_value = mock_driver
            handle_outcome(State("vbv_cancelled"), task.order_queue, worker_id="worker-11", ctx=ctx)

        mock_refill.assert_called_once_with(mock_driver, ctx, new_card)

    def test_handle_outcome_skips_refill_when_not_reloaded(self):
        """handle_outcome does NOT call refill_after_vbv_reload when page is not reloaded."""
        billing = _make_billing()
        primary = _make_card("4111111111111111")
        new_card = _make_card("4000000000000002")
        task = _make_task(primary, order_queue=(new_card,))
        ctx = CycleContext(
            cycle_id="cycle-12", worker_id="worker-12",
            billing_profile=billing, task=task,
        )
        mock_driver = MagicMock()

        with patch("integration.orchestrator.is_payment_page_reloaded", return_value=False), \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.refill_after_vbv_reload") as mock_refill:
            mock_cdp._get_driver.return_value = mock_driver
            handle_outcome(State("vbv_cancelled"), task.order_queue, worker_id="worker-12", ctx=ctx)

        mock_refill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
