"""Tests for modules/cdp/driver.py — GivexDriver happy-path implementation.

Covers:
- fill_egift_form  — all fields are typed with correct values
- add_to_cart_and_checkout — waits for review button then clicks it
- select_guest_checkout — full sequence: begin → guest heading → email → continue
- fill_billing_form — all billing fields filled, CONTINUE clicked
- run_full_cycle — steps called in the correct order
- _wait_for_element — returns True when element found, False on timeout
- detect_page_state — checks all URL_CONFIRM_FRAGMENTS, element presence,
  VBV iframe, declined text, ui_lock spinner
"""

import time
import unittest
from unittest.mock import MagicMock, call, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    GivexDriver,
    SEL_ADD_TO_CART,
    SEL_AMOUNT_INPUT,
    SEL_BEGIN_CHECKOUT,
    SEL_BILLING_CITY,
    SEL_BILLING_CONTINUE,
    SEL_BILLING_EMAIL,
    SEL_BILLING_FIRST_NAME,
    SEL_BILLING_LAST_NAME,
    SEL_BILLING_ADDRESS,
    SEL_BILLING_PHONE,
    SEL_BILLING_STATE,
    SEL_BILLING_ZIP,
    SEL_CARD_CVV,
    SEL_CARD_EXPIRY_MONTH,
    SEL_CARD_EXPIRY_YEAR,
    SEL_CARD_NUMBER,
    SEL_COMPLETE_PURCHASE,
    SEL_CONFIRMATION_EL,
    SEL_DECLINED_MSG,
    SEL_GREETING_MSG,
    SEL_GUEST_CONTINUE,
    SEL_GUEST_EMAIL,
    SEL_GUEST_HEADING,
    SEL_RECIPIENT_EMAIL,
    SEL_RECIPIENT_NAME,
    SEL_REVIEW_CHECKOUT,
    SEL_SENDER_NAME,
    SEL_UI_LOCK_SPINNER,
    SEL_VBV_IFRAME,
    URL_CONFIRM_FRAGMENTS,
)
from modules.common.exceptions import SelectorTimeoutError
from modules.common.types import BillingProfile, CardInfo, WorkerTask


def _make_driver(current_url: str = "https://example.com/page") -> MagicMock:
    """Build a minimal Selenium-like mock that returns no elements by default."""
    d = MagicMock()
    d.current_url = current_url
    # Default: find_elements returns empty list (no element present)
    d.find_elements.return_value = []
    # body element for text-based declined detection
    body_el = MagicMock()
    body_el.text = ""
    d.find_element.return_value = body_el
    return d


def _make_task() -> WorkerTask:
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="12",
        exp_year="2027",
        cvv="123",
    )
    return WorkerTask(
        recipient_email="recipient@example.com",
        amount=50,
        primary_card=card,
        order_queue=(),
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
    )


