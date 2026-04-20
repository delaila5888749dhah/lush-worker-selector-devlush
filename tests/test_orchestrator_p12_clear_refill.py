"""P1-2 tests: clear/refill card fields after "Thank you" popup detection.

Verifies that after a successful payment where detect_popup_thank_you returns
True, the orchestrator:
  - Calls clear_card_fields_cdp on the driver.
  - Then calls fill_card_fields(new_card) with the next card from the queue.
  - Skips clear/refill when the order queue is empty.
  - Skips clear/refill when detect_popup_thank_you returns False.
  - Skips clear/refill when ENABLE_CLEAR_REFILL_AFTER_POPUP flag is disabled.
  - Handles exceptions in clear/refill gracefully without raising.

Also covers the module-level helpers:
  - clear_refill_after_thank_you_popup (orchestrator function)
  - detect_popup_thank_you (driver-level function)
  - detect_popup_thank_you (cdp.main wrapper)
"""
# pylint: disable=protected-access
import os
import unittest
from unittest.mock import MagicMock, call, patch

import integration.orchestrator as _orch
from integration.orchestrator import (
    clear_refill_after_thank_you_popup,
    run_cycle,
)
from modules.cdp.driver import (
    THANK_YOU_TEXT_PATTERNS_DEFAULT,
    THANK_YOU_TEXT_PATTERNS_EN,
    THANK_YOU_TEXT_PATTERNS_VN,
    URL_CONFIRM_FRAGMENTS,
    detect_popup_thank_you,
)
from modules.common.types import CardInfo, CycleContext, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_ID = "p12-clear-refill-test-worker"


def _make_card(suffix: str = "111111") -> CardInfo:
    return CardInfo(
        card_number=f"4111111111{suffix}",
        exp_month="12",
        exp_year="2030",
        cvv="123",
    )


def _make_task(order_queue: tuple = ()) -> WorkerTask:
    return WorkerTask(
        task_id="task-p12-001",
        recipient_email="test@example.com",
        amount=50,
        primary_card=_make_card(),
        order_queue=order_queue,
    )


def _make_billing_mock() -> MagicMock:
    billing = MagicMock()
    profile = MagicMock()
    profile.zip_code = "90210"
    profile.email = "billing@example.com"
    billing.select_profile.return_value = profile
    return billing


def _make_store_mock() -> MagicMock:
    store = MagicMock()
    store.is_duplicate.return_value = False
    return store


_STORE_PATCH = "integration.orchestrator._get_idempotency_store"


# ---------------------------------------------------------------------------
# Unit tests — detect_popup_thank_you (driver-level)
# ---------------------------------------------------------------------------

