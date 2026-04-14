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
        gd = GivexDriver(selenium)

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
        gd = GivexDriver(selenium)

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
        gd = GivexDriver(selenium)

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
        selenium.execute_script.assert_called_once()
        script_arg = selenium.execute_script.call_args[0][0]
        self.assertIn("scrollIntoView", script_arg)
        self.assertIn("behavior: 'smooth'", script_arg)

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
        expected_delay = persona.get_click_delay()
        # Reset persona so get_click_delay returns same value
        persona2 = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona2)
        sleep_calls = []
        with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            gd._smooth_scroll_to(SEL_GREETING_MSG)
        self.assertTrue(sleep_calls)
        self.assertAlmostEqual(sleep_calls[-1], expected_delay, places=10)

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
        cdp_calls = [c for c in selenium.execute_cdp_cmd.call_args_list]
        self.assertEqual(len(cdp_calls), 3)
        event_types = [c[0][1]["type"] for c in cdp_calls]
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
        x_vals, y_vals = [], []
        def capture_cdp(cmd, params):
            x_vals.append(params["x"])
            y_vals.append(params["y"])
        selenium.execute_cdp_cmd.side_effect = capture_cdp
        with patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        self.assertTrue(x_vals, "CDP should have been called")
        self.assertGreaterEqual(x_vals[0], cx - 15)
        self.assertLessEqual(x_vals[0], cx + 15)
        self.assertGreaterEqual(y_vals[0], cy - 5)
        self.assertLessEqual(y_vals[0], cy + 5)

    def test_bounding_box_click_falls_back_to_plain_click_when_cdp_fails(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = self._rect()
        selenium.execute_cdp_cmd.side_effect = RuntimeError("CDP unavailable")
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        with patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        element.click.assert_called_once()

    def test_bounding_box_click_falls_back_when_no_rect(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.side_effect = RuntimeError("script error")
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
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
        cdp_calls = []
        selenium.execute_cdp_cmd.side_effect = lambda cmd, params: cdp_calls.append(params.copy())
        with patch.object(gd._temporal, "get_time_state", return_value="NIGHT"), \
             patch("time.sleep"):
            gd.bounding_box_click("#some-el")
        self.assertGreater(len(cdp_calls), 0, "CDP should have been called in NIGHT mode")
        # Coordinates must still land within element bounds (clamped)
        rect = self._rect()
        x = cdp_calls[0]["x"]
        y = cdp_calls[0]["y"]
        self.assertGreaterEqual(x, rect["left"])
        self.assertLessEqual(x, rect["left"] + rect["width"])
        self.assertGreaterEqual(y, rect["top"])
        self.assertLessEqual(y, rect["top"] + rect["height"])


# ── TestGhostMoveTo ──────────────────────────────────────────────────────────


class TestGhostMoveTo(unittest.TestCase):
    """_ghost_move_to generates a Bézier path and moves the mouse along it."""

    def test_ghost_move_to_calls_move_by_offset_multiple_times(self):
        """ActionChains.move_by_offset is called ≥ 4 times when ActionChains is available."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = {"left": 100.0, "top": 200.0, "width": 50.0, "height": 30.0}
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        mock_actions = MagicMock()
        mock_actions.move_by_offset.return_value = mock_actions
        mock_actions.perform.return_value = None
        mock_actions_cls = MagicMock(return_value=mock_actions)
        # Patch at the location where _ghost_move_to imports ActionChains
        with patch("time.sleep"), \
             patch.dict("sys.modules", {"selenium": MagicMock(), "selenium.webdriver": MagicMock(),
                                        "selenium.webdriver.common": MagicMock(),
                                        "selenium.webdriver.common.action_chains": MagicMock(ActionChains=mock_actions_cls)}):
            gd._ghost_move_to("#some-el")
        self.assertGreaterEqual(mock_actions.move_by_offset.call_count, 4)

    def test_ghost_move_to_calls_move_by_offset_without_selenium(self):
        """Without selenium, _ghost_move_to silently returns (no-op)."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        selenium.execute_script.return_value = {"left": 100.0, "top": 200.0, "width": 50.0, "height": 30.0}
        gd = GivexDriver(selenium)
        # Should not raise even though ActionChains is unavailable
        with patch("time.sleep"):
            gd._ghost_move_to("#some-el")

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
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium)
        call_log = []

        with patch.object(gd, "_smooth_scroll_to", side_effect=lambda s: call_log.append(("scroll", s))), \
             patch.object(drv, "_random_greeting", return_value="Hi"), \
             patch("time.sleep"):
            original_type = gd._cdp_type_field
            def tracking_type(sel, val):
                call_log.append(("type", sel))
                original_type(sel, val)
            gd._cdp_type_field = tracking_type
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
    """fill_payment_and_billing injects card-entry biometric delays when bio is available."""

    def test_fill_payment_calls_inject_card_entry_delays_when_bio_available(self):
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = _make_persona(42)
        gd = GivexDriver(selenium, persona=persona)
        task = _make_task()
        billing = _make_billing()
        with patch.object(gd, "_cdp_select_option"), \
             patch("modules.cdp.driver._inject_card_entry_delays") as mock_inject:
            gd.fill_payment_and_billing(task.primary_card, billing)
        mock_inject.assert_called_once_with(gd._bio, engine=gd._engine)

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
        with patch.object(gd, "_cdp_select_option"), \
             patch("modules.cdp.driver._inject_card_entry_delays"):
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
        with patch.object(gd, "_cdp_select_option"), \
             patch("modules.cdp.driver._inject_card_entry_delays") as mock_inject:
            gd.fill_payment_and_billing(task.primary_card, billing)
        mock_inject.assert_not_called()


if __name__ == "__main__":
    unittest.main()
