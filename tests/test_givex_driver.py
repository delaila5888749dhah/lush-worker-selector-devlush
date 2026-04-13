"""Tests for modules/cdp/driver.py — GivexDriver happy-path implementation.

Covers:
- fill_egift_form  — all fields are typed with correct values, including
  confirm recipient email
- add_to_cart_and_checkout — waits for review button then clicks it
- select_guest_checkout — begin checkout → email + continue sequence
- fill_payment_and_billing — all card and billing fields filled
- fill_billing — backward-compat alias fills billing fields
- fill_billing_form — alias for fill_billing
- fill_card — raises NotImplementedError
- run_full_cycle — steps called in the correct order
- _wait_for_element — returns True when found, False on timeout
- detect_page_state — checks all URL_CONFIRM_FRAGMENTS, element presence,
  VBV iframe, declined text, ui_lock spinner, raises PageStateError on unknown
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    GivexDriver,
    SEL_ADD_TO_CART,
    SEL_AMOUNT_INPUT,
    SEL_BEGIN_CHECKOUT,
    SEL_BILLING_ADDRESS,
    SEL_BILLING_CITY,
    SEL_BILLING_COUNTRY,
    SEL_BILLING_PHONE,
    SEL_BILLING_STATE,
    SEL_BILLING_ZIP,
    SEL_CARD_CVV,
    SEL_CARD_EXPIRY_MONTH,
    SEL_CARD_EXPIRY_YEAR,
    SEL_CARD_NAME,
    SEL_CARD_NUMBER,
    SEL_COMPLETE_PURCHASE,
    SEL_CONFIRM_RECIPIENT_EMAIL,
    SEL_CONFIRMATION_EL,
    SEL_DECLINED_MSG,
    SEL_GREETING_MSG,
    SEL_GUEST_CONTINUE,
    SEL_GUEST_EMAIL,
    SEL_RECIPIENT_EMAIL,
    SEL_RECIPIENT_NAME,
    SEL_REVIEW_CHECKOUT,
    SEL_SENDER_NAME,
    SEL_UI_LOCK_SPINNER,
    SEL_VBV_IFRAME,
    URL_CONFIRM_FRAGMENTS,
    URL_EGIFT,
    URL_GEO_CHECK,
)
from modules.common.exceptions import PageStateError, SelectorTimeoutError
from modules.common.types import BillingProfile, CardInfo, WorkerTask


def _make_driver(current_url: str = "https://example.com/page") -> MagicMock:
    """Build a minimal Selenium-like mock that returns no elements by default."""
    d = MagicMock()
    d.current_url = current_url
    d.find_elements.return_value = []
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
        card_name="Jane Doe",
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
        country="US",
    )


class TestWaitForElement(unittest.TestCase):
    """_wait_for_element returns True when found, False on timeout."""

    def test_wait_for_element_returns_true_when_found(self):
        selenium = _make_driver()
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
        start = time.monotonic()
        result = gd._wait_for_element("#missing", timeout=1)
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.5)


class TestFillEgiftForm(unittest.TestCase):
    """fill_egift_form types the correct value into each eGift field."""

    def test_fill_egift_form_types_all_fields(self):
        selenium = _make_driver()
        task = _make_task()
        billing = _make_billing()
        full_name = f"{billing.first_name} {billing.last_name}"

        element = MagicMock()
        selenium.find_elements.return_value = [element]

        gd = GivexDriver(selenium)

        with patch.object(drv, "_random_greeting", return_value="Test greeting"):
            gd.fill_egift_form(task, billing)

        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
        self.assertIn(str(task.amount), sent_values)
        self.assertIn(task.recipient_email, sent_values)
        self.assertIn(full_name, sent_values)
        self.assertIn("Test greeting", sent_values)
        # full_name appears twice: recipient name + sender name
        self.assertEqual(sent_values.count(full_name), 2)
        # recipient_email appears twice: email + confirm email
        self.assertEqual(sent_values.count(task.recipient_email), 2)

    def test_fill_egift_form_sends_confirm_email(self):
        """Verify recipient_email is sent to both SEL_RECIPIENT_EMAIL and SEL_CONFIRM_RECIPIENT_EMAIL."""
        selenium = _make_driver()
        task = _make_task()
        billing = _make_billing()

        element = MagicMock()
        selenium.find_elements.return_value = [element]

        gd = GivexDriver(selenium)

        with patch.object(drv, "_random_greeting", return_value="Hi"):
            gd.fill_egift_form(task, billing)

        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
        # Both email and confirm email should receive recipient_email
        self.assertEqual(sent_values.count(task.recipient_email), 2)

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

        call_count = [0]

        def side_effect(method, selector):
            call_count[0] += 1
            clean = selector.strip()
            if clean == SEL_ADD_TO_CART:
                return [cart_el]
            if clean == SEL_REVIEW_CHECKOUT:
                if call_count[0] <= 3:
                    return []
                return [review_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)

        with patch("time.sleep"):
            gd.add_to_cart_and_checkout()

        cart_el.click.assert_called_once()
        review_el.click.assert_called_once()

    def test_add_to_cart_raises_if_review_button_never_appears(self):
        selenium = _make_driver()
        cart_el = MagicMock()

        def side_effect(method, selector):
            clean = selector.strip()
            if clean == SEL_ADD_TO_CART:
                return [cart_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)

        with patch("time.sleep"):
            with self.assertRaises(SelectorTimeoutError):
                gd.add_to_cart_and_checkout()


class TestSelectGuestCheckout(unittest.TestCase):
    """select_guest_checkout: begin checkout → email → continue."""

    def test_select_guest_checkout_sequence(self):
        selenium = _make_driver()
        begin_el = MagicMock()
        email_el = MagicMock()
        continue_el = MagicMock()

        def side_effect(method, selector):
            clean = selector.strip()
            if clean == SEL_BEGIN_CHECKOUT:
                return [begin_el]
            if clean == SEL_GUEST_EMAIL:
                return [email_el]
            if clean == SEL_GUEST_CONTINUE:
                return [continue_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)

        with patch("time.sleep"):
            gd.select_guest_checkout("guest@example.com")

        begin_el.click.assert_called_once()
        email_el.send_keys.assert_called_with("guest@example.com")
        continue_el.click.assert_called_once()

    def test_select_guest_checkout_raises_if_begin_checkout_missing(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        with patch("time.sleep"):
            with self.assertRaises(SelectorTimeoutError):
                gd.select_guest_checkout("guest@example.com")

    def test_select_guest_checkout_raises_if_email_field_missing(self):
        selenium = _make_driver()
        begin_el = MagicMock()

        def side_effect(method, selector):
            clean = selector.strip()
            if clean == SEL_BEGIN_CHECKOUT:
                return [begin_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)
        with patch("time.sleep"):
            with self.assertRaises(SelectorTimeoutError):
                gd.select_guest_checkout("guest@example.com")


class TestFillPaymentAndBilling(unittest.TestCase):
    """fill_payment_and_billing fills all card and billing fields."""

    def test_fill_payment_and_billing_fills_all_fields(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]

        task = _make_task()
        billing = _make_billing()
        gd = GivexDriver(selenium)

        with patch.object(gd, "_cdp_select_option") as mock_select:
            gd.fill_payment_and_billing(task.primary_card, billing)

        # Card name, number, and CVV are typed
        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
        self.assertIn(task.primary_card.card_name, sent_values)
        self.assertIn(task.primary_card.card_number, sent_values)
        self.assertIn(task.primary_card.cvv, sent_values)

        # Billing text fields
        self.assertIn(billing.address, sent_values)
        self.assertIn(billing.city, sent_values)
        self.assertIn(billing.zip_code, sent_values)
        self.assertIn(billing.phone, sent_values)

        # Select options for expiry, country, state
        select_calls = {c.args[0]: c.args[1] for c in mock_select.call_args_list}
        self.assertEqual(select_calls[SEL_CARD_EXPIRY_MONTH], task.primary_card.exp_month)
        self.assertEqual(select_calls[SEL_CARD_EXPIRY_YEAR], task.primary_card.exp_year)
        self.assertEqual(select_calls[SEL_BILLING_COUNTRY], billing.country)
        self.assertEqual(select_calls[SEL_BILLING_STATE], billing.state)

    def test_fill_payment_and_billing_sends_card_name(self):
        """Verify card_name is sent to SEL_CARD_NAME."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]

        task = _make_task()
        billing = _make_billing()
        gd = GivexDriver(selenium)

        with patch.object(gd, "_cdp_select_option"):
            gd.fill_payment_and_billing(task.primary_card, billing)

        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
        self.assertIn("Jane Doe", sent_values)  # card_name

    def test_fill_payment_and_billing_selects_country(self):
        """Verify _cdp_select_option is called with SEL_BILLING_COUNTRY and billing_profile.country."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]

        task = _make_task()
        billing = _make_billing()
        gd = GivexDriver(selenium)

        with patch.object(gd, "_cdp_select_option") as mock_select:
            gd.fill_payment_and_billing(task.primary_card, billing)

        country_calls = [c for c in mock_select.call_args_list if c.args[0] == SEL_BILLING_COUNTRY]
        self.assertEqual(len(country_calls), 1)
        self.assertEqual(country_calls[0].args[1], "US")

    def test_fill_payment_and_billing_skips_phone_when_none(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        card = CardInfo(
            card_number="4111111111111111",
            exp_month="12",
            exp_year="2027",
            cvv="123",
            card_name="A B",
        )
        billing = BillingProfile(
            first_name="A",
            last_name="B",
            address="1 St",
            city="City",
            state="CA",
            zip_code="90001",
            phone=None,
            email=None,
            country="US",
        )
        gd = GivexDriver(selenium)
        with patch.object(gd, "_cdp_select_option"):
            # Should not raise even with None phone
            gd.fill_payment_and_billing(card, billing)


class TestFillBilling(unittest.TestCase):
    """fill_billing is backward-compat: fills billing fields only."""

    def test_fill_billing_fills_billing_fields(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]

        billing = _make_billing()
        gd = GivexDriver(selenium)

        with patch.object(gd, "_cdp_select_option") as mock_select:
            gd.fill_billing(billing)

        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
        self.assertIn(billing.address, sent_values)
        self.assertIn(billing.city, sent_values)
        self.assertIn(billing.zip_code, sent_values)
        self.assertIn(billing.phone, sent_values)

        # State and country selected
        state_calls = [c for c in mock_select.call_args_list if c.args[0] == SEL_BILLING_STATE]
        self.assertTrue(len(state_calls) >= 1)
        self.assertEqual(state_calls[0].args[1], billing.state)

    def test_fill_billing_form_is_alias_for_fill_billing(self):
        """fill_billing_form must delegate to fill_billing."""
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        billing = _make_billing()
        with patch.object(gd, "fill_billing") as mock_fill:
            gd.fill_billing_form(billing)
        mock_fill.assert_called_once_with(billing)


class TestFillCard(unittest.TestCase):
    """fill_card raises NotImplementedError."""

    def test_fill_card_raises_not_implemented(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        task = _make_task()
        with self.assertRaises(NotImplementedError):
            gd.fill_card(task.primary_card)


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
            patch.object(gd, "fill_payment_and_billing", side_effect=_step("payment_billing")),
            patch.object(gd, "submit_purchase", side_effect=_step("submit")),
            patch.object(gd, "detect_page_state", return_value="success"),
        ):
            result = gd.run_full_cycle(task, billing)

        self.assertEqual(
            call_log,
            ["geo", "nav", "egift", "cart", "guest", "payment_billing", "submit"],
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
            patch.object(gd, "fill_payment_and_billing"),
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

    def test_detect_page_state_raises_page_state_error_on_unknown(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        body_el = MagicMock()
        body_el.text = "Some normal page content"
        selenium.find_element.return_value = body_el
        gd = GivexDriver(selenium)
        with self.assertRaises(PageStateError):
            gd.detect_page_state()

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


class TestNavigateToEgift(unittest.TestCase):
    """navigate_to_egift navigates to URL_EGIFT after clicking buy button."""

    def test_navigate_to_egift_calls_url_egift(self):
        selenium = _make_driver()
        btn_el = MagicMock()

        def side_effect(method, selector):
            clean = selector.strip()
            if clean == "#button--accept-cookies":
                return []
            return [btn_el]

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)

        with patch("time.sleep"):
            gd.navigate_to_egift()

        # Verify URL_EGIFT was navigated to
        get_calls = [str(c) for c in selenium.get.call_args_list]
        self.assertTrue(any(URL_EGIFT in c for c in get_calls))


class TestPreflightGeoCheck(unittest.TestCase):
    """preflight_geo_check uses URL_GEO_CHECK constant."""

    def test_preflight_uses_url_constant(self):
        selenium = _make_driver()
        body_el = MagicMock()
        body_el.text = '{"country": "US"}'
        selenium.find_element.return_value = body_el
        gd = GivexDriver(selenium)

        gd.preflight_geo_check()

        selenium.get.assert_called_once_with(URL_GEO_CHECK)


if __name__ == "__main__":
    unittest.main()
