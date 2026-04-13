"""GivexDriver — Selenium/CDP driver thực cho trang Givex USA.

.. note::
    Selectors marked ``[TODO: ...]`` are intentional placeholders that must be
    replaced with real CSS selectors after inspecting the live page.  This file
    is the dev-fork stub; do **not** deploy to production until every TODO
    selector has been resolved via browser DevTools inspection.
"""

import random
import time

# ── URLs ─────────────────────────────────────────────────────────────────────
URL_BASE             = "https://wwws-usa2.givex.com/cws4.0/lushusa/"
URL_EGIFT            = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/"
URL_GEO_CHECK        = "https://lumtest.com/myip.json"
URL_CONFIRM_FRAGMENT = "/confirmation"

# ── Selectors ─────────────────────────────────────────────────────────────────
# Navigation
SEL_COOKIE_ACCEPT   = "#button--accept-cookies"
SEL_BUY_EGIFT_BTN   = "#cardForeground a[href*='Buy-E-gift-Cards']"

# e-Gift form fields (recipient section)
SEL_RECIPIENT_EMAIL = "[TODO: inspect #recipient-email or input[name*='email']]"
SEL_RECIPIENT_NAME  = "[TODO: inspect input[name*='recipient'] or similar]"
SEL_SENDER_NAME     = "[TODO: inspect input[name*='from'] or similar]"
SEL_GREETING_MSG    = "[TODO: inspect textarea[name*='message'] or similar]"
SEL_AMOUNT_INPUT    = "[TODO: inspect input[name*='amount'] or select[name*='amount']]"

# Cart actions — TODO: fill in after page inspection; reserved for add-to-cart
# and review-checkout flows (not yet wired to driver methods)
SEL_ADD_TO_CART     = "[TODO: inspect button[type='submit'] or input[value*='Add to Cart']]"
SEL_REVIEW_CHECKOUT = "[TODO: inspect a[href*='checkout'] or button containing 'Review']"

# Checkout — billing section
SEL_BILLING_FIRST_NAME = "[TODO: inspect input[name*='firstName'] or input[id*='first']"
SEL_BILLING_LAST_NAME  = "[TODO: inspect input[name*='lastName'] or input[id*='last']"
SEL_BILLING_ADDRESS    = "[TODO: inspect input[name*='address'] or input[id*='address']"
SEL_BILLING_CITY       = "[TODO: inspect input[name*='city']]"
SEL_BILLING_STATE      = "[TODO: inspect select[name*='state']]"
SEL_BILLING_ZIP        = "[TODO: inspect input[name*='zip'] or input[name*='postal']"
SEL_BILLING_PHONE      = "[TODO: inspect input[name*='phone'] or input[type='tel']"
SEL_BILLING_EMAIL      = "[TODO: inspect input[type='email'] in billing section]"

# Card fields
SEL_CARD_NUMBER       = "[TODO: inspect input[name*='card'] or input[id*='cardNumber']"
SEL_CARD_EXPIRY_MONTH = "[TODO: inspect select[name*='expMonth'] or similar]"
SEL_CARD_EXPIRY_YEAR  = "[TODO: inspect select[name*='expYear'] or similar]"
SEL_CARD_CVV          = "[TODO: inspect input[name*='cvv'] or input[id*='cvv']"

# Submit
SEL_COMPLETE_PURCHASE = "[TODO: inspect button[type='submit'] containing 'Complete Purchase']"

# Post-submit states
SEL_CONFIRMATION_HEADER = "[TODO: inspect h1 or .confirmation-title on success page]"
SEL_DECLINED_MSG        = "[TODO: inspect .error-message or div containing 'Declined']"
SEL_UI_LOCK_SPINNER     = "[TODO: inspect loading spinner selector]"
SEL_VBV_IFRAME          = "[TODO: inspect iframe[src*='3dsecure'] or iframe[src*='adyen']"
SEL_VBV_CANCEL_BTN      = "[TODO: inspect button/a containing 'Cancel' or 'Return' inside VBV iframe]"
SEL_POPUP_CLOSE_BTN     = "[TODO: inspect button containing 'Close' or 'OK' in error popup]"
SEL_NEUTRAL_DIV         = "body"   # fallback neutral click target for ui_lock

