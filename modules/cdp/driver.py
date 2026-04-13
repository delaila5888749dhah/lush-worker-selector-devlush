"""GivexDriver — Givex e-gift card purchase automation driver.

Implements the full happy-path flow for purchasing Givex e-gift cards
via Chrome DevTools Protocol (CDP) / Selenium.  All selector constants
are defined at module level so they can be patched in tests without
touching the class.
"""

from __future__ import annotations

import json as _json
import logging
import secrets
import time

try:
    from selenium.webdriver.support.ui import Select  # type: ignore[import]
except ImportError:  # pragma: no cover - tests mock _cdp_select_option
    Select = None  # type: ignore[assignment,misc]

from modules.common.exceptions import PageStateError, SelectorTimeoutError

_log = logging.getLogger(__name__)

# ── URL constants ─────────────────────────────────────────────────────────
URL_GEO_CHECK = "https://lumtest.com/myip.json"
URL_BASE      = "https://wwws-usa2.givex.com/cws4.0/lushusa/"
URL_EGIFT     = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/"
URL_CART      = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/shopping-cart.html"
URL_CHECKOUT  = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/checkout.html"
URL_PAYMENT   = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html"

# ── URL fragments used to detect order confirmation ─────────────────────────
URL_CONFIRM_FRAGMENTS = ("/confirmation", "/order-confirmation", "order-confirm")

# ── Navigation ───────────────────────────────────────────────────────────
SEL_COOKIE_ACCEPT = "#button--accept-cookies"
SEL_BUY_EGIFT_BTN = "#cardForeground > div > div.bannerButtons.clearfix > div.bannerBtn.btn1.displaySectionYes > a"

# ── eGift form (Step 1) — URL_EGIFT ─────────────────────────────────────────
SEL_GREETING_MSG           = "#cws_txt_gcMsg"
SEL_AMOUNT_INPUT           = "#cws_txt_gcBuyAmt"
SEL_RECIPIENT_NAME         = "#cws_txt_gcBuyTo"
SEL_RECIPIENT_EMAIL        = "#cws_txt_recipEmail"
SEL_CONFIRM_RECIPIENT_EMAIL = "#cws_txt_confRecipEmail"
SEL_SENDER_NAME            = "#cws_txt_gcBuyFrom"
SEL_ADD_TO_CART            = "#cws_btn_gcBuyAdd > span"
SEL_REVIEW_CHECKOUT        = "#cws_btn_gcBuyCheckout"

# ── Cart & Guest Checkout (Step 2) ───────────────────────────────────────────
SEL_BEGIN_CHECKOUT = "#cws_btn_cartCheckout"
SEL_GUEST_EMAIL    = "#cws_txt_guestEmail"
SEL_GUEST_CONTINUE = "#cws_btn_guestChkout"

# ── Payment / Card fields (Step 4) — URL_PAYMENT ────────────────────────────
SEL_CARD_NAME         = "#cws_txt_ccName"
SEL_CARD_NUMBER       = "#cws_txt_ccNum"
SEL_CARD_EXPIRY_MONTH = "#cws_list_ccExpMon"
SEL_CARD_EXPIRY_YEAR  = "#cws_list_ccExpYr"
SEL_CARD_CVV          = "#cws_txt_ccCvv"

# ── Billing fields (Step 4 — same page as payment) ──────────────────────────
SEL_BILLING_ADDRESS = "#cws_txt_billingAddr1"
SEL_BILLING_COUNTRY = "#cws_list_billingCountry"
SEL_BILLING_STATE   = "#cws_list_billingProvince"
SEL_BILLING_CITY    = "#cws_txt_billingCity"
SEL_BILLING_ZIP     = "#cws_txt_billingPostal"
SEL_BILLING_PHONE   = "#cws_txt_billingPhone"
SEL_COMPLETE_PURCHASE = "#cws_btn_checkoutPay"