class TestDetectPopupThankYou(unittest.TestCase):
    """Tests for the standalone detect_popup_thank_you driver function."""

    def _make_driver(self, url: str = "", body_text: str = ""):
        base = MagicMock()
        base.current_url = url
        body_el = MagicMock()
        body_el.text = body_text
        base.find_element.return_value = body_el
        wrapper = MagicMock()
        wrapper._driver = base
        return wrapper, base

    # ── URL-based detection ──────────────────────────────────────────────────

    def test_url_confirmation_fragment_returns_true(self):
        for frag in URL_CONFIRM_FRAGMENTS:
            with self.subTest(frag=frag):
                wrapper, _ = self._make_driver(url=f"https://example.com{frag}")
                self.assertTrue(detect_popup_thank_you(wrapper))

    def test_url_no_match_falls_through_to_text(self):
        wrapper, _ = self._make_driver(
            url="https://example.com/payment.html",
            body_text="Thank you for your order, it has been placed.",
        )
        self.assertTrue(detect_popup_thank_you(wrapper))

    # ── Text-based detection ─────────────────────────────────────────────────

    def test_en_pattern_match_returns_true(self):
        for pat in THANK_YOU_TEXT_PATTERNS_EN:
            with self.subTest(pat=pat):
                wrapper, _ = self._make_driver(
                    url="https://example.com/payment.html",
                    body_text=f"Your transaction was complete. {pat.capitalize()}.",
                )
                self.assertTrue(detect_popup_thank_you(wrapper))

    def test_vn_pattern_match_returns_true(self):
        for pat in THANK_YOU_TEXT_PATTERNS_VN:
            with self.subTest(pat=pat):
                wrapper, _ = self._make_driver(
                    url="https://example.com/payment.html",
                    body_text=f"Giao dịch thành công. {pat}.",
                )
                self.assertTrue(detect_popup_thank_you(wrapper))

    def test_irrelevant_text_returns_false(self):
        wrapper, _ = self._make_driver(
            url="https://example.com/payment.html",
            body_text="Please enter your card number below.",
        )
        self.assertFalse(detect_popup_thank_you(wrapper))

    def test_empty_body_text_no_url_returns_false(self):
        wrapper, _ = self._make_driver(url="https://example.com/payment.html")
        self.assertFalse(detect_popup_thank_you(wrapper))

    def test_url_error_falls_back_gracefully(self):
        """current_url raising should not propagate — falls through to text check."""
        class _BrokenUrlDriver:
            """Simulates a driver where current_url raises, but body text matches."""
            @property
            def current_url(self):
                raise AttributeError("no url available")

            def find_element(self, *args, **kwargs):  # pylint: disable=unused-argument
                m = MagicMock()
                m.text = "thank you for your order"
                return m

        result = detect_popup_thank_you(_BrokenUrlDriver())
        self.assertTrue(result)

    def test_body_find_element_error_returns_false(self):
        base = MagicMock()
        base.current_url = "https://example.com/payment.html"
        base.find_element.side_effect = Exception("DOM not ready")
        self.assertFalse(detect_popup_thank_you(base))

    def test_custom_patterns_override(self):
        wrapper, _ = self._make_driver(
            url="https://example.com/payment.html",
            body_text="bestellung erfolgreich",
        )
        self.assertTrue(detect_popup_thank_you(wrapper, patterns=("bestellung erfolgreich",)))
        self.assertFalse(detect_popup_thank_you(wrapper))

    # ── Pattern constant sanity checks ───────────────────────────────────────

    def test_en_patterns_non_empty(self):
        self.assertGreater(len(THANK_YOU_TEXT_PATTERNS_EN), 0)

    def test_vn_patterns_non_empty(self):
        self.assertGreater(len(THANK_YOU_TEXT_PATTERNS_VN), 0)

    def test_default_includes_en_and_vn(self):
        for pat in THANK_YOU_TEXT_PATTERNS_EN:
            self.assertIn(pat, THANK_YOU_TEXT_PATTERNS_DEFAULT)
        for pat in THANK_YOU_TEXT_PATTERNS_VN:
            self.assertIn(pat, THANK_YOU_TEXT_PATTERNS_DEFAULT)

    def test_patterns_are_lowercase(self):
        for pat in THANK_YOU_TEXT_PATTERNS_DEFAULT:
            self.assertEqual(pat, pat.lower(), f"Pattern not lowercase: {pat!r}")


# ---------------------------------------------------------------------------
# Unit tests — clear_refill_after_thank_you_popup (orchestrator function)
# ---------------------------------------------------------------------------

class TestClearRefillAfterThankYouPopup(unittest.TestCase):
    """Tests for clear_refill_after_thank_you_popup orchestrator function."""

    def _make_driver_mock(self):
        driver = MagicMock()
        driver.clear_card_fields_cdp = MagicMock()
        driver.fill_card_fields = MagicMock()
        return driver

    def test_calls_clear_then_fill_in_order(self):
        driver = self._make_driver_mock()
        new_card = _make_card("222222")

        clear_refill_after_thank_you_popup(driver, new_card)

        call_order = [c[0] for c in driver.method_calls]
        self.assertEqual(call_order, ["clear_card_fields_cdp", "fill_card_fields"])

    def test_fill_receives_correct_new_card(self):
        driver = self._make_driver_mock()
        new_card = _make_card("333333")

        clear_refill_after_thank_you_popup(driver, new_card)

        driver.fill_card_fields.assert_called_once_with(new_card)

    def test_clear_cdp_called_once(self):
        driver = self._make_driver_mock()
        clear_refill_after_thank_you_popup(driver, _make_card("444444"))
        driver.clear_card_fields_cdp.assert_called_once()

    def test_exception_in_clear_does_not_raise(self):
        driver = self._make_driver_mock()
        driver.clear_card_fields_cdp.side_effect = RuntimeError("CDP error")
        # Should not raise
        try:
            clear_refill_after_thank_you_popup(driver, _make_card("555555"))
        except RuntimeError:
            self.fail("clear_refill_after_thank_you_popup should not propagate RuntimeError")

    def test_exception_in_fill_does_not_raise(self):
        driver = self._make_driver_mock()
        driver.fill_card_fields.side_effect = RuntimeError("fill error")
        try:
            clear_refill_after_thank_you_popup(driver, _make_card("666666"))
        except RuntimeError:
            self.fail("clear_refill_after_thank_you_popup should not propagate RuntimeError")