# ── Greeting pool ─────────────────────────────────────────────────────────────
_GREETINGS = [
    "Happy Birthday!", "Best wishes!", "Enjoy your gift!",
    "Thank you for being you", "Thinking of you!",
    "Have a wonderful day!", "You deserve this!",
    "With love and appreciation", "Celebrate you!",
    "Wishing you all the best!",
]


def _random_greeting() -> str:
    """Return a random greeting message from the pool."""
    return random.choice(_GREETINGS)


class GivexDriver:
    """Selenium/CDP driver for the Givex USA e-gift purchase flow."""

    def __init__(self, selenium_driver, persona=None):
        """Initialise the driver wrapper.

        Args:
            selenium_driver: Selenium WebDriver instance attached via CDP
                remote debugging.
            persona: Optional PersonaProfile instance providing ``seed`` and
                typing behaviour parameters.
        """
        self._driver = selenium_driver
        self._persona = persona

    # ── Public API ────────────────────────────────────────────────────────────

    def preflight_geo_check(self) -> bool:
        """Navigate to the geo-check URL and assert the IP is in the US.

        Returns:
            True when the country check passes.

        Raises:
            RuntimeError: if the detected country is not ``"US"``.
        """
        self._driver.get(URL_GEO_CHECK)
        body_text = self._driver.find_element("tag name", "body").text
        import json as _json
        data = _json.loads(body_text)
        country = data.get("country", "")
        if country != "US":
            raise RuntimeError(f"Geo check failed: country={country!r}")
        return True

    def navigate_to_egift(self) -> None:
        """Navigate to the e-gift purchase page and reset browser state."""
        self._driver.get(URL_BASE)
        # Accept cookie banner if present
        try:
            elements = self._driver.find_elements("css selector", SEL_COOKIE_ACCEPT)
            if elements:
                self.bounding_box_click(SEL_COOKIE_ACCEPT)
        except Exception:
            pass
        # Click the Buy e-Gift Cards link
        try:
            self.bounding_box_click(SEL_BUY_EGIFT_BTN)
        except Exception:
            pass
        self._driver.get(URL_EGIFT)
        self._hard_reset_state()

    def fill_recipient(
        self,
        recipient_email: str,
        recipient_name: str,
        sender_name: str,
        greeting: str,
    ) -> None:
        """Fill the recipient section of the e-gift form.

        Args:
            recipient_email: Email address of the gift recipient.
            recipient_name: Display name of the gift recipient.
            sender_name: Display name of the gift sender.
            greeting: Personal greeting message.
        """
        self._cdp_type_field(SEL_RECIPIENT_EMAIL, recipient_email)
        self._cdp_type_field(SEL_RECIPIENT_NAME, recipient_name)
        self._cdp_type_field(SEL_SENDER_NAME, sender_name)
        self._cdp_type_field(SEL_GREETING_MSG, greeting)

    def fill_billing(self, billing_profile) -> None:
        """Fill all billing form fields.

        Args:
            billing_profile: A ``BillingProfile`` instance with address and
                contact information.
        """
        self._cdp_type_field(SEL_BILLING_FIRST_NAME, billing_profile.first_name)
        self._cdp_type_field(SEL_BILLING_LAST_NAME, billing_profile.last_name)
        self._cdp_type_field(SEL_BILLING_ADDRESS, billing_profile.address)
        self._cdp_type_field(SEL_BILLING_CITY, billing_profile.city)
        self._cdp_type_field(SEL_BILLING_STATE, billing_profile.state)
        self._cdp_type_field(SEL_BILLING_ZIP, billing_profile.zip_code)
        if billing_profile.phone:
            self._cdp_type_field(SEL_BILLING_PHONE, billing_profile.phone)
        if billing_profile.email:
            self._cdp_type_field(SEL_BILLING_EMAIL, billing_profile.email)

    def fill_card(self, card_info) -> None:
        """Fill the card payment fields using a 4×4 grouped typing pattern.

        Card number is entered in four groups of four digits with a random
        inter-group pause (0.6–1.8 s) to mimic natural card-entry behaviour.
        After filling the CVV a hesitation pause (3–5 s) with hover and light
        scroll is performed before submitting.

        Args:
            card_info: A ``CardInfo`` instance with card number, expiry, and CVV.
        """
        card_number = card_info.card_number.replace(" ", "").replace("-", "")
        groups = [card_number[i:i + 4] for i in range(0, len(card_number), 4)]

        self.bounding_box_click(SEL_CARD_NUMBER)
        self._send_ctrl_a_backspace()

        for i, group in enumerate(groups):
            for char in group:
                self._driver.execute_cdp_cmd(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "char",
                        "text": char,
                    },
                )
            if i < len(groups) - 1:
                time.sleep(random.uniform(0.6, 1.8))

        self._cdp_type_field(SEL_CARD_EXPIRY_MONTH, card_info.exp_month, clear_first=True)
        self._cdp_type_field(SEL_CARD_EXPIRY_YEAR, card_info.exp_year, clear_first=True)
        self._cdp_type_field(SEL_CARD_CVV, card_info.cvv, clear_first=True)

        # Hesitation before clicking Complete Purchase
        time.sleep(random.uniform(3, 5))
        try:
            elements = self._driver.find_elements("css selector", SEL_COMPLETE_PURCHASE)
            if elements:
                el = elements[0]
                rect = self._driver.execute_script(
                    "var r = arguments[0].getBoundingClientRect();"
                    "return {x: r.x, y: r.y, width: r.width, height: r.height};",
                    el,
                )
                cx = rect["x"] + rect["width"] / 2
                cy = rect["y"] + rect["height"] / 2
                # Hover over button
                self._driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseMoved",
                        "x": cx + random.uniform(-5, 5),
                        "y": cy + random.uniform(-2, 2),
                        "button": "none",
                        "clickCount": 0,
                    },
                )
                # Light scroll
                self._driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": cx,
                        "y": cy,
                        "deltaX": 0,
                        "deltaY": random.uniform(-20, 20),
                    },
                )
        except Exception:
            pass

    def clear_card_fields(self) -> None:
        """Clear card number field content without reloading the page."""
        self.bounding_box_click(SEL_CARD_NUMBER)
        self._send_ctrl_a_backspace()

    def submit_purchase(self) -> None:
        """Click the Complete Purchase button with a small random offset."""
        self.bounding_box_click(SEL_COMPLETE_PURCHASE, x_offset_range=15, y_offset_range=5)

    def detect_page_state(self) -> str:
        """Detect the current purchase flow page state.

        Returns:
            ``"success"`` — confirmation page detected.
            ``"vbv_3ds"`` — 3-D Secure / VBV iframe present.
            ``"declined"`` — declined message visible.
            ``"ui_lock"`` — loading spinner present.
            ``"unknown"`` — none of the above matched.
        """
        current_url = self._driver.current_url
        if URL_CONFIRM_FRAGMENT in current_url:
            return "success"

        try:
            vbv_iframes = self._driver.find_elements("css selector", SEL_VBV_IFRAME)
            if vbv_iframes:
                return "vbv_3ds"
        except Exception:
            pass

        try:
            declined = self._driver.find_elements("css selector", SEL_DECLINED_MSG)
            if declined:
                return "declined"
        except Exception:
            pass

        try:
            spinners = self._driver.find_elements("css selector", SEL_UI_LOCK_SPINNER)
            if spinners:
                return "ui_lock"
        except Exception:
            pass

        return "unknown"

    def handle_vbv(self) -> None:
        """Handle a 3-D Secure / VBV iframe by cancelling it.

        Waits for the iframe to load, switches context into it, computes the
        absolute on-screen coordinates of the Cancel button and dispatches a
        CDP mouse-click, then switches back to the default context.  If an
        error popup appears it is closed automatically.
        """
        time.sleep(random.uniform(8, 12))
        try:
            iframes = self._driver.find_elements("css selector", SEL_VBV_IFRAME)
            if not iframes:
                return
            iframe = iframes[0]
            iframe_rect = self._driver.execute_script(
                "var r = arguments[0].getBoundingClientRect();"
                "return {x: r.x, y: r.y, width: r.width, height: r.height};",
                iframe,
            )
            self._driver.switch_to.frame(iframe)
            try:
                cancel_elements = self._driver.find_elements(
                    "css selector", SEL_VBV_CANCEL_BTN
                )
                if cancel_elements:
                    el = cancel_elements[0]
                    el_rect = self._driver.execute_script(
                        "var r = arguments[0].getBoundingClientRect();"
                        "return {x: r.x, y: r.y, width: r.width, height: r.height};",
                        el,
                    )
                    abs_x = (
                        iframe_rect["x"]
                        + el_rect["x"]
                        + el_rect["width"] / 2
                        + random.uniform(-5, 5)
                    )
                    abs_y = (
                        iframe_rect["y"]
                        + el_rect["y"]
                        + el_rect["height"] / 2
                        + random.uniform(-3, 3)
                    )
                    self._driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mousePressed",
                            "x": abs_x,
                            "y": abs_y,
                            "button": "left",
                            "clickCount": 1,
                        },
                    )
                    self._driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseReleased",
                            "x": abs_x,
                            "y": abs_y,
                            "button": "left",
                            "clickCount": 1,
                        },
                    )
            finally:
                self._driver.switch_to.default_content()

            # Handle "Something went wrong" popup
            try:
                popup_btns = self._driver.find_elements(
                    "css selector", SEL_POPUP_CLOSE_BTN
                )
                if popup_btns:
                    self.bounding_box_click(SEL_POPUP_CLOSE_BTN)
            except Exception:
                pass
        except Exception:
            self._driver.switch_to.default_content()

    # ── Helper methods ────────────────────────────────────────────────────────

    def _cdp_type_field(
        self, selector: str, text: str, clear_first: bool = True
    ) -> None:
        """Click a field and type text into it using CDP key events.

        Args:
            selector: CSS selector for the target input element.
            text: The text to type.
            clear_first: When True, select-all then backspace before typing.
        """
        self.bounding_box_click(selector)
        if clear_first:
            self._send_ctrl_a_backspace()
        seed = getattr(self._persona, "seed", 42) if self._persona else 42
        rng = random.Random(seed)
        for char in text:
            self._driver.execute_cdp_cmd(
                "Input.dispatchKeyEvent",
                {
                    "type": "char",
                    "text": char,
                },
            )
            time.sleep(rng.uniform(0.05, 0.18))

    def bounding_box_click(
        self,
        selector: str,
        x_offset_range: int = 15,
        y_offset_range: int = 5,
    ) -> None:
        """Click an element at a random offset from its centre using CDP.

        Args:
            selector: CSS selector for the target element.
            x_offset_range: Maximum pixel deviation in the X axis.
            y_offset_range: Maximum pixel deviation in the Y axis.
        """
        el = self._driver.find_element("css selector", selector)
        rect = self._driver.execute_script(
            "var r = arguments[0].getBoundingClientRect();"
            "return {x: r.x, y: r.y, width: r.width, height: r.height};",
            el,
        )
        cx = rect["x"] + rect["width"] / 2 + random.uniform(
            -x_offset_range, x_offset_range
        )
        cy = rect["y"] + rect["height"] / 2 + random.uniform(
            -y_offset_range, y_offset_range
        )
        self._driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": cx,
                "y": cy,
                "button": "left",
                "clickCount": 1,
            },
        )
        self._driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": cx,
                "y": cy,
                "button": "left",
                "clickCount": 1,
            },
        )

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
            "document.cookie.split(';').forEach(function(c) {"
            "  document.cookie = c.replace(/^ +/, '').replace(/=.*/, '=;expires='"
            "    + new Date().toUTCString() + ';path=/');"
            "});"
        )
        self._driver.execute_script("localStorage.clear();")
        self._driver.execute_script("sessionStorage.clear();")

    def _send_ctrl_a_backspace(self) -> None:
        """Send Ctrl+A followed by Backspace via CDP to clear a field."""
        self._driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "a",
                "code": "KeyA",
                "modifiers": 2,  # Ctrl
            },
        )
        self._driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "a",
                "code": "KeyA",
                "modifiers": 2,
            },
        )
        self._driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Backspace",
                "code": "Backspace",
                "modifiers": 0,
            },
        )
        self._driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Backspace",
                "code": "Backspace",
                "modifiers": 0,
            },
        )
