import unittest
from unittest.mock import MagicMock, call, patch

from integration.orchestrator import handle_outcome
from modules.common.types import CardInfo, CycleContext, State, WorkerTask
from modules.cdp.driver import GivexDriver


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


class TestVbvChallengeWiring(unittest.TestCase):
    def test_vbv_3ds_state_invokes_dynamic_wait_then_iframe_click_then_popup_handler(self):
        driver = MagicMock()
        gd = GivexDriver(driver)
        calls = []

        def record(name):
            calls.append(name)

        with patch("modules.cdp.driver.vbv_dynamic_wait", side_effect=lambda *args, **kwargs: record("wait")), \
             patch("modules.cdp.driver.cdp_click_iframe_element", side_effect=lambda *args, **kwargs: record("click")), \
             patch("modules.cdp.driver.handle_something_wrong_popup", side_effect=lambda *args, **kwargs: record("popup")):
            result = gd.handle_vbv_challenge()

        self.assertTrue(result)
        self.assertEqual(calls, ["wait", "click", "popup"])

    def test_vbv_3ds_transitions_to_vbv_cancelled_on_success(self):
        task = _make_task()
        ctx = CycleContext(cycle_id="cycle-1", worker_id="worker-1", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = True
        driver.detect_page_state.return_value = "declined"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded", return_value=False):
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")
        self.assertEqual(ctx.swap_count, 1)
        driver.handle_vbv_challenge.assert_called_once()
        driver.detect_page_state.assert_called_once()

    def test_vbv_3ds_falls_back_to_await_3ds_on_exception(self):
        task = _make_task()
        ctx = CycleContext(cycle_id="cycle-2", worker_id="worker-2", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.side_effect = RuntimeError("boom")

        with patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action, "await_3ds")

    def test_vbv_challenge_enforces_wait_before_click_sequence(self):
        driver = MagicMock()
        gd = GivexDriver(driver)
        order = []

        def record_wait(_value):
            order.append("sleep")

        def record_click(*_args, **_kwargs):
            order.append("click")

        with patch("modules.cdp.driver.time.sleep", side_effect=record_wait), \
             patch("modules.cdp.driver.cdp_click_iframe_element", side_effect=record_click), \
             patch("modules.cdp.driver.handle_something_wrong_popup", return_value=False):
            gd.handle_vbv_challenge()

        self.assertIn("sleep", order)
        self.assertIn("click", order)
        self.assertLess(order.index("sleep"), order.index("click"))


if __name__ == "__main__":
    unittest.main()
