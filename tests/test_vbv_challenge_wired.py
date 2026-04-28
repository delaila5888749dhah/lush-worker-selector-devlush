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

        self.assertEqual(result, "cancelled")
        self.assertEqual(calls, ["wait", "click", "popup"])

    def test_vbv_3ds_transitions_to_vbv_cancelled_on_success(self):
        task = _make_task()
        ctx = CycleContext(cycle_id="cycle-1", worker_id="worker-1", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "cancelled"
        # Site rolled back to declined after VBV cancel; orchestrator must
        # honour the re-probed state instead of hardcoding 'vbv_cancelled'.
        driver.detect_page_state.return_value = "declined"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded", return_value=False):
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        # Re-probed 'declined' → swap card via the declined fork (no
        # vbv-cancelled refill, hence is_payment_page_reloaded must NOT
        # be consulted because that path is gated on vbv_cancelled).
        self.assertEqual(action[0], "retry_new_card")
        self.assertEqual(ctx.swap_count, 1)
        driver.handle_vbv_challenge.assert_called_once()
        driver.detect_page_state.assert_called_once()

    def test_vbv_cancel_reprobe_success_returns_complete(self):
        """Site auto-approves after VBV cancel — no card swap should occur."""
        task = _make_task()
        ctx = CycleContext(cycle_id="cycle-success", worker_id="worker-success", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "cancelled"
        driver.detect_page_state.return_value = "success"

        with patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action, "complete")
        self.assertEqual(ctx.swap_count, 0)

    def test_vbv_cancel_reprobe_unknown_falls_back_to_vbv_cancelled(self):
        """Unknown post-state falls back to legacy vbv_cancelled branch."""
        task = _make_task()
        ctx = CycleContext(cycle_id="cycle-fallback", worker_id="worker-fallback", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "cancelled"
        driver.detect_page_state.return_value = "something_unexpected"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded", return_value=False):
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        # Fallback to legacy vbv_cancelled flow → swap card.
        self.assertEqual(action[0], "retry_new_card")
        self.assertEqual(ctx.swap_count, 1)

    def test_vbv_cancel_reprobe_vbv_3ds_falls_back_to_vbv_cancelled(self):
        """vbv_3ds re-probe must NOT recurse — fall back to vbv_cancelled."""
        task = _make_task()
        ctx = CycleContext(cycle_id="cycle-recurse", worker_id="worker-recurse", task=task)
        driver = MagicMock()
        driver.handle_vbv_challenge.return_value = "cancelled"
        driver.detect_page_state.return_value = "vbv_3ds"

        with patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded", return_value=False):
            mock_cdp._get_driver.return_value = driver
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        self.assertEqual(action[0], "retry_new_card")
        self.assertEqual(ctx.swap_count, 1)

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

    def test_popup_close_clears_card_fields_end_to_end(self):
        """P1-2 integration: popup appears → close + clear + refill new card.

        Drives the full VBV flow end-to-end with a real (non-mocked)
        ``handle_something_wrong_popup``, asserting that (1) the popup
        close click is dispatched, (2) the driver's card fields are
        wiped via ``clear_card_fields_cdp`` before the orchestrator
        swap path fires, and (3) the orchestrator then clears again
        and fills the next card from the order queue (re-submit ready).
        """
        from modules.cdp.driver import SEL_POPUP_CLOSE, PopupCloseOutcome
        import modules.cdp.driver as drv

        task = _make_task()
        ctx = CycleContext(cycle_id="cycle-p12", worker_id="worker-p12", task=task)

        # A single GivexDriver-like object that tracks every interaction.
        driver = MagicMock()
        driver.handle_vbv_challenge = GivexDriver.handle_vbv_challenge.__get__(
            driver, type(driver)
        )
        driver.detect_page_state.return_value = "vbv_cancelled"
        driver._get_rng.return_value = None

        # Make handle_something_wrong_popup see a popup and succeed.
        with patch("modules.cdp.driver.vbv_dynamic_wait"), \
             patch("modules.cdp.driver.cdp_click_iframe_element"), \
             patch.object(drv, "WebDriverWait") as mock_wait, \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.is_payment_page_reloaded", return_value=False):
            # Popup-handler presence checks: first returns truthy (popup
            # present), subsequent verify checks raise TimeoutException
            # (popup gone after click). Use a side-effect function so
            # additional WebDriverWait calls elsewhere (e.g. orchestrator
            # polling) also get a safe "not present" response instead of
            # exhausting a fixed list.
            _calls = {"n": 0}

            def _until_side_effect(*_a, **_kw):
                _calls["n"] += 1
                if _calls["n"] == 1:
                    return MagicMock()
                raise drv.TimeoutException()

            mock_wait.return_value.until.side_effect = _until_side_effect
            mock_cdp._get_driver.return_value = driver

            # Exercise the VBV handler directly; this triggers popup-close.
            outcome_ok = driver.handle_vbv_challenge()

            # Orchestrator consumes the post-popup state and picks next card.
            action = handle_outcome(State("vbv_3ds"), task.order_queue, ctx=ctx)

        # (1) VBV handler succeeded.
        self.assertEqual(outcome_ok, "cancelled")
        # (2) Popup close button was clicked.
        driver.bounding_box_click.assert_any_call(SEL_POPUP_CLOSE)
        # (3) clear_card_fields_cdp was invoked inside the popup handler
        #     (at least once — orchestrator may call it again during swap).
        self.assertGreaterEqual(driver.clear_card_fields_cdp.call_count, 1)
        # (4) Orchestrator returned a swap signal with the next card
        #     so the retry loop re-submits with a fresh card.
        self.assertEqual(action[0], "retry_new_card")
        self.assertEqual(action[1], task.order_queue[0])


if __name__ == "__main__":
    unittest.main()
