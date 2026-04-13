"""GivexDriver — Givex e-gift card purchase automation driver.

Implements the full happy-path flow for purchasing Givex e-gift cards
via Chrome DevTools Protocol (CDP) / Selenium.  All selector constants
are defined at module level so they can be patched in tests without
touching the class.

URL flow:
  Step 0:  https://wwws-usa2.givex.com/cws4.0/lushusa/
  Step 1:  https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/
  Step 2a: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/shopping-cart.html
  Step 2b: https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/checkout.html
  Step 4:  https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html
"""

from __future__ import annotations

import logging
import random
import time

from modules.common.exceptions import SelectorTimeoutError

_log = logging.getLogger(__name__)

# ── URL constants ──────────────────────────────────────────────────────────────
URL_BASE       = "https://wwws-usa2.givex.com/cws4.0/lushusa/"
URL_EGIFT      = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/"
URL_CART       = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/shopping-cart.html"
URL_CHECKOUT   = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/checkout.html"
URL_PAYMENT    = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html"
URL_GEO_CHECK  = "https://lumtest.com/myip.json"

# URL fragments used to detect order confirmation
URL_CONFIRM_FRAGMENTS = ("/confirmation", "/order-confirmation", "order-confirm")

# ── Step 0: Navigation ─────────────────────────────────────────────────────────
SEL_COOKIE_ACCEPT = "#button--accept-cookies"
SEL_BUY_EGIFT_BTN = "#cardForeground > div > div.bannerButtons.clearfix > div.bannerBtn.btn1.displaySectionYes > a"

# ── Step 1: eGift form ─────────────────────────────────────────────────────────
SEL_GREETING_MSG         = "#cws_txt_gcMsg"
SEL_AMOUNT_INPUT         = "#cws_txt_gcBuyAmt"
SEL_RECIPIENT_NAME       = "#cws_txt_gcBuyTo"
SEL_RECIPIENT_EMAIL      = "#cws_txt_recipEmail"
SEL_CONFIRM_RECIP_EMAIL  = "#cws_txt_confRecipEmail"
SEL_SENDER_NAME          = "#cws_txt_gcBuyFrom"
SEL_ADD_TO_CART          = "#cws_btn_gcBuyAdd > span"
SEL_REVIEW_CHECKOUT      = "#cws_btn_gcBuyCheckout"

# ── Step 2a: Cart ──────────────────────────────────────────────────────────────
SEL_BEGIN_CHECKOUT = "#cws_btn_cartCheckout"

# ── Step 2b: Guest Checkout ────────────────────────────────────────────────────
SEL_GUEST_EMAIL    = "#cws_txt_guestEmail"
SEL_GUEST_CONTINUE = "#cws_btn_guestChkout"

# ── Step 4: Payment — Card fields ─────────────────────────────────────────────
SEL_CARD_NAME         = "#cws_txt_ccName"
SEL_CARD_NUMBER       = "#cws_txt_ccNum"
SEL_CARD_EXPIRY_MONTH = "#cws_list_ccExpMon"
SEL_CARD_EXPIRY_YEAR  = "#cws_list_ccExpYr"
SEL_CARD_CVV          = "#cws_txt_ccCvv"

# ── Step 4: Payment — Billing fields ──────────────────────────────────────────
SEL_BILLING_ADDRESS  = "#cws_txt_billingAddr1"
SEL_BILLING_COUNTRY  = "#cws_list_billingCountry"
SEL_BILLING_STATE    = "#cws_list_billingProvince"
SEL_BILLING_CITY     = "#cws_txt_billingCity"
SEL_BILLING_ZIP      = "#cws_txt_billingPostal"
SEL_BILLING_PHONE    = "#cws_txt_billingPhone"

# ── Step 4: Complete Purchase ──────────────────────────────────────────────────
SEL_COMPLETE_PURCHASE = "#cws_btn_checkoutPay"

# ── Step 5: Post-submit state detection ───────────────────────────────────────
SEL_CONFIRMATION_EL = ".order-confirmation, .confirmation-message"
SEL_DECLINED_MSG    = ".payment-error, .error-message, div[data-error]"
SEL_UI_LOCK_SPINNER = ".loading-overlay, .spinner, div[aria-busy='true']"
SEL_VBV_IFRAME      = "iframe[src*='3dsecure'], iframe[src*='adyen'], iframe[id*='threeds']"
SEL_VBV_CANCEL_BTN  = "button[id*='cancel'], a[id*='cancel'], button[id*='return'], a[id*='return']"
SEL_POPUP_CLOSE_BTN = "button.modal-close, button[aria-label='Close'], .modal button[type='button']"
SEL_NEUTRAL_DIV     = "body"