# ---------------------------------------------------------------------------
# Integration tests — run_cycle wiring (P1-2 clear/refill after thank you)
# ---------------------------------------------------------------------------

class TestRunCycleClearRefillWiring(unittest.TestCase):
    """Integration tests for P1-2 wiring inside run_cycle."""

    def setUp(self):
        reset_registry()
        cleanup_worker(_WORKER_ID)

    def tearDown(self):
        cleanup_worker(_WORKER_ID)
        reset_registry()

    def _run_cycle_with_mocks(
        self,
        *,
        order_queue=(),
        detect_thank_you_return=True,
        enable_flag=True,
        driver_clear_side_effect=None,
        driver_fill_side_effect=None,
    ):
        """Helper: run run_cycle with all external dependencies mocked."""
        next_card = _make_card("999999") if order_queue else None
        task = _make_task(order_queue=order_queue)

        mock_driver = MagicMock()
        mock_driver.clear_card_fields_cdp = MagicMock(side_effect=driver_clear_side_effect)
        mock_driver.fill_card_fields = MagicMock(side_effect=driver_fill_side_effect)

        success_state = State("success")
        billing_mock = _make_billing_mock()
        store_mock = _make_store_mock()

        with patch.object(_orch, "_ENABLE_RETRY_LOOP", True), \
             patch.object(_orch, "_ENABLE_CLEAR_REFILL_AFTER_POPUP", enable_flag), \
             patch("integration.orchestrator.billing", billing_mock), \
             patch(_STORE_PATCH, return_value=store_mock), \
             patch("integration.orchestrator.watchdog") as mock_wd, \
             patch("integration.orchestrator.fsm") as mock_fsm, \
             patch("integration.orchestrator.cdp") as mock_cdp, \
             patch("integration.orchestrator.monitor") as mock_mon, \
             patch("integration.orchestrator._notify_success"), \
             patch("integration.orchestrator._cdp_call_with_timeout",
                   side_effect=lambda fn, *a, **kw: fn(*a, **kw) if callable(fn) else None):
            # FSM
            mock_fsm.transition_for_worker.return_value = success_state
            mock_fsm.get_current_state_for_worker.return_value = success_state
            mock_fsm.ALLOWED_STATES = {"success", "declined", "vbv_3ds", "ui_lock", "vbv_cancelled"}

            # CDP
            mock_cdp._get_driver.return_value = mock_driver
            mock_cdp.detect_page_state.return_value = "success"
            mock_cdp.detect_popup_thank_you.return_value = detect_thank_you_return
            mock_cdp.run_preflight_and_fill.return_value = None
            mock_cdp.submit_purchase.return_value = None

            # Watchdog
            mock_wd.wait_for_total.return_value = 50.0
            mock_wd.enable_network_monitor.return_value = None
            mock_wd.reset_session.return_value = None
            mock_wd.create_session.return_value = None

            # Monitor
            mock_mon.start_cycle.return_value = None
            mock_mon.end_cycle.return_value = None

            action, state, total = run_cycle(
                task, zip_code="90210", worker_id=_WORKER_ID
            )

        return action, state, total, mock_driver, mock_cdp

    # ── Clear/refill triggered when queue has cards and popup detected ────────

    def test_clear_cdp_called_when_queue_non_empty_and_thank_you_detected(self):
        next_card = _make_card("999999")
        _, _, _, mock_driver, _ = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=True,
        )
        mock_driver.clear_card_fields_cdp.assert_called_once()

    def test_fill_card_fields_called_with_next_card_when_queue_non_empty(self):
        next_card = _make_card("888888")
        _, _, _, mock_driver, _ = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=True,
        )
        mock_driver.fill_card_fields.assert_called_once_with(next_card)

    def test_clear_called_before_fill(self):
        next_card = _make_card("777777")
        _, _, _, mock_driver, _ = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=True,
        )
        call_order = [c[0] for c in mock_driver.method_calls
                      if c[0] in ("clear_card_fields_cdp", "fill_card_fields")]
        self.assertEqual(call_order, ["clear_card_fields_cdp", "fill_card_fields"])

    # ── Clear/refill skipped when queue is empty ──────────────────────────────

    def test_no_clear_or_fill_when_queue_empty(self):
        _, _, _, mock_driver, _ = self._run_cycle_with_mocks(
            order_queue=(),
            detect_thank_you_return=True,
        )
        mock_driver.clear_card_fields_cdp.assert_not_called()
        mock_driver.fill_card_fields.assert_not_called()

    # ── Clear/refill skipped when thank-you not detected ─────────────────────

    def test_no_clear_or_fill_when_thank_you_not_detected(self):
        next_card = _make_card("666666")
        _, _, _, mock_driver, _ = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=False,
        )
        mock_driver.clear_card_fields_cdp.assert_not_called()
        mock_driver.fill_card_fields.assert_not_called()

    # ── Feature flag disabled ─────────────────────────────────────────────────

    def test_no_clear_or_fill_when_feature_flag_disabled(self):
        next_card = _make_card("555555")
        _, _, _, mock_driver, _ = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=True,
            enable_flag=False,
        )
        mock_driver.clear_card_fields_cdp.assert_not_called()
        mock_driver.fill_card_fields.assert_not_called()

    # ── Cycle still returns "complete" after clear/refill ─────────────────────

    def test_action_is_complete_even_after_clear_refill(self):
        next_card = _make_card("444444")
        action, _, _, _, _ = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=True,
        )
        self.assertEqual(action, "complete")

    # ── Exception in clear does not fail the cycle ────────────────────────────

    def test_clear_exception_does_not_fail_cycle(self):
        next_card = _make_card("333333")
        action, _, _, _, _ = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=True,
            driver_clear_side_effect=RuntimeError("CDP clear failed"),
        )
        self.assertEqual(action, "complete")

    # ── detect_popup_thank_you called for confirmation ────────────────────────

    def test_detect_popup_thank_you_called_when_queue_non_empty(self):
        next_card = _make_card("222222")
        _, _, _, _, mock_cdp = self._run_cycle_with_mocks(
            order_queue=(next_card,),
            detect_thank_you_return=True,
        )
        mock_cdp.detect_popup_thank_you.assert_called_once_with(_WORKER_ID)

    def test_detect_popup_thank_you_not_called_when_queue_empty(self):
        _, _, _, _, mock_cdp = self._run_cycle_with_mocks(
            order_queue=(),
            detect_thank_you_return=True,
        )
        mock_cdp.detect_popup_thank_you.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests — cdp.main detect_popup_thank_you wrapper