# ── Post-submit state detection (Step 5) ─────────────────────────────────────
SEL_CONFIRMATION_EL = ".order-confirmation, .confirmation-message"
SEL_DECLINED_MSG    = ".payment-error, .error-message, div[data-error]"
SEL_UI_LOCK_SPINNER = ".loading-overlay, .spinner, div[aria-busy='true']"
SEL_VBV_IFRAME      = "iframe[src*='3dsecure'], iframe[src*='adyen'], iframe[id*='threeds']"
SEL_VBV_CANCEL_BTN  = "button[id*='cancel'], a[id*='cancel'], button[id*='return'], a[id*='return']"
SEL_POPUP_CLOSE_BTN = "button.modal-close, button[aria-label='Close'], .modal button[type='button']"
SEL_NEUTRAL_DIV     = "body"

_GREETINGS = [
    "Happy gifting!",
    "Enjoy this little treat!",
    "Thinking of you!",
    "With love and best wishes!",
    "Hope this brightens your day!",
]

def _random_greeting() -> str:
    """Return a random greeting message for the eGift form."""
    return secrets.choice(_GREETINGS)

class GivexDriver:
    """Automates the Givex e-gift card purchase flow using CDP/Selenium.

    The driver expects a Selenium ``webdriver`` instance (or compatible mock)
    to be supplied at construction time.  All page interactions are performed
    through the ``_driver`` attribute; no direct import of Selenium is
    required so that unit tests can inject plain mocks.

    Args:
        driver: A Selenium WebDriver instance (or test double).
    """

    def __init__(self, driver: object) -> None:
        self._driver = driver

    # ── Low-level helpers ────────────────────────────────────────────────────

    def find_elements(self, selector: str) -> list:
        """Return all elements matching *selector* (CSS, comma-separated OK).

        Iterates over each comma-separated sub-selector and returns the first
        non-empty match list, falling back to an empty list when none match.

        Args:
            selector: CSS selector string, may contain comma-separated parts.

        Returns:
            List of matching WebElement objects (may be empty).
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

        Args:
            selector: CSS selector to wait for.
            timeout: Maximum seconds to wait (default 10).

        Returns:
            True if the element appeared within *timeout* seconds, False
            otherwise.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.find_elements(selector):
                return True
            time.sleep(0.5)
        return False

    def _cdp_type_field(self, selector: str, value: str) -> None:
        """Clear *selector* element and type *value* into it.

        Args:
            selector: CSS selector for the input/textarea element.
            value: Text to type.

        Raises:
            SelectorTimeoutError: if no matching element is found.
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)
        el = elements[0]
        try:
            el.clear()
        except Exception:  # clear() is best-effort; send_keys still runs
            _log.debug("Element clear() skipped in _cdp_type_field")
        el.send_keys(value)

    def _cdp_select_option(self, selector: str, value: str) -> None:
        """Select the option matching *value* in a ``<select>`` element.

        Args:
            selector: CSS selector for the select element.
            value: The option value to select.

        Raises:
            SelectorTimeoutError: if no matching element is found.
            RuntimeError: if the selenium ``Select`` helper is unavailable.
        """
        if Select is None:
            raise RuntimeError(
                "selenium is not installed; cannot use _cdp_select_option"
            )
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)
        Select(elements[0]).select_by_value(value)

    def bounding_box_click(self, selector: str) -> None:
        """Click the first element matching *selector*.

        Args:
            selector: CSS selector for the element to click.

        Raises:
            SelectorTimeoutError: if no matching element is found.
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)
        elements[0].click()

    # ── Navigation ──────────────────────────────────────────────────────────

    def preflight_geo_check(self) -> None:
        """Navigate to geo-check URL and assert the IP is US-based.

        Raises:
            RuntimeError: if the detected country is not ``"US"``.
        """
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
        """Navigate to the Givex base URL and open the eGift purchase page.

        Accepts the cookie banner if present, then clicks the Buy eGift link,
        and navigates directly to the eGift form page.
        """
        self._driver.get(URL_BASE)
        # Dismiss cookie banner if present (best-effort)
        if self.find_elements(SEL_COOKIE_ACCEPT):
            try:
                self.bounding_box_click(SEL_COOKIE_ACCEPT)
            except Exception as exc:  # cookie banner is best-effort; continue navigation
                _log.debug("Cookie banner click skipped: %s", exc)
        self._wait_for_element(SEL_BUY_EGIFT_BTN, timeout=10)
        self.bounding_box_click(SEL_BUY_EGIFT_BTN)
        self._driver.get(URL_EGIFT)

    # ── eGift form (Step 1) ─────────────────────────────────────────────────

    def fill_egift_form(self, task, billing_profile) -> None:
        """Fill all fields on the eGift purchase form.

        Args:
            task: WorkerTask with ``recipient_email`` and ``amount``.
            billing_profile: BillingProfile with ``first_name`` and
                ``last_name`` (used as recipient/sender name).
        """
        full_name = f"{billing_profile.first_name} {billing_profile.last_name}"
        self._cdp_type_field(SEL_GREETING_MSG, _random_greeting())
        self._cdp_type_field(SEL_AMOUNT_INPUT, str(task.amount))
        self._cdp_type_field(SEL_RECIPIENT_NAME, full_name)
        self._cdp_type_field(SEL_RECIPIENT_EMAIL, task.recipient_email)
        self._cdp_type_field(SEL_CONFIRM_RECIPIENT_EMAIL, task.recipient_email)
        self._cdp_type_field(SEL_SENDER_NAME, full_name)

    def add_to_cart_and_checkout(self) -> None:
        """Click Add-to-Cart, wait for Review & Checkout button, then click it."""
        self.bounding_box_click(SEL_ADD_TO_CART)
        found = self._wait_for_element(SEL_REVIEW_CHECKOUT, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_REVIEW_CHECKOUT, 10)
        self.bounding_box_click(SEL_REVIEW_CHECKOUT)

    # ── Cart & Guest Checkout (Step 2) ───────────────────────────────────────

    def select_guest_checkout(self, guest_email: str) -> None:
        """Click Begin Checkout, then enter guest email and click Continue.

        Args:
            guest_email: Email address to enter in the guest checkout field.

        Raises:
            SelectorTimeoutError: if the Begin Checkout button or guest email
                field never appears.
        """
        found = self._wait_for_element(SEL_BEGIN_CHECKOUT, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_BEGIN_CHECKOUT, 10)
        self.bounding_box_click(SEL_BEGIN_CHECKOUT)

        found = self._wait_for_element(SEL_GUEST_EMAIL, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_GUEST_EMAIL, 10)
        self._cdp_type_field(SEL_GUEST_EMAIL, guest_email)
        self.bounding_box_click(SEL_GUEST_CONTINUE)

    # ── Payment & Billing (Step 4 — same page) ──────────────────────────────

    def fill_payment_and_billing(self, card_info, billing_profile) -> None:
        """Fill both card payment fields and billing address fields.

        Card and billing fields are on the same page (``URL_PAYMENT``).

        Args:
            card_info: CardInfo with ``card_name``, ``card_number``,
                ``exp_month``, ``exp_year``, and ``cvv``.
            billing_profile: BillingProfile with address details.
        """
        # Card section
        self._cdp_type_field(SEL_CARD_NAME, card_info.card_name)
        self._cdp_type_field(SEL_CARD_NUMBER, card_info.card_number)
        self._cdp_select_option(SEL_CARD_EXPIRY_MONTH, card_info.exp_month)
        self._cdp_select_option(SEL_CARD_EXPIRY_YEAR, card_info.exp_year)
        self._cdp_type_field(SEL_CARD_CVV, card_info.cvv)
        # Billing section
        self._cdp_type_field(SEL_BILLING_ADDRESS, billing_profile.address)
        self._cdp_select_option(SEL_BILLING_COUNTRY, billing_profile.country)
        self._cdp_select_option(SEL_BILLING_STATE, billing_profile.state)
        self._cdp_type_field(SEL_BILLING_CITY, billing_profile.city)
        self._cdp_type_field(SEL_BILLING_ZIP, billing_profile.zip_code)
        if billing_profile.phone:
            self._cdp_type_field(SEL_BILLING_PHONE, billing_profile.phone)

    def fill_billing(self, billing_profile) -> None:
        """Backward-compatibility method that fills only billing fields.

        .. deprecated::
            Use ``fill_payment_and_billing(card_info, billing_profile)`` instead.
        """
        self._cdp_type_field(SEL_BILLING_ADDRESS, billing_profile.address)
        self._cdp_select_option(SEL_BILLING_COUNTRY, billing_profile.country)
        self._cdp_select_option(SEL_BILLING_STATE, billing_profile.state)
        self._cdp_type_field(SEL_BILLING_CITY, billing_profile.city)
        self._cdp_type_field(SEL_BILLING_ZIP, billing_profile.zip_code)
        if billing_profile.phone:
            self._cdp_type_field(SEL_BILLING_PHONE, billing_profile.phone)

    def fill_billing_form(self, billing_profile) -> None:
        """Backward-compatibility alias for ``fill_billing``."""
        self.fill_billing(billing_profile)

    def fill_card(self, card_info) -> None:
        """Backward-compatibility stub.

        .. deprecated::
            Card and billing are now on the same page.
            Use ``fill_payment_and_billing(card_info, billing_profile)`` instead.

        Raises:
            NotImplementedError: always — use ``fill_payment_and_billing``.
        """
        raise NotImplementedError(
            "fill_card() is deprecated. "
            "Use fill_payment_and_billing(card_info, billing_profile) instead."
        )

    def submit_purchase(self) -> None:
        """Click the Complete Purchase button."""
        self.bounding_box_click(SEL_COMPLETE_PURCHASE)

    def clear_card_fields(self) -> None:
        """Clear all card form fields (best-effort)."""
        for selector in (
            SEL_CARD_NUMBER,
            SEL_CARD_CVV,
        ):
            elements = self.find_elements(selector)
            if elements:
                try:
                    elements[0].clear()
                except Exception:  # field clear is best-effort
                    _log.debug("Element clear() skipped in clear_card_fields")

    # ── Post-submit state detection (Step 5) ─────────────────────────────────

    def detect_page_state(self) -> str:
        """Inspect the current page and return the FSM state name.

        Detection order:
        1. ``success``   — URL contains a confirmation fragment, OR
                           ``.order-confirmation`` element is present.
        2. ``vbv_3ds``   — A 3-D Secure / Adyen iframe is present.
        3. ``declined``  — A payment-error element is present, OR page text
                           contains "declined" / "transaction failed".
        4. ``ui_lock``   — A loading overlay or spinner is present.
        5. Raises ``PageStateError`` if none of the above matched.

        Returns:
            One of: ``"success"``, ``"vbv_3ds"``, ``"declined"``,
            ``"ui_lock"``.

        Raises:
            PageStateError: if the page state cannot be determined.
        """
        current_url = ""
        try:
            current_url = self._driver.current_url
        except Exception as exc:  # URL unavailable; fall through to element checks
            _log.debug("current_url unavailable: %s", exc)

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
        except Exception as exc:  # body text unavailable; fall through
            _log.debug("Page body text unavailable: %s", exc)

        # 4 — ui_lock
        if self.find_elements(SEL_UI_LOCK_SPINNER):
            return "ui_lock"

        raise PageStateError("unknown")

    # ── Full-cycle orchestrator ───────────────────────────────────────────────

    def run_full_cycle(self, task, billing_profile) -> str:
        """Execute the complete happy-path purchase flow end-to-end.

        Steps:
        1. Geo pre-flight check (``preflight_geo_check``).
        2. Navigate to eGift page (``navigate_to_egift``).
        3. Fill the eGift form (``fill_egift_form``).
        4. Add to cart and click Review & Checkout
           (``add_to_cart_and_checkout``).
        5. Select guest checkout using billing profile email
           (``select_guest_checkout``).
        6. Fill payment and billing fields
           (``fill_payment_and_billing``).
        7. Submit the purchase (``submit_purchase``).

        Args:
            task: WorkerTask with purchase details.
            billing_profile: BillingProfile with address and email.

        Returns:
            The FSM state string returned by ``detect_page_state()``.
        """
        self.preflight_geo_check()
        self.navigate_to_egift()
        self.fill_egift_form(task, billing_profile)
        self.add_to_cart_and_checkout()
        self.select_guest_checkout(billing_profile.email)
        self.fill_payment_and_billing(task.primary_card, billing_profile)
        self.submit_purchase()
        return self.detect_page_state()