_GREETINGS = [
    "Happy Birthday!", "Best wishes!", "Enjoy your gift!",
    "Thank you for being you", "Thinking of you!",
    "Have a wonderful day!", "You deserve this!",
    "With love and appreciation", "Celebrate you!",
    "Wishing you all the best!",
]


def _random_greeting() -> str:
    """Return a random greeting message for the eGift form."""
    return random.choice(_GREETINGS)


class GivexDriver:
    """Automates the Givex e-gift card purchase flow using CDP/Selenium.

    Args:
        driver: A Selenium WebDriver instance (or test double).
    """

    def __init__(self, driver: object) -> None:
        self._driver = driver

    # ── Low-level helpers ──────────────────────────────────────────────────────

    def find_elements(self, selector: str) -> list:
        """Return elements matching *selector* (CSS, comma-separated OK).

        Iterates over each comma-separated sub-selector and returns the first
        non-empty match list, falling back to an empty list when none match.
        """
        for part in selector.split(","):
            part = part.strip()
            try:
                elements = self._driver.find_elements("css selector", part)
            except Exception:
                elements = []
            if elements:
                return elements
        return []

    def _wait_for_element(self, selector: str, timeout: int = 10) -> bool:
        """Poll until *selector* matches at least one element or *timeout* expires.

        Returns:
            True if found within *timeout* seconds, False otherwise.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.find_elements(selector):
                return True
            time.sleep(0.5)
        return False

    def _cdp_type_field(self, selector: str, value: str) -> None:
        """Clear element and type *value* into it using send_keys.

        Raises:
            SelectorTimeoutError: if no matching element is found.
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)
        el = elements[0]
        try:
            el.clear()
        except Exception:
            pass
        el.send_keys(value)

    def _cdp_select_option(self, selector: str, value: str) -> None:
        """Select the option matching *value* in a <select> element.

        Raises:
            SelectorTimeoutError: if no matching element is found.
        """
        from selenium.webdriver.support.ui import Select  # type: ignore[import]

        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)
        Select(elements[0]).select_by_value(value)

    def bounding_box_click(self, selector: str) -> None:
        """Click the first element matching *selector*.

        Raises:
            SelectorTimeoutError: if no matching element is found.
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)
        elements[0].click()

    def _close_extra_tabs(self) -> None:
        """Close all browser tabs except the first one."""
        handles = self._driver.window_handles
        if len(handles) <= 1:
            return
        first = handles[0]
        for handle in handles[1:]:
            self._driver.switch_to.window(handle)
            self._driver.close()
        self._driver.switch_to.window(first)

    def _hard_reset_state(self) -> None:
        """Clear cookies, localStorage, and sessionStorage via JavaScript."""
        self._driver.execute_script(
            "document.cookie.split(';').forEach(function(c){"
            "document.cookie=c.replace(/^ +/,'').replace(/=.*/,'=;expires='+new Date().toUTCString()+';path=/');"
            "});"
        )
        self._driver.execute_script("localStorage.clear();")
        self._driver.execute_script("sessionStorage.clear();")

    # ── Step 0: Navigation ─────────────────────────────────────────────────────

    def preflight_geo_check(self) -> None:
        """Navigate to geo-check URL and assert the IP is US-based.

        Raises:
            RuntimeError: if the detected country is not "US".
        """
        import json as _json

        self._driver.get(URL_GEO_CHECK)
        try:
            body = self._driver.find_element("tag name", "body").text
            data = _json.loads(body)
            country = data.get("country", "")
        except Exception as exc:
            raise RuntimeError(f"Geo-check failed: {exc}") from exc
        if country != "US":
            raise RuntimeError(
                f"Geo-check failed: expected country 'US', got {country!r}"
            )

    def navigate_to_egift(self) -> None:
        """Navigate to base URL, dismiss cookie banner, click Buy eGift Cards.

        Then navigate directly to eGift URL and hard-reset browser state.
        """
        self._driver.get(URL_BASE)
        self._close_extra_tabs()
        # Dismiss cookie banner if present (best-effort)
        if self.find_elements(SEL_COOKIE_ACCEPT):
            try:
                self.bounding_box_click(SEL_COOKIE_ACCEPT)
            except Exception:
                pass
        self._wait_for_element(SEL_BUY_EGIFT_BTN, timeout=10)
        self.bounding_box_click(SEL_BUY_EGIFT_BTN)
        # Navigate directly to eGift URL and hard-reset state
        self._driver.get(URL_EGIFT)
        self._hard_reset_state()

    # ── Step 1: eGift form ─────────────────────────────────────────────────────

    def fill_egift_form(self, task, billing_profile) -> None:
        """Fill all fields on the eGift purchase form.

        Field order matches the page layout:
          1. Greeting message (random)
          2. Amount
          3. Recipient Name (To)
          4. Recipient Email
          5. Confirm Recipient Email
          6. Sender Name (From)

        Args:
            task: WorkerTask with recipient_email and amount.
            billing_profile: BillingProfile with first_name and last_name.
        """
        full_name = f"{billing_profile.first_name} {billing_profile.last_name}"
        self._cdp_type_field(SEL_GREETING_MSG, _random_greeting())
        self._cdp_type_field(SEL_AMOUNT_INPUT, str(task.amount))
        self._cdp_type_field(SEL_RECIPIENT_NAME, full_name)
        self._cdp_type_field(SEL_RECIPIENT_EMAIL, task.recipient_email)
        self._cdp_type_field(SEL_CONFIRM_RECIP_EMAIL, task.recipient_email)
        self._cdp_type_field(SEL_SENDER_NAME, full_name)

    def add_to_cart_and_checkout(self) -> None:
        """Click ADD TO CART, wait for REVIEW & CHECKOUT, then click it."""
        self.bounding_box_click(SEL_ADD_TO_CART)
        found = self._wait_for_element(SEL_REVIEW_CHECKOUT, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_REVIEW_CHECKOUT, 10)
        self.bounding_box_click(SEL_REVIEW_CHECKOUT)

    # ── Step 2a: Cart ──────────────────────────────────────────────────────────

    def begin_checkout(self) -> None:
        """At the cart page, click BEGIN CHECKOUT."""
        found = self._wait_for_element(SEL_BEGIN_CHECKOUT, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_BEGIN_CHECKOUT, 10)
        self.bounding_box_click(SEL_BEGIN_CHECKOUT)

    # ── Step 2b: Guest Checkout ────────────────────────────────────────────────

    def select_guest_checkout(self, guest_email: str) -> None:
        """Enter guest email and click CONTINUE to proceed as guest.

        Args:
            guest_email: Email address to enter in the guest checkout field.
        """
        found = self._wait_for_element(SEL_GUEST_EMAIL, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_GUEST_EMAIL, 10)
        self._cdp_type_field(SEL_GUEST_EMAIL, guest_email)
        self.bounding_box_click(SEL_GUEST_CONTINUE)

    # ── Step 4: Payment ────────────────────────────────────────────────────────

    def fill_card(self, card_info) -> None:
        """Fill credit-card payment fields using 4x4 grouped typing.

        Card number is typed in four groups of four digits with a random
        inter-group pause (0.6-1.8s) to mimic natural card-entry behaviour.
        After CVV, hesitation pause (3-5s) before completing.

        Args:
            card_info: CardInfo with card_number, exp_month, exp_year, cvv.
        """
        # Name on card (first + last from billing profile — caller supplies full name as card_info.name if available)
        card_name = getattr(card_info, "name_on_card", None)
        if card_name:
            self._cdp_type_field(SEL_CARD_NAME, card_name)

        # Card number — 4x4 grouped typing
        card_number = card_info.card_number.replace(" ", "").replace("-", "")
        groups = [card_number[i:i + 4] for i in range(0, len(card_number), 4)]
        elements = self.find_elements(SEL_CARD_NUMBER)
        if not elements:
            raise SelectorTimeoutError(SEL_CARD_NUMBER, 0)
        el = elements[0]
        try:
            el.clear()
        except Exception:
            pass
        for i, group in enumerate(groups):
            el.send_keys(group)
            if i < len(groups) - 1:
                time.sleep(random.uniform(0.6, 1.8))

        # Expiry month and year
        self._cdp_select_option(SEL_CARD_EXPIRY_MONTH, card_info.exp_month)
        self._cdp_select_option(SEL_CARD_EXPIRY_YEAR, card_info.exp_year)

        # CVV
        self._cdp_type_field(SEL_CARD_CVV, card_info.cvv)

        # Hesitation: 3-5s before completing purchase
        time.sleep(random.uniform(3, 5))

    def fill_billing(self, billing_profile) -> None:
        """Fill all billing address fields on the payment page.

        Fields filled (all on payment page):
          - Address 1
          - Country
          - State/Province
          - City
          - ZIP/Postal Code
          - Phone Number

        Args:
            billing_profile: BillingProfile instance with address details.
        """
        self._cdp_type_field(SEL_BILLING_ADDRESS, billing_profile.address)
        # Country — select by value (e.g. "US")
        country = getattr(billing_profile, "country", "US")
        try:
            self._cdp_select_option(SEL_BILLING_COUNTRY, country)
        except Exception:
            pass
        self._cdp_select_option(SEL_BILLING_STATE, billing_profile.state)
        self._cdp_type_field(SEL_BILLING_CITY, billing_profile.city)
        self._cdp_type_field(SEL_BILLING_ZIP, billing_profile.zip_code)
        if billing_profile.phone:
            self._cdp_type_field(SEL_BILLING_PHONE, billing_profile.phone)

    # Keep fill_billing_form as alias
    def fill_billing_form(self, billing_profile) -> None:
        """Alias for fill_billing (backward compatibility)."""
        self.fill_billing(billing_profile)

    def submit_purchase(self) -> None:
        """Click the COMPLETE PURCHASE button."""
        self.bounding_box_click(SEL_COMPLETE_PURCHASE)

    def clear_card_fields(self) -> None:
        """Clear card number and CVV fields (best-effort, no page reload)."""
        for selector in (SEL_CARD_NUMBER, SEL_CARD_CVV):
            elements = self.find_elements(selector)
            if elements:
                try:
                    elements[0].clear()
                except Exception:
                    pass

    # ── Step 5: Post-submit state detection ───────────────────────────────────

    def detect_page_state(self) -> str:
        """Inspect current page and return FSM state name.

        Detection order:
        1. success  — URL contains confirmation fragment OR .order-confirmation present
        2. vbv_3ds  — 3DS/Adyen iframe present
        3. declined — payment-error element present OR page text contains "declined"
        4. ui_lock  — loading overlay/spinner present
        5. unknown  — none matched

        Returns:
            One of: "success", "vbv_3ds", "declined", "ui_lock", "unknown".
        """
        current_url = ""
        try:
            current_url = self._driver.current_url
        except Exception:
            pass

        # 1 — success
        if any(frag in current_url for frag in URL_CONFIRM_FRAGMENTS):
            return "success"
        if self.find_elements(SEL_CONFIRMATION_EL):
            return "success"

        # 2 — vbv_3ds
        if self.find_elements(SEL_VBV_IFRAME):
            return "vbv_3ds"

        # 3 — declined
        if self.find_elements(SEL_DECLINED_MSG):
            return "declined"
        try:
            page_text = self._driver.find_element("tag name", "body").text.lower()
            if "declined" in page_text or "transaction failed" in page_text:
                return "declined"
        except Exception:
            pass

        # 4 — ui_lock
        if self.find_elements(SEL_UI_LOCK_SPINNER):
            return "ui_lock"

        return "unknown"

    # ── Full-cycle orchestrator ────────────────────────────────────────────────

    def run_full_cycle(self, task, billing_profile) -> str:
        """Execute the complete happy-path purchase flow end-to-end.

        Flow:
        0. Geo pre-flight check
        0. Navigate to eGift page (cookie banner + hard reset)
        1. Fill eGift form (greeting, amount, recipient, confirm email, sender)
        1. Add to cart → Review & Checkout
        2a. Begin Checkout (cart page)
        2b. Guest checkout (email + continue)
        4. Fill billing (address, country, state, city, zip, phone)
        4. Fill card (name, number 4x4, expiry, cvv) + hesitation
        4. Submit purchase (COMPLETE PURCHASE)
        5. Detect and return page state

        Args:
            task: WorkerTask with purchase details.
            billing_profile: BillingProfile with address and email.

        Returns:
            FSM state string from detect_page_state().
        """
        self.preflight_geo_check()
        self.navigate_to_egift()
        self.fill_egift_form(task, billing_profile)
        self.add_to_cart_and_checkout()
        self.begin_checkout()
        self.select_guest_checkout(billing_profile.email)
        self.fill_billing(billing_profile)
        self.fill_card(task.primary_card)
        self.submit_purchase()
        return self.detect_page_state()