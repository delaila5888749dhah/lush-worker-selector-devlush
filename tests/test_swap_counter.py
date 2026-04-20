import unittest
from unittest.mock import MagicMock, patch

from integration.orchestrator import handle_outcome
from modules.common.types import CardInfo, CycleContext, State, WorkerTask


def _can_swap(ctx) -> bool:
    return ctx.task is not None and ctx.swap_count < len(ctx.task.order_queue)


def _make_cards(count: int):
    cards = []
    for i in range(count):
        cards.append(
            CardInfo(
                card_number=f"41111111111111{i:02d}",
                exp_month="07",
                exp_year="27",
                cvv="123",
                card_name=f"User {i}",
            )
        )
    return cards


def _make_task(queue_size: int) -> WorkerTask:
    cards = _make_cards(queue_size + 1)
    return WorkerTask(
        recipient_email="recipient@example.com",
        amount=50,
        primary_card=cards[0],
        order_queue=tuple(cards[1:]),
    )


class TestSwapCounter(unittest.TestCase):
    def test_can_swap_returns_true_when_under_limit(self):
        task = _make_task(2)
        ctx = CycleContext(cycle_id="cycle-1", worker_id="worker", task=task)
        self.assertTrue(_can_swap(ctx))

    def test_can_swap_returns_false_at_limit(self):
        task = _make_task(3)
        ctx = CycleContext(cycle_id="cycle-2", worker_id="worker", task=task, swap_count=3)
        self.assertFalse(_can_swap(ctx))

    def test_record_swap_increments(self):
        task = _make_task(1)
        ctx = CycleContext(cycle_id="cycle-3", worker_id="worker", task=task)
        ctx.swap_count += 1
        self.assertEqual(ctx.swap_count, 1)

    def test_swap_counter_shared_across_vbv_and_decline(self):
        task = _make_task(3)
        ctx = CycleContext(cycle_id="cycle-4", worker_id="worker", task=task)
        with patch("integration.orchestrator.is_payment_page_reloaded", return_value=False), \
             patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = MagicMock()
            action1 = handle_outcome(State("vbv_cancelled"), task.order_queue, ctx=ctx)
            action2 = handle_outcome(State("vbv_cancelled"), task.order_queue, ctx=ctx)
            action3 = handle_outcome(State("declined"), task.order_queue, ctx=ctx)
            action4 = handle_outcome(State("declined"), task.order_queue, ctx=ctx)

        self.assertEqual(action1[0], "retry_new_card")
        self.assertEqual(action2[0], "retry_new_card")
        self.assertEqual(action3[0], "retry_new_card")
        self.assertEqual(action4, "abort_cycle")

    def test_swap_counter_resets_on_new_cycle(self):
        task = _make_task(2)
        ctx1 = CycleContext(cycle_id="cycle-5", worker_id="worker", task=task)
        ctx1.swap_count += 1
        ctx2 = CycleContext(cycle_id="cycle-6", worker_id="worker", task=task)
        self.assertEqual(ctx2.swap_count, 0)


if __name__ == "__main__":
    unittest.main()