# ---------------------------------------------------------------------------

class TestCdpMainDetectPopupThankYou(unittest.TestCase):
    """Tests for the cdp.main.detect_popup_thank_you public wrapper."""

    def test_wrapper_delegates_to_driver(self):
        import modules.cdp.main as cdp_main
        mock_driver = MagicMock()
        # Ensure _driver is not auto-created as a sub-mock; use the driver itself as base.
        mock_driver._driver = mock_driver
        mock_driver.current_url = "https://example.com/confirmation"
        body_el = MagicMock()
        body_el.text = ""
        mock_driver.find_element.return_value = body_el

        with patch.object(cdp_main, "_get_driver", return_value=mock_driver):
            result = cdp_main.detect_popup_thank_you("worker-test")

        self.assertTrue(result)

    def test_wrapper_returns_false_when_no_signal(self):
        import modules.cdp.main as cdp_main
        mock_driver = MagicMock()
        mock_driver.current_url = "https://example.com/payment.html"
        body_el = MagicMock()
        body_el.text = "Enter your card details"
        mock_driver.find_element.return_value = body_el

        with patch.object(cdp_main, "_get_driver", return_value=mock_driver):
            result = cdp_main.detect_popup_thank_you("worker-test")

        self.assertFalse(result)

    def test_wrapper_raises_when_no_driver(self):
        import modules.cdp.main as cdp_main
        with patch.object(cdp_main, "_get_driver", side_effect=RuntimeError("No driver")):
            with self.assertRaises(RuntimeError):
                cdp_main.detect_popup_thank_you("missing-worker")


if __name__ == "__main__":
    unittest.main()