class TestWaitForElement(unittest.TestCase):
    """_wait_for_element returns True when found, False on timeout."""

    def test_wait_for_element_returns_true_when_found(self):
        selenium = _make_driver()
        # Simulate element appearing on the second poll
        element = MagicMock()
        selenium.find_elements.side_effect = [[], [element]]
        gd = GivexDriver(selenium)
        with patch("time.sleep"):
            result = gd._wait_for_element("#some-selector", timeout=5)
        self.assertTrue(result)

    def test_wait_for_element_returns_false_on_timeout(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        # Use a very short timeout so the test runs fast
        start = time.monotonic()
        result = gd._wait_for_element("#missing", timeout=1)
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        # Sanity check: method did actually wait
        self.assertGreaterEqual(elapsed, 0.5)


class TestFillEgiftForm(unittest.TestCase):
    """fill_egift_form types the correct value into each eGift field."""

    def _make_field_el(self):
        el = MagicMock()
        return el

    def test_fill_egift_form_types_all_fields(self):
        selenium = _make_driver()
        task = _make_task()
        billing = _make_billing()
        full_name = f"{billing.first_name} {billing.last_name}"

        # Each find_elements call returns one element
        element = MagicMock()
        selenium.find_elements.return_value = [element]

        gd = GivexDriver(selenium)

        with patch.object(drv, "_random_greeting", return_value="Test greeting"):
            gd.fill_egift_form(task, billing)

        # Collect all send_keys calls
        send_keys_calls = element.send_keys.call_args_list

        # Verify each expected value appears somewhere in send_keys calls
        sent_values = [c.args[0] for c in send_keys_calls]
        self.assertIn(str(task.amount), sent_values)
        self.assertIn(task.recipient_email, sent_values)
        self.assertIn(full_name, sent_values)
        self.assertIn("Test greeting", sent_values)
        # full_name appears twice: recipient name + sender name
        self.assertEqual(sent_values.count(full_name), 2)

    def test_fill_egift_form_uses_billing_profile_name_as_sender(self):
        selenium = _make_driver()
        task = _make_task()
        billing = _make_billing()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        with patch.object(drv, "_random_greeting", return_value="Hi"):
            gd.fill_egift_form(task, billing)
        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
        self.assertIn("Jane Doe", sent_values)


class TestAddToCartAndCheckout(unittest.TestCase):
    """add_to_cart_and_checkout waits for the review button then clicks it."""

    def test_add_to_cart_waits_for_review_button(self):
        selenium = _make_driver()
        cart_el = MagicMock()
        review_el = MagicMock()

        # find_elements returns: [cart_el] for ADD_TO_CART, then [] for
        # REVIEW_CHECKOUT (poll 1), then [review_el] (poll 2), then
        # [review_el] again (the click call).
        call_count = [0]

        def side_effect(method, selector):
            call_count[0] += 1
            clean = selector.strip()
            first_part_add = SEL_ADD_TO_CART.split(",")[0].strip()
            first_part_review = SEL_REVIEW_CHECKOUT.split(",")[0].strip()
            if clean == first_part_add:
                return [cart_el]
            if clean == first_part_review:
                # First two calls return empty (simulating wait), then return element
                if call_count[0] <= 3:
                    return []
                return [review_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)

        with patch("time.sleep"):
            gd.add_to_cart_and_checkout()

        # Cart button was clicked
        cart_el.click.assert_called_once()
        # Review button was clicked after wait
        review_el.click.assert_called_once()

    def test_add_to_cart_raises_if_review_button_never_appears(self):
        selenium = _make_driver()
        cart_el = MagicMock()

        def side_effect(method, selector):
            clean = selector.strip()
            first_part_add = SEL_ADD_TO_CART.split(",")[0].strip()
            if clean == first_part_add:
                return [cart_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)

        with patch("time.sleep"):
            with self.assertRaises(SelectorTimeoutError):
                gd.add_to_cart_and_checkout()


class TestSelectGuestCheckout(unittest.TestCase):
    """select_guest_checkout executes the full cart → guest email sequence."""

    def test_select_guest_checkout_sequence(self):
        selenium = _make_driver()
        begin_el = MagicMock()
        guest_el = MagicMock()
        email_el = MagicMock()
        continue_el = MagicMock()

        def side_effect(method, selector):
            clean = selector.strip()
            first_begin = SEL_BEGIN_CHECKOUT.split(",")[0].strip()
            first_guest = SEL_GUEST_HEADING.strip()
            first_email = SEL_GUEST_EMAIL.split(",")[0].strip()
            first_continue = SEL_GUEST_CONTINUE.split(",")[0].strip()
            if clean == first_begin:
                return [begin_el]
            if clean == first_guest:
                return [guest_el]
            if clean == first_email:
                return [email_el]
            if clean == first_continue:
                return [continue_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)

        with patch("time.sleep"):
            gd.select_guest_checkout("guest@example.com")

        begin_el.click.assert_called_once()
        guest_el.click.assert_called_once()
        email_el.send_keys.assert_called_with("guest@example.com")
        continue_el.click.assert_called_once()

    def test_select_guest_checkout_raises_if_begin_checkout_missing(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        with patch("time.sleep"):
            with self.assertRaises(SelectorTimeoutError):
                gd.select_guest_checkout("guest@example.com")


class TestFillBillingForm(unittest.TestCase):
    """fill_billing_form fills all fields and clicks CONTINUE."""

    def test_fill_billing_form_clicks_continue(self):
        selenium = _make_driver()
        element = MagicMock()
        continue_el = MagicMock()

        first_continue = SEL_BILLING_CONTINUE.split(",")[0].strip()

        def side_effect(method, selector):
            clean = selector.strip()
            if clean == first_continue:
                return [continue_el]
            return [element]

        selenium.find_elements.side_effect = side_effect

        billing = _make_billing()
        gd = GivexDriver(selenium)

        # Patch _cdp_select_option to avoid Selenium Select dependency
        with patch.object(gd, "_cdp_select_option") as mock_select:
            gd.fill_billing_form(billing)

        # CONTINUE was clicked
        continue_el.click.assert_called_once()

        # State was selected by value
        mock_select.assert_called_with(SEL_BILLING_STATE, billing.state)

        # All text fields received their values
        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
        self.assertIn(billing.first_name, sent_values)
        self.assertIn(billing.last_name, sent_values)
        self.assertIn(billing.address, sent_values)
        self.assertIn(billing.city, sent_values)
        self.assertIn(billing.zip_code, sent_values)
        self.assertIn(billing.phone, sent_values)
        self.assertIn(billing.email, sent_values)

    def test_fill_billing_form_skips_optional_phone_when_none(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        billing = BillingProfile(
            first_name="A",
            last_name="B",
            address="1 St",
            city="City",
            state="CA",
            zip_code="90001",
            phone=None,
            email=None,
        )
        gd = GivexDriver(selenium)
        with patch.object(gd, "_cdp_select_option"):
            # Should not raise even with None phone/email
            gd.fill_billing_form(billing)


class TestRunFullCycle(unittest.TestCase):
    """run_full_cycle calls each step in order and returns detect_page_state result."""

    def test_run_full_cycle_calls_steps_in_order(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        call_log = []

        def _step(name):
            def fn(*args, **kwargs):
                call_log.append(name)
            return fn

        task = _make_task()
        billing = _make_billing()

        with (
            patch.object(gd, "preflight_geo_check", side_effect=_step("geo")),
            patch.object(gd, "navigate_to_egift", side_effect=_step("nav")),
            patch.object(gd, "fill_egift_form", side_effect=_step("egift")),
            patch.object(gd, "add_to_cart_and_checkout", side_effect=_step("cart")),
            patch.object(gd, "select_guest_checkout", side_effect=_step("guest")),
            patch.object(gd, "fill_billing_form", side_effect=_step("billing")),
            patch.object(gd, "fill_card", side_effect=_step("card")),
            patch.object(gd, "submit_purchase", side_effect=_step("submit")),
            patch.object(gd, "detect_page_state", return_value="success"),
        ):
            result = gd.run_full_cycle(task, billing)

        self.assertEqual(
            call_log,
            ["geo", "nav", "egift", "cart", "guest", "billing", "card", "submit"],
        )
        self.assertEqual(result, "success")

    def test_run_full_cycle_passes_billing_email_to_guest_checkout(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        task = _make_task()
        billing = _make_billing()
        captured = {}

        def capture_guest(email):
            captured["email"] = email

        with (
            patch.object(gd, "preflight_geo_check"),
            patch.object(gd, "navigate_to_egift"),
            patch.object(gd, "fill_egift_form"),
            patch.object(gd, "add_to_cart_and_checkout"),
            patch.object(gd, "select_guest_checkout", side_effect=capture_guest),
            patch.object(gd, "fill_billing_form"),
            patch.object(gd, "fill_card"),
            patch.object(gd, "submit_purchase"),
            patch.object(gd, "detect_page_state", return_value="success"),
        ):
            gd.run_full_cycle(task, billing)

        self.assertEqual(captured["email"], billing.email)


class TestDetectPageState(unittest.TestCase):
    """detect_page_state checks URL fragments, elements, and page text."""

    def test_detect_page_state_uses_all_confirm_fragments(self):
        for fragment in URL_CONFIRM_FRAGMENTS:
            with self.subTest(fragment=fragment):
                selenium = _make_driver(
                    current_url=f"https://wwws-usa2.givex.com{fragment}/12345"
                )
                selenium.find_elements.return_value = []
                gd = GivexDriver(selenium)
                self.assertEqual(gd.detect_page_state(), "success")

    def test_detect_page_state_success_via_element(self):
        selenium = _make_driver(current_url="https://example.com/unknown")
        confirm_el = MagicMock()
        first_confirm = SEL_CONFIRMATION_EL.split(",")[0].strip()

        def side_effect(method, selector):
            if selector.strip() == first_confirm:
                return [confirm_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)
        self.assertEqual(gd.detect_page_state(), "success")

    def test_detect_page_state_vbv_3ds(self):
        selenium = _make_driver()
        iframe_el = MagicMock()
        first_vbv = SEL_VBV_IFRAME.split(",")[0].strip()

        def side_effect(method, selector):
            if selector.strip() == first_vbv:
                return [iframe_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)
        self.assertEqual(gd.detect_page_state(), "vbv_3ds")

    def test_detect_page_state_declined_via_element(self):
        selenium = _make_driver()
        err_el = MagicMock()
        first_declined = SEL_DECLINED_MSG.split(",")[0].strip()

        def side_effect(method, selector):
            if selector.strip() == first_declined:
                return [err_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)
        self.assertEqual(gd.detect_page_state(), "declined")

    def test_detect_page_state_declined_via_page_text(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        body_el = MagicMock()
        body_el.text = "Your transaction failed. Please try again."
        selenium.find_element.return_value = body_el
        gd = GivexDriver(selenium)
        self.assertEqual(gd.detect_page_state(), "declined")

    def test_detect_page_state_ui_lock(self):
        selenium = _make_driver()
        spinner_el = MagicMock()
        first_spinner = SEL_UI_LOCK_SPINNER.split(",")[0].strip()

        def side_effect(method, selector):
            if selector.strip() == first_spinner:
                return [spinner_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)
        self.assertEqual(gd.detect_page_state(), "ui_lock")

    def test_detect_page_state_unknown(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        body_el = MagicMock()
        body_el.text = "Some normal page content"
        selenium.find_element.return_value = body_el
        gd = GivexDriver(selenium)
        self.assertEqual(gd.detect_page_state(), "unknown")

    def test_detect_page_state_success_takes_priority_over_vbv(self):
        """success should be returned even if a VBV iframe is also present."""
        for fragment in URL_CONFIRM_FRAGMENTS:
            with self.subTest(fragment=fragment):
                selenium = _make_driver(
                    current_url=f"https://example.com{fragment}"
                )
                iframe_el = MagicMock()
                selenium.find_elements.return_value = [iframe_el]
                gd = GivexDriver(selenium)
                self.assertEqual(gd.detect_page_state(), "success")


if __name__ == "__main__":
    unittest.main()
