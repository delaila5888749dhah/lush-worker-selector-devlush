import unittest
from unittest.mock import MagicMock, patch

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


class TestVbvFullRefill(unittest.TestCase):
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
        billing = _make_billing()
        ctx = CycleContext(cycle_id="cycle-3", worker_id="worker-3", billing_profile=billing)
        driver = MagicMock()
        new_card = _make_card("4000000000000002")

        refill_after_vbv_reload(driver, ctx, new_card)

        driver.fill_billing.assert_called_once_with(billing)
        driver.fill_card_fields.assert_called_once_with(new_card)


if __name__ == "__main__":
    unittest.main()
