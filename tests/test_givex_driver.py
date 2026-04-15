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
from unittest.mock import MagicMock, call, patch

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
    SEL_GUEST_HEADING,
    SEL_RECIPIENT_EMAIL,
    SEL_RECIPIENT_NAME,
    SEL_REVIEW_CHECKOUT,
    SEL_SENDER_NAME,
    SEL_UI_LOCK_SPINNER,
    SEL_VBV_IFRAME,
    URL_BASE,
    URL_CART,
    URL_CHECKOUT,
    URL_CONFIRM_FRAGMENTS,
    URL_EGIFT,
    URL_GEO_CHECK,
    URL_PAYMENT,
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


class TestWaitForUrl(unittest.TestCase):
    """_wait_for_url returns when URL matches, raises PageStateError on timeout."""

    def test_wait_for_url_returns_when_url_matches(self):
        selenium = _make_driver(current_url=URL_EGIFT)
        gd = GivexDriver(selenium)
        # Should not raise
        gd._wait_for_url("/e-gifts/", timeout=5)

    def test_wait_for_url_raises_on_timeout(self):
        selenium = _make_driver(current_url="https://example.com/other")
        gd = GivexDriver(selenium)
        with self.assertRaises(PageStateError):
            gd._wait_for_url("/e-gifts/", timeout=1)


class TestFindElements(unittest.TestCase):
    """find_elements propagates driver exceptions instead of swallowing them."""

    def test_find_elements_returns_matching_elements(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        result = gd.find_elements("#some-selector")
        self.assertEqual(result, [element])

    def test_find_elements_returns_empty_for_no_match(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        result = gd.find_elements("#missing")
        self.assertEqual(result, [])

    def test_find_elements_propagates_driver_exceptions(self):
        selenium = _make_driver()
        selenium.find_elements.side_effect = RuntimeError("session expired")
        gd = GivexDriver(selenium)
        with self.assertRaises(RuntimeError):
            gd.find_elements("#any-selector")


class TestFillEgiftForm(unittest.TestCase):
    """fill_egift_form types the correct value into each eGift field."""

    def test_fill_egift_form_types_all_fields(self):
        selenium = _make_driver()
        task = _make_task()
        billing = _make_billing()
        full_name = f"{billing.first_name} {billing.last_name}"

        gd = GivexDriver(selenium)

        with patch.object(drv, "_random_greeting", return_value="Test greeting"), \
             patch.object(gd, "_realistic_type_field") as mock_type:
            gd.fill_egift_form(task, billing)

        expected_calls = [
            call(SEL_GREETING_MSG, "Test greeting", field_kind="text"),
            call(SEL_AMOUNT_INPUT, str(task.amount), field_kind="amount", typo_rate=0.0),
            call(SEL_RECIPIENT_NAME, full_name, field_kind="name"),
            call(SEL_RECIPIENT_EMAIL, task.recipient_email, field_kind="text"),
            call(SEL_CONFIRM_RECIPIENT_EMAIL, task.recipient_email, field_kind="text"),
            call(SEL_SENDER_NAME, full_name, field_kind="name"),
        ]
        self.assertEqual(mock_type.call_count, 6)
        mock_type.assert_has_calls(expected_calls)

    def test_fill_egift_form_sends_confirm_email(self):
        """Verify recipient_email is sent to both SEL_RECIPIENT_EMAIL and SEL_CONFIRM_RECIPIENT_EMAIL."""
        selenium = _make_driver()
        task = _make_task()
        billing = _make_billing()

        gd = GivexDriver(selenium)

        with patch.object(drv, "_random_greeting", return_value="Hi"), \
             patch.object(gd, "_realistic_type_field") as mock_type:
            gd.fill_egift_form(task, billing)

        email_calls = [
            c for c in mock_type.call_args_list
            if c.args[0] in (SEL_RECIPIENT_EMAIL, SEL_CONFIRM_RECIPIENT_EMAIL)
        ]
        self.assertEqual(len(email_calls), 2)
        self.assertTrue(all(c.args[1] == task.recipient_email for c in email_calls))

    def test_fill_egift_form_uses_billing_profile_name_as_sender(self):
        selenium = _make_driver()
        task = _make_task()
        billing = _make_billing()
        gd = GivexDriver(selenium)
        with patch.object(drv, "_random_greeting", return_value="Hi"), \
             patch.object(gd, "_realistic_type_field") as mock_type:
            gd.fill_egift_form(task, billing)
        sender_calls = [c for c in mock_type.call_args_list if c.args[0] == SEL_SENDER_NAME]
        self.assertEqual(len(sender_calls), 1)
        self.assertEqual(sender_calls[0].args[1], "Jane Doe")

    def test_fill_egift_form_uses_type_value_not_send_keys(self):
        """fill_egift_form dispatches via _type_value, never via send_keys."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)

        with patch.object(drv, "_random_greeting", return_value="Hi"), \
             patch("modules.cdp.driver._type_value") as mock_tv, \
             patch("time.sleep"):
            mock_tv.return_value = {"typed_chars": 1, "typos_injected": 0,
                                    "corrections_made": 0, "mode": "cdp_key"}
            gd.fill_egift_form(_make_task(), _make_billing())

        self.assertEqual(mock_tv.call_count, 6)
        element.send_keys.assert_not_called()


class TestAddToCartAndCheckout(unittest.TestCase):
    """add_to_cart_and_checkout waits for the review button then clicks it."""

    def test_add_to_cart_waits_for_review_button(self):
        selenium = _make_driver(current_url=URL_CART)
        cart_el = MagicMock()
        review_el = MagicMock()

        call_count = [0]

        def side_effect(_method, selector):
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
        gd = GivexDriver(selenium, strict=False)

        with patch("time.sleep"):
            gd.add_to_cart_and_checkout()

        cart_el.click.assert_called_once()
        review_el.click.assert_called_once()

    def test_add_to_cart_raises_if_review_button_never_appears(self):
        selenium = _make_driver()
        cart_el = MagicMock()

        def side_effect(_method, selector):
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
    """select_guest_checkout: begin checkout → heading → email → continue."""

    def test_select_guest_checkout_sequence(self):
        selenium = _make_driver()
        begin_el = MagicMock()
        heading_el = MagicMock()
        email_el = MagicMock()
        continue_el = MagicMock()

        def side_effect(_method, selector):
            clean = selector.strip()
            if clean == SEL_BEGIN_CHECKOUT:
                return [begin_el]
            if clean == SEL_GUEST_HEADING:
                return [heading_el]
            if clean == SEL_GUEST_EMAIL:
                return [email_el]
            if clean == SEL_GUEST_CONTINUE:
                return [continue_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium, strict=False)

        with patch("time.sleep"), patch.object(gd, "_wait_for_url"):
            gd.select_guest_checkout("guest@example.com")

        begin_el.click.assert_called_once()
        heading_el.click.assert_called_once()
        email_el.send_keys.assert_called_with("guest@example.com")
        continue_el.click.assert_called_once()

    def test_select_guest_checkout_raises_if_begin_checkout_missing(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        with patch("time.sleep"):
            with self.assertRaises(SelectorTimeoutError):
                gd.select_guest_checkout("guest@example.com")

    def test_select_guest_checkout_raises_if_guest_heading_missing(self):
        selenium = _make_driver()
        begin_el = MagicMock()

        def side_effect(_method, selector):
            clean = selector.strip()
            if clean == SEL_BEGIN_CHECKOUT:
                return [begin_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)
        with patch("time.sleep"), patch.object(gd, "_wait_for_url"):
            with self.assertRaises(SelectorTimeoutError):
                gd.select_guest_checkout("guest@example.com")

    def test_select_guest_checkout_raises_if_email_field_missing(self):
        selenium = _make_driver()
        begin_el = MagicMock()
        heading_el = MagicMock()

        def side_effect(_method, selector):
            clean = selector.strip()
            if clean == SEL_BEGIN_CHECKOUT:
                return [begin_el]
            if clean == SEL_GUEST_HEADING:
                return [heading_el]
            return []

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium)
        with patch("time.sleep"), patch.object(gd, "_wait_for_url"):
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

        with patch.object(gd, "_cdp_select_option") as mock_select, \
             patch("time.sleep"):
            gd.fill_payment_and_billing(task.primary_card, billing)

        # Card fields go through CDP dispatchKeyEvent; collect "text" params.
        cdp_chars = "".join(
            c[0][1].get("text", "") for c in selenium.execute_cdp_cmd.call_args_list
            if len(c[0]) >= 2 and isinstance(c[0][1], dict) and c[0][1].get("type") == "keyDown"
        )
        self.assertIn(task.primary_card.card_name, cdp_chars)
        self.assertIn(task.primary_card.card_number, cdp_chars)
        self.assertIn(task.primary_card.cvv, cdp_chars)

        # Billing text fields (typed as whole string via _cdp_type_field).
        sent_values = [c.args[0] for c in element.send_keys.call_args_list]
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
        """Verify card_name characters are dispatched via CDP key events."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]

        task = _make_task()
        billing = _make_billing()
        gd = GivexDriver(selenium)

        with patch.object(gd, "_cdp_select_option"), patch("time.sleep"):
            gd.fill_payment_and_billing(task.primary_card, billing)

        cdp_chars = "".join(
            c[0][1].get("text", "") for c in selenium.execute_cdp_cmd.call_args_list
            if len(c[0]) >= 2 and isinstance(c[0][1], dict) and c[0][1].get("type") == "keyDown"
        )
        self.assertIn("Jane Doe", cdp_chars)

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
        self.assertGreaterEqual(len(state_calls), 1)
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
            def fn(*_args, **_kwargs):
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

    def test_run_full_cycle_raises_on_none_email(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        task = _make_task()
        billing = BillingProfile(
            first_name="Jane",
            last_name="Doe",
            address="123 Main St",
            city="Portland",
            state="OR",
            zip_code="97201",
            phone="5035550100",
            email=None,
            country="US",
        )

        with self.assertRaises(ValueError):
            gd.run_full_cycle(task, billing)


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

        def side_effect(_method, selector):
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

        def side_effect(_method, selector):
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

        def side_effect(_method, selector):
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

        def side_effect(_method, selector):
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
    """navigate_to_egift waits for URL_EGIFT after clicking buy button."""

    def test_navigate_to_egift_waits_for_url(self):
        selenium = _make_driver(current_url=URL_EGIFT)
        btn_el = MagicMock()

        def side_effect(_method, selector):
            clean = selector.strip()
            if clean == "#button--accept-cookies":
                return []
            return [btn_el]

        selenium.find_elements.side_effect = side_effect
        gd = GivexDriver(selenium, strict=False)

        with patch("time.sleep"):
            gd.navigate_to_egift()

        # Verify only URL_BASE was navigated to (not URL_EGIFT directly)
        selenium.get.assert_called_once_with(URL_BASE)
        btn_el.click.assert_called_once()


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


# ── Helpers for persona-aware tests ─────────────────────────────────────────


def _make_persona(seed: int = 42):
    """Build a real PersonaProfile with fixed seed for deterministic tests."""
    from modules.delay.persona import PersonaProfile  # noqa: PLC0415
    return PersonaProfile(seed)


# ── TestSmoothScrollTo ──────────────────────────────────────────────────────


class TestSmoothScrollTo(unittest.TestCase):
    """_smooth_scroll_to scrolls into view and uses persona delay."""

    def test_smooth_scroll_to_calls_execute_script_with_scrollIntoView(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        with patch("time.sleep"):
            gd._smooth_scroll_to(SEL_GREETING_MSG)
        self.assertGreaterEqual(selenium.execute_script.call_count, 1)
        first_script = selenium.execute_script.call_args_list[0][0][0]
        self.assertIn("scrollIntoView", first_script)
        self.assertIn("behavior: 'smooth'", first_script)

    def test_smooth_scroll_to_noop_when_element_missing(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        gd._smooth_scroll_to(SEL_GREETING_MSG)
        selenium.execute_script.assert_not_called()

    def test_smooth_scroll_to_uses_persona_click_delay(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        persona.get_click_delay = MagicMock(return_value=0.12345)
        gd = GivexDriver(selenium, persona=persona)
        sleep_calls = []
        with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            gd._smooth_scroll_to(SEL_GREETING_MSG)
        self.assertTrue(sleep_calls)
        self.assertAlmostEqual(sleep_calls[-1], 0.12345)

    def test_smooth_scroll_to_uses_default_delay_without_persona(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        sleep_calls = []
        with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            gd._smooth_scroll_to(SEL_GREETING_MSG)
        self.assertTrue(sleep_calls)
        self.assertAlmostEqual(sleep_calls[-1], 0.15)

    def test_smooth_scroll_to_includes_correction_step(self):
        """fill_egift_form triggers a scrollBy correction via _smooth_scroll_to."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        givex_driver = GivexDriver(selenium)
        with patch.object(givex_driver, "_cdp_type_field"), \
             patch("modules.cdp.driver._random_greeting", return_value="Hi"), \
             patch("time.sleep"):
            givex_driver.fill_egift_form(_make_task(), _make_billing())
        all_scripts = [c[0][0] for c in selenium.execute_script.call_args_list]
        scrollby_scripts = [s for s in all_scripts if "scrollBy" in s]
        self.assertGreaterEqual(
            len(scrollby_scripts), 1,
            "expected at least one scrollBy correction call",
        )


# ── TestBoundingBoxClickCoordinates ─────────────────────────────────────────


class TestBoundingBoxClickCoordinates(unittest.TestCase):
    """bounding_box_click uses CDP dispatchMouseEvent with persona offset."""

    def _rect(self):
        return {"left": 100.0, "top": 200.0, "width": 50.0, "height": 30.0}

    def test_bounding_box_click_uses_cdp_dispatchMouseEvent(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        # GhostCursor emits mouseMoved (button=none); the click path emits
        # mouseMoved + mousePressed + mouseReleased (button=left).
        click_calls = [
            c for c in selenium.execute_cdp_cmd.call_args_list
            if c[0][1].get("button") == "left"
        ]
        self.assertEqual(len(click_calls), 3)
        event_types = [c[0][1]["type"] for c in click_calls]
        self.assertEqual(event_types, ["mouseMoved", "mousePressed", "mouseReleased"])

    def test_bounding_box_click_offset_within_bounds(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        rect = self._rect()
        selenium.execute_script.return_value = rect
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        cx = rect["left"] + rect["width"] / 2
        cy = rect["top"] + rect["height"] / 2
        click_coords = []

        def capture_cdp(_cmd, params):
            if params.get("button") == "left":
                click_coords.append(params.copy())

        selenium.execute_cdp_cmd.side_effect = capture_cdp
        with patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        self.assertTrue(click_coords, "CDP click events should have been dispatched")
        x = click_coords[0]["x"]
        y = click_coords[0]["y"]
        self.assertGreaterEqual(x, cx - 15)
        self.assertLessEqual(x, cx + 15)
        self.assertGreaterEqual(y, cy - 5)
        self.assertLessEqual(y, cy + 5)

    def test_bounding_box_click_falls_back_to_plain_click_when_cdp_fails(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        selenium.execute_cdp_cmd.side_effect = RuntimeError("CDP unavailable")
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona, strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        element.click.assert_called_once()

    def test_bounding_box_click_falls_back_when_no_rect(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.side_effect = RuntimeError("script error")
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona, strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        element.click.assert_called_once()

    def test_bounding_box_click_night_mode_widens_offset(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        click_calls = []

        def capture_cdp(_cmd, params):
            if params.get("button") == "left":
                click_calls.append(params.copy())

        selenium.execute_cdp_cmd.side_effect = capture_cdp
        with patch.object(gd._temporal, "get_time_state", return_value="NIGHT"), \
             patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        self.assertGreater(len(click_calls), 0, "CDP click should have been called in NIGHT mode")
        # Click coordinates must still land within element bounds (clamped)
        rect = self._rect()
        x = click_calls[0]["x"]
        y = click_calls[0]["y"]
        self.assertGreaterEqual(x, rect["left"])
        self.assertLessEqual(x, rect["left"] + rect["width"])
        self.assertGreaterEqual(y, rect["top"])
        self.assertLessEqual(y, rect["top"] + rect["height"])


# ── TestGhostMoveTo ──────────────────────────────────────────────────────────


class TestGhostMoveTo(unittest.TestCase):
    """_ghost_move_to dispatches CDP mouseMoved events via GhostCursor."""

    def test_ghost_move_to_dispatches_cdp_mousemoved_multiple_times(self):
        """GhostCursor dispatches CDP mouseMoved ≥ 4 times when persona is set."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = {"left": 100.0, "top": 200.0, "width": 50.0, "height": 30.0}
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch("time.sleep"):
            gd._ghost_move_to("#some-el")
        mousemoved_calls = [
            c for c in selenium.execute_cdp_cmd.call_args_list
            if c[0][1].get("type") == "mouseMoved"
        ]
        self.assertGreaterEqual(len(mousemoved_calls), 4)

    def test_ghost_move_to_noop_when_cursor_unavailable(self):
        """Without persona (no GhostCursor) and no ActionChains, silently returns."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = {"left": 100.0, "top": 200.0, "width": 50.0, "height": 30.0}
        gd = GivexDriver(selenium)
        # No persona → _cursor is None; _ActionChains is None in test env
        with patch("time.sleep"):
            gd._ghost_move_to("#some-el")  # Should not raise

    def test_ghost_move_to_noop_when_element_missing(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        gd._ghost_move_to("#missing")
        selenium.execute_script.assert_not_called()

    def test_ghost_move_to_noop_when_execute_script_fails(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.side_effect = RuntimeError("script error")
        gd = GivexDriver(selenium)
        # Should not raise
        gd._ghost_move_to("#some-el")


# ── TestHesitateBeforeSubmit ─────────────────────────────────────────────────


class TestHesitateBeforeSubmit(unittest.TestCase):
    """_hesitate_before_submit sleeps 3-5s with hard clamps."""

    def test_hesitate_sleeps_between_3_and_5_seconds(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        sleep_vals = []
        with patch("time.sleep", side_effect=lambda d: sleep_vals.append(d)):
            gd._hesitate_before_submit()
        self.assertTrue(sleep_vals, "time.sleep should have been called")
        val = sleep_vals[-1]
        self.assertGreaterEqual(val, 3.0)
        self.assertLessEqual(val, 5.0)

    def test_hesitate_clamps_to_5_seconds_max(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        persona = _make_persona(42)
        persona.get_hesitation_delay = MagicMock(return_value=999.0)
        gd = GivexDriver(selenium, persona=persona)
        sleep_vals = []
        with patch("time.sleep", side_effect=lambda d: sleep_vals.append(d)):
            gd._hesitate_before_submit()
        self.assertAlmostEqual(sleep_vals[-1], 5.0)

    def test_hesitate_clamps_to_3_seconds_min(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        persona = _make_persona(42)
        persona.get_hesitation_delay = MagicMock(return_value=0.1)
        gd = GivexDriver(selenium, persona=persona)
        sleep_vals = []
        with patch("time.sleep", side_effect=lambda d: sleep_vals.append(d)):
            gd._hesitate_before_submit()
        self.assertAlmostEqual(sleep_vals[-1], 3.0)

    def test_hesitate_skips_when_engine_not_permitted(self):
        selenium = _make_driver()
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        gd._engine.is_delay_permitted = MagicMock(return_value=False)
        with patch("time.sleep") as mock_sleep:
            gd._hesitate_before_submit()
        mock_sleep.assert_not_called()

    def test_hesitate_without_persona_uses_random_in_range(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        sleep_vals = []
        with patch("time.sleep", side_effect=lambda d: sleep_vals.append(d)):
            gd._hesitate_before_submit()
        self.assertTrue(sleep_vals)
        val = sleep_vals[-1]
        self.assertGreaterEqual(val, 3.0)
        self.assertLessEqual(val, 5.0)


# ── TestHesitateScrollBehavior ───────────────────────────────────────────────


def _hesitate_rect():
    """Return a sample bounding rect for hesitate-scroll tests."""
    return {"left": 400.0, "top": 600.0, "width": 120.0, "height": 40.0}


class TestHesitateScrollBehavior(unittest.TestCase):
    """submit_purchase includes light scroll when button is visible."""

    def test_hesitate_performs_scroll_down_and_up_when_button_visible(self):
        """Scroll-down and scroll-up CDP events are dispatched during the hesitation window."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = _hesitate_rect()
        persona = _make_persona(42)
        givex_driver = GivexDriver(selenium, persona=persona)
        wheel_calls = []

        def capture_cdp(cmd, params):
            if params.get("type") == "mouseWheel":
                wheel_calls.append(params.get("deltaY", 0))

        selenium.execute_cdp_cmd.side_effect = capture_cdp
        with patch("time.sleep"), \
             patch("time.monotonic", return_value=0.0), \
             patch.object(givex_driver, "bounding_box_click"):
            givex_driver.submit_purchase()
        # At least one positive (scroll-down) and one negative (scroll-up) wheel event.
        self.assertTrue(any(d > 0 for d in wheel_calls), "expected scroll-down events")
        self.assertTrue(any(d < 0 for d in wheel_calls), "expected scroll-up events")

    def test_hesitate_scroll_skipped_when_no_button_found(self):
        """When no button element is found, no scrollBy is emitted."""
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        persona = _make_persona(42)
        givex_driver = GivexDriver(selenium, persona=persona)
        with patch("time.sleep"), \
             patch.object(givex_driver, "bounding_box_click"):
            givex_driver.submit_purchase()
        scrollby_calls = [
            c for c in selenium.execute_script.call_args_list
            if "scrollBy" in str(c[0][0])
        ]
        self.assertEqual(len(scrollby_calls), 0)

    def test_hesitate_scroll_skipped_when_rect_is_falsy(self):
        """When getBoundingClientRect returns falsy, no scrollBy is emitted."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = None
        persona = _make_persona(42)
        givex_driver = GivexDriver(selenium, persona=persona)
        with patch("time.sleep"), \
             patch.object(givex_driver, "bounding_box_click"):
            givex_driver.submit_purchase()
        scrollby_calls = [
            c for c in selenium.execute_script.call_args_list
            if "scrollBy" in str(c[0][0])
        ]
        self.assertEqual(len(scrollby_calls), 0)


# ── TestCdpClickAbsolute ─────────────────────────────────────────────────────


class TestCdpClickAbsolute(unittest.TestCase):
    """cdp_click_absolute sends three CDP mouse events at exact coordinates."""

    def test_cdp_click_absolute_sends_three_mouse_events(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        gd.cdp_click_absolute(100.0, 200.0)
        self.assertEqual(selenium.execute_cdp_cmd.call_count, 3)

    def test_cdp_click_absolute_passes_correct_coordinates(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        gd.cdp_click_absolute(123.5, 456.7)
        calls = selenium.execute_cdp_cmd.call_args_list
        event_types = [c[0][1]["type"] for c in calls]
        self.assertEqual(event_types, ["mouseMoved", "mousePressed", "mouseReleased"])
        for c in calls:
            params = c[0][1]
            self.assertAlmostEqual(params["x"], 123.5)
            self.assertAlmostEqual(params["y"], 456.7)
            self.assertEqual(params["button"], "left")
            self.assertEqual(params["clickCount"], 1)


# ── TestSubmitPurchaseHesitates ──────────────────────────────────────────────


class TestSubmitPurchaseHesitates(unittest.TestCase):
    """submit_purchase calls _hesitate_before_submit before bounding_box_click."""

    def test_submit_purchase_calls_hesitate_before_click(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        call_log = []
        with patch.object(gd, "_hesitate_before_submit", side_effect=lambda: call_log.append("hesitate")), \
             patch.object(gd, "bounding_box_click", side_effect=lambda s: call_log.append("click")):
            gd.submit_purchase()
        self.assertEqual(call_log, ["hesitate", "click"])

    def test_submit_purchase_exception_from_hesitate_propagates(self):
        """If _hesitate_before_submit raises, the exception propagates (bounding_box_click not called)."""
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        with patch.object(gd, "_hesitate_before_submit", side_effect=RuntimeError("boom")), \
             patch.object(gd, "bounding_box_click") as mock_click:
            with self.assertRaises(RuntimeError):
                gd.submit_purchase()
        mock_click.assert_not_called()


# ── TestFillEgiftFormScrolls ─────────────────────────────────────────────────


class TestFillEgiftFormScrolls(unittest.TestCase):
    """fill_egift_form calls _smooth_scroll_to before any field typing."""

    def test_fill_egift_form_calls_smooth_scroll_first(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium)
        call_log = []

        with patch.object(gd, "_smooth_scroll_to", side_effect=lambda s: call_log.append(("scroll", s))), \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             patch("time.sleep"):
            def tracking_type(sel, _val, **_kw):  # pylint: disable=unused-argument
                """Spy that records selector to call_log."""
                call_log.append(("type", sel))
            gd._realistic_type_field = tracking_type  # pylint: disable=protected-access
            gd.fill_egift_form(_make_task(), _make_billing())

        self.assertGreater(len(call_log), 1)
        self.assertEqual(call_log[0], ("scroll", SEL_GREETING_MSG))
        type_entries = [e for e in call_log if e[0] == "type"]
        self.assertTrue(type_entries, "send_keys calls expected after scroll")

    def test_fill_egift_form_with_persona_transitions_state(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch.object(gd._sm, "transition") as mock_transition, \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             patch("time.sleep"):
            gd.fill_egift_form(_make_task(), _make_billing())
        mock_transition.assert_any_call("FILLING_FORM")


# ── TestFillPaymentBiometrics ────────────────────────────────────────────────


class TestFillPaymentBiometrics(unittest.TestCase):
    """fill_payment_and_billing uses field-aware realistic typing for card fields."""

    def test_fill_payment_uses_realistic_type_for_card_fields(self):
        """Card fields use _realistic_type_field with field_kind; no separate delay injection."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        task = _make_task()
        billing = _make_billing()
        field_kinds = []
        original = gd._realistic_type_field
        def spy(sel, val, **kw):
            field_kinds.append(kw.get("field_kind", "text"))
            original(sel, val, **kw)
        with patch.object(gd, "_realistic_type_field", side_effect=spy), \
             patch.object(gd, "_cdp_select_option"), patch("time.sleep"):
            gd.fill_payment_and_billing(task.primary_card, billing)
        self.assertIn("name", field_kinds)
        self.assertIn("card_number", field_kinds)
        self.assertIn("cvv", field_kinds)

    def test_fill_payment_with_persona_transitions_payment_state(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        # Transition to FILLING_FORM first so PAYMENT is a valid next state
        gd._sm.transition("FILLING_FORM")
        task = _make_task()
        billing = _make_billing()
        with patch.object(gd, "_cdp_select_option"), patch("time.sleep"):
            with patch.object(gd._sm, "transition") as mock_transition:
                gd.fill_payment_and_billing(task.primary_card, billing)
        mock_transition.assert_any_call("PAYMENT")

    def test_fill_payment_no_biometrics_without_persona(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        task = _make_task()
        billing = _make_billing()
        with patch.object(gd, "_cdp_select_option"), patch("time.sleep"):
            gd.fill_payment_and_billing(task.primary_card, billing)


# ── TestStrictMode ───────────────────────────────────────────────────────────


class TestStrictMode(unittest.TestCase):
    """GivexDriver(strict=True) suppresses .click() fallback on CDP failure."""

    @staticmethod
    def _rect():
        return {"left": 100.0, "top": 200.0, "width": 80.0, "height": 30.0}

    def test_strict_mode_no_click_fallback_when_cdp_fails(self):
        """In strict mode, .click() must NOT be called when CDP fails."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        selenium.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona, strict=True)
        with patch("time.sleep"):
            gd.bounding_box_click("#el")
        element.click.assert_not_called()

    def test_non_strict_mode_uses_click_fallback_when_cdp_fails(self):
        """Without strict mode, .click() fallback is still used on CDP failure."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        selenium.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona, strict=False)
        with patch("time.sleep"):
            gd.bounding_box_click("#el")
        element.click.assert_called_once()

    def test_strict_mode_emits_warning_on_cdp_failure(self):
        """Strict mode logs WARNING when CDP interaction is suppressed."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        selenium.execute_cdp_cmd.side_effect = RuntimeError("CDP gone")
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona, strict=True)
        with patch("time.sleep"):
            with self.assertLogs("modules.cdp.driver", level="WARNING") as cm:
                gd.bounding_box_click("#el")
        self.assertTrue(any("strict" in msg.lower() for msg in cm.output))

    def test_default_strict_is_true(self):
        """GivexDriver defaults to strict mode."""
        selenium = _make_driver()
        givex_driver = GivexDriver(selenium)
        self.assertTrue(givex_driver._strict)  # pylint: disable=protected-access

    def test_explicit_strict_false_is_supported(self):
        """GivexDriver(strict=False) enables legacy fallback mode."""
        selenium = _make_driver()
        givex_driver = GivexDriver(selenium, strict=False)
        self.assertFalse(givex_driver._strict)  # pylint: disable=protected-access


# ── TestRealisticTypeField ───────────────────────────────────────────────────


class TestRealisticTypeField(unittest.TestCase):
    """_realistic_type_field dispatches via CDP key events through keyboard module."""

    def test_realistic_type_uses_keyboard_module_when_available(self):
        """_realistic_type_field calls keyboard.type_value with driver arg."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch("modules.cdp.driver._type_value") as mock_tv, \
             patch("time.sleep"):
            mock_tv.return_value = {"typed_chars": 4, "typos_injected": 0,
                                    "corrections_made": 0, "mode": "cdp_key"}
            gd._realistic_type_field("#some-field", "test")
        mock_tv.assert_called_once()
        # First positional arg is the driver, second is the element.
        self.assertEqual(mock_tv.call_args[0][0], selenium)
        self.assertEqual(mock_tv.call_args[0][1], element)
        self.assertEqual(mock_tv.call_args[0][2], "test")

    def test_realistic_type_falls_back_when_keyboard_unavailable(self):
        """When _type_value is None, falls back to _cdp_type_field."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        with patch("modules.cdp.driver._type_value", None):
            with patch.object(gd, "_cdp_type_field") as mock_fallback:
                gd._realistic_type_field("#field", "value")
        mock_fallback.assert_called_once_with("#field", "value")

    def test_realistic_type_raises_on_missing_element(self):
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)
        with self.assertRaises(SelectorTimeoutError):
            gd._realistic_type_field("#missing", "x")

    def test_realistic_type_passes_field_kind(self):
        """field_kind is forwarded to type_value."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch("modules.cdp.driver._type_value") as mock_tv, \
             patch("time.sleep"):
            mock_tv.return_value = {"typed_chars": 3, "typos_injected": 0,
                                    "corrections_made": 0, "mode": "cdp_key"}
            gd._realistic_type_field("#cvv", "123", field_kind="cvv")
        self.assertEqual(mock_tv.call_args[1]["field_kind"], "cvv")

    def test_realistic_type_uses_burst_delays_for_card_number(self):
        """With use_burst=True and 16-char value, uses 4x4 delay pattern."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch("modules.cdp.driver._type_value") as mock_tv, \
             patch("time.sleep"):
            mock_tv.return_value = {"typed_chars": 16, "typos_injected": 0,
                                    "corrections_made": 0, "mode": "cdp_key"}
            gd._realistic_type_field("#card", "4111111111111111", use_burst=True)
        kwargs = mock_tv.call_args[1]
        delays = kwargs.get("delays")
        self.assertIsNotNone(delays)
        self.assertEqual(len(delays), 19)

    def test_realistic_type_temporal_adjusts_typo_rate(self):
        """Night temporal state adds to persona typo_rate passed to type_value."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(0)
        persona.typo_rate = 0.02
        gd = GivexDriver(selenium, persona=persona)
        received_typo_rate = []
        def capture_tv(drv, el, val, rnd, **kw):
            received_typo_rate.append(kw.get("typo_rate", 0.0))
            return {"typed_chars": 1, "typos_injected": 0, "corrections_made": 0, "mode": "cdp_key"}
        with patch("modules.cdp.driver._type_value", side_effect=capture_tv), \
             patch.object(gd._temporal, "get_night_typo_increase", return_value=0.015), \
             patch("time.sleep"):
            gd._realistic_type_field("#f", "x")
        self.assertAlmostEqual(received_typo_rate[0], 0.02 + 0.015, places=4)


# ── TestHesitationDistribution ───────────────────────────────────────────────


class TestHesitationDistribution(unittest.TestCase):
    """_hesitate_before_submit distributes behavior across 4 equal time slots."""

    @staticmethod
    def _rect():
        return {"left": 400.0, "top": 600.0, "width": 120.0, "height": 40.0}

    def test_hesitation_distributes_sleep_across_slots(self):
        """When rect is available, sleep is distributed across multiple slots."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        persona = _make_persona(42)
        persona.get_hesitation_delay = MagicMock(return_value=4.0)
        gd = GivexDriver(selenium, persona=persona)
        # Mock cursor methods to avoid extra internal sleeps from GhostCursor.
        gd._cursor.scroll_wheel = MagicMock()
        gd._cursor.move_to = MagicMock()
        sleep_calls = []
        with patch("time.sleep", side_effect=sleep_calls.append), \
             patch("time.monotonic", return_value=0.0):
            gd._hesitate_before_submit()
        # 4 slots of 1.0 s each; remaining = 1.0 since monotonic always returns 0.
        self.assertEqual(len(sleep_calls), 4)
        self.assertAlmostEqual(sum(sleep_calls), 4.0, places=5)

    def test_hesitation_scroll_wheel_used_when_cursor_available(self):
        """When GhostCursor is present, scroll_wheel is called (not scrollBy)."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        wheel_calls = []
        gd._cursor.scroll_wheel = MagicMock(side_effect=lambda delta_y, **kw: wheel_calls.append(delta_y))
        with patch("time.sleep"), patch("time.monotonic", return_value=0.0):
            gd._hesitate_before_submit()
        self.assertGreater(len(wheel_calls), 0)


class TestRealisticTypeFieldPassesEngine(unittest.TestCase):
    """_realistic_type_field forwards the delay engine to type_value."""

    def test_engine_kwarg_passed_to_type_value(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch("modules.cdp.driver._type_value") as mock_tv, \
             patch("time.sleep"):
            mock_tv.return_value = {"typed_chars": 3, "typos_injected": 0,
                                    "corrections_made": 0, "mode": "cdp_key"}
            gd._realistic_type_field("#field", "abc")
        kwargs = mock_tv.call_args[1]
        self.assertIn("engine", kwargs)
        self.assertIs(kwargs["engine"], gd._engine)

    def test_engine_none_when_no_persona(self):
        """Without persona, engine is None and still passed through."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        with patch("modules.cdp.driver._type_value") as mock_tv, \
             patch("time.sleep"):
            mock_tv.return_value = {"typed_chars": 3, "typos_injected": 0,
                                    "corrections_made": 0, "mode": "cdp_key"}
            gd._realistic_type_field("#field", "abc")
        kwargs = mock_tv.call_args[1]
        self.assertIn("engine", kwargs)
        self.assertIsNone(kwargs["engine"])


if __name__ == "__main__":
    unittest.main()
