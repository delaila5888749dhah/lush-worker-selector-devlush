"""GivexDriver — Givex e-gift card purchase automation driver.

Implements the full happy-path flow for purchasing Givex e-gift cards
via Chrome DevTools Protocol (CDP) / Selenium.  All selector constants
are defined at module level so they can be patched in tests without
touching the class.
"""

from __future__ import annotations

import json as _json
import logging
import os
import secrets
import time
import datetime
import ipaddress
import urllib.request
import urllib.error

try:
    from selenium.webdriver.support.ui import Select  # type: ignore[import]
except ImportError:  # pragma: no cover - tests mock _cdp_select_option
    Select = None  # type: ignore[assignment,misc]

try:
    from selenium.webdriver.common.action_chains import ActionChains as _ActionChains  # type: ignore[import]
except ImportError:  # pragma: no cover - only used as fallback in _ghost_move_to
    _ActionChains = None  # type: ignore[assignment,misc]

try:
    from modules.cdp.mouse import GhostCursor as _GhostCursor
    from modules.cdp.keyboard import type_value as _type_value
except ImportError:  # pragma: no cover - defensive; mouse.py/keyboard.py always present
    _GhostCursor = None  # type: ignore[assignment,misc]
    _type_value = None  # type: ignore[assignment,misc]

from modules.common.exceptions import PageStateError, SelectorTimeoutError
from modules.common.sanitize import sanitize_error as _sanitize_error  # noqa: F401 — re-exported so callers can do `from modules.cdp.driver import _sanitize_error`

try:
    from zoneinfo import ZoneInfo as _ZoneInfo  # type: ignore[import]
    from zoneinfo import ZoneInfoNotFoundError as _ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    _ZoneInfo = None  # type: ignore[assignment,misc]
    _ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

try:
    from modules.delay.biometrics import BiometricProfile as _BiometricProfile  # type: ignore
    from modules.delay.temporal import TemporalModel as _TemporalModel  # type: ignore
    from modules.delay.state import BehaviorStateMachine as _BehaviorStateMachine  # type: ignore
    from modules.delay.engine import DelayEngine as _DelayEngine  # type: ignore
except ImportError:
    _BiometricProfile = _TemporalModel = None
    _BehaviorStateMachine = _DelayEngine = None

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
SEL_GUEST_HEADING  = "#guestHeading"
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


def _lookup_maxmind_utc_offset(ip_addr: str) -> int | None:
    """Look up UTC offset for an IP using MaxMind GeoLite2-City.mmdb."""
    if _ZoneInfo is None:
        return None
    mmdb_path = os.environ.get("GEOIP_DB_PATH", "data/GeoLite2-City.mmdb")
    if not os.path.exists(mmdb_path):
        return None
    try:
        import geoip2.database  # type: ignore
    except ImportError:
        return None
    try:
        with geoip2.database.Reader(mmdb_path) as reader:
            record = reader.city(ip_addr)
            tz_name = record.location.time_zone
            if tz_name:
                tz_info = _ZoneInfo(tz_name)
                now = datetime.datetime.now(tz_info)
                offset = now.utcoffset()
                if offset is None:
                    return None
                return int(offset.total_seconds() // 3600)
    except Exception as exc:  # pylint: disable=broad-except
        _log.debug("MaxMind lookup failed for %s: %s", ip_addr, exc)
    return None


def _get_current_ip_best_effort() -> str | None:
    """Return current public IP using a short best-effort ipify request."""
    try:
        with urllib.request.urlopen(  # nosec B310
            "https://api.ipify.org", timeout=3,
        ) as response:
            ip_value = response.read().decode("utf-8").strip()
            if not ip_value:
                return None
            ipaddress.ip_address(ip_value)
            return ip_value
    except (urllib.error.URLError, TimeoutError,
            OSError, ValueError, UnicodeDecodeError) as exc:
        _log.debug("Public IP lookup failed: %s", exc)
        return None

class GivexDriver:
    """Automates the Givex e-gift card purchase flow using CDP/Selenium.

    The driver expects a Selenium ``webdriver`` instance (or compatible mock)
    to be supplied at construction time.  All page interactions are performed
    through the ``_driver`` attribute; no direct import of Selenium is
    required so that unit tests can inject plain mocks.

    Args:
        driver: A Selenium WebDriver instance (or test double).
        persona: Optional behavior profile; ``None`` preserves legacy mode.
        strict: When ``True`` (default), CDP dispatch failures raise instead of
            silently falling back.
    """

    def __init__(self, driver: object, persona=None, *, strict: bool = True) -> None:
        """Initialize a Givex driver wrapper.

        Args:
            driver: Selenium WebDriver instance (or compatible mock).
            persona: Optional behavior profile.
            strict: Defaults to ``True`` so CDP dispatch failures raise instead
                of silently falling back.
        """
        self._driver = driver
        self._persona = persona
        self._strict = strict
        self._rnd = persona._rnd if persona is not None else None
        self._utc_offset_hours: int = 0
        if persona is not None and _BehaviorStateMachine is not None:
            self._sm = _BehaviorStateMachine()
            self._engine = _DelayEngine(persona, self._sm)
            self._temporal = _TemporalModel(persona)
            self._bio = _BiometricProfile(persona)
        else:
            self._sm, self._engine = None, None
            self._temporal, self._bio = None, None
        self._cursor = (
            _GhostCursor(driver, self._rnd)
            if (_GhostCursor is not None and self._rnd is not None)
            else None
        )

    # ── Low-level helpers ────────────────────────────────────────────────────

    def _get_rng(self):
        """Return the persona RNG or a SystemRandom fallback."""
        if self._rnd is not None:
            return self._rnd
        import random as _random  # noqa: PLC0415
        return _random.SystemRandom()

    def set_proxy_utc_offset(self, utc_offset_hours: int) -> None:
        """Set UTC offset for temporal model (injected by orchestrator after geo-check)."""
        self._utc_offset_hours = utc_offset_hours

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
            elements = self._driver.find_elements("css selector", part)
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

    def _wait_for_url(self, url_fragment: str, timeout: int = 15) -> None:
        """Poll until the current URL contains *url_fragment* or *timeout* expires.

        Used after navigation-triggering actions (button clicks) to confirm
        the browser has reached the expected page before interacting with
        page-specific selectors.

        Args:
            url_fragment: Substring expected in the current URL.
            timeout: Maximum seconds to wait (default 15).

        Raises:
            PageStateError: if the URL does not contain *url_fragment*
                within *timeout* seconds.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = ""
            try:
                current = self._driver.current_url
            except Exception:  # URL briefly unavailable during page transition
                _log.debug("URL check deferred: page transition in progress")
            if url_fragment in current:
                return
            time.sleep(0.5)
        raise PageStateError(f"url_wait:{url_fragment}")

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

    def _realistic_type_field(
            self, sel, val, *, use_burst=False,
            field_kind="text", typo_rate=None,
    ):
        els = self.find_elements(sel)
        if not els: raise SelectorTimeoutError(sel, 0)  # noqa: E701
        if _type_value is None:
            if self._strict:
                _log.warning("_realistic_type_field: keyboard unavailable (strict)")
            self._cdp_type_field(sel, val)
            return
        typo_prob = self._persona.get_typo_probability() if self._persona else 0.0
        if self._persona and self._temporal:
            typo_prob += self._temporal.get_night_typo_increase(self._utc_offset_hours)
        if typo_rate is not None:
            typo_prob = typo_rate
        dl = (self._bio.generate_4x4_pattern() if self._bio and use_burst and len(val) >= 16 else self._bio.generate_burst_pattern(len(val)) if self._bio else None)
        _type_value(
            self._driver, els[0], val, self._get_rng(),
            typo_rate=typo_prob, delays=dl, strict=self._strict,
            field_kind=field_kind, engine=self._engine,
        )

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

    def _smooth_scroll_to(self, selector: str) -> None:
        """Scroll an element into view with a smooth pass and micro-correction."""
        elements = self.find_elements(selector)
        if not elements:
            return
        try:
            self._driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                elements[0],
            )
            if self._cursor is not None:  # Prefer CDP wheel for micro-correction.
                self._cursor.scroll_wheel(-8.0, steps=2)
            else:
                self._driver.execute_script("window.scrollBy(0, -8);")
        except Exception:
            _log.debug("_smooth_scroll_to: execute_script skipped")
        delay = self._persona.get_click_delay() if self._persona is not None else 0.15
        time.sleep(delay)

    def _ghost_move_to(self, selector: str) -> None:
        """Move mouse to the target element via CDP mouseMoved events.

        Uses ``GhostCursor`` (``modules.cdp.mouse``) as the primary
        dispatch path.  Falls back to ActionChains-based movement only
        when ``GhostCursor`` is not available (e.g. module import failed).
        """
        elements = self.find_elements(selector)
        if not elements:
            return
        try:
            rect = self._driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {left:r.left,top:r.top,width:r.width,height:r.height};",
                elements[0],
            )
            if not rect:
                return
            target_x = rect["left"] + rect["width"] / 2
            target_y = rect["top"] + rect["height"] / 2
        except Exception:
            _log.debug("_ghost_move_to: getBoundingClientRect skipped")
            return

        click_delay = self._persona.get_click_delay() if self._persona is not None else 0.05

        if self._cursor is not None:
            self._cursor.move_to(target_x, target_y, click_delay=click_delay)
            return

        # Fallback: ActionChains-based movement when GhostCursor is unavailable.
        rnd = self._get_rng()
        n_points = rnd.randint(4, 8)
        points = []
        for i in range(1, n_points + 1):
            t = i / (n_points + 1)
            cx = target_x * t + rnd.uniform(-30, 30)
            cy = target_y * t + rnd.uniform(-20, 20)
            points.append((cx, cy))
        points.append((target_x, target_y))

        try:
            if _ActionChains is None:
                return
            actions = _ActionChains(self._driver)
            prev_x, prev_y = 0.0, 0.0
            for px, py in points:
                dx = px - prev_x
                dy = py - prev_y
                actions.move_by_offset(int(dx), int(dy))
                prev_x, prev_y = px, py
            actions.perform()
        except Exception:
            _log.debug("_ghost_move_to: ActionChains fallback failed", exc_info=True)
            return

        time.sleep(click_delay * len(points))

    def bounding_box_click(self, selector: str) -> None:
        """Click using randomized bounding-box coordinates, with safe fallbacks.

        Strict mode (the default) suppresses the plain ``.click()`` fallback
        **only** when CDP dispatch itself fails — not when the element rect or
        the randomness helper is unavailable.  In those cases a WARNING is
        emitted so the condition is never silent, and a plain ``.click()``
        executes as a safe fallback regardless of strict mode.

        Args:
            selector: CSS selector for the element to click.

        Raises:
            SelectorTimeoutError: if no matching element is found.
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)

        self._ghost_move_to(selector)

        try:
            rect = self._driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {left:r.left,top:r.top,width:r.width,height:r.height};",
                elements[0],
            )
        except Exception:
            _log.warning(
                "bounding_box_click: getBoundingClientRect raised for selector %r;"
                " falling back to plain click",
                selector,
                exc_info=True,
            )
            elements[0].click()
            return

        if not rect:
            _log.warning(
                "bounding_box_click: rect is falsy for selector %r;"
                " falling back to plain click",
                selector,
            )
            elements[0].click()
            return

        if self._rnd is None:
            _log.warning(
                "bounding_box_click: rnd unavailable for selector %r;"
                " falling back to plain click",
                selector,
            )
            elements[0].click()
            return

        rnd = self._rnd
        night_factor = 1.0
        if self._temporal is not None:
            try:
                if self._temporal.get_time_state(self._utc_offset_hours) == "NIGHT":
                    night_factor = 1.0 + getattr(self._persona, "night_penalty_factor", 0.0)
            except Exception:  # pylint: disable=broad-except
                _log.debug(
                    "bounding_box_click: unable to read temporal state;"
                    " using default night_factor",
                )
        offset_x = rnd.uniform(-15, 15) * night_factor
        offset_y = rnd.uniform(-5, 5) * night_factor
        offset_x = max(-15.0, min(15.0, offset_x))
        offset_y = max(-5.0, min(5.0, offset_y))
        center_x = rect["left"] + rect["width"] / 2
        center_y = rect["top"] + rect["height"] / 2
        abs_x = max(rect["left"], min(center_x + offset_x, rect["left"] + rect["width"]))
        abs_y = max(rect["top"], min(center_y + offset_y, rect["top"] + rect["height"]))
        try:
            for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
                self._driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": abs_x,
                        "y": abs_y,
                        "button": "left",
                        "clickCount": 1,
                    },
                )
            return
        except Exception:  # pylint: disable=broad-except
            if self._strict:
                _log.warning("bounding_box_click: CDP failed (strict mode)")
                return
            _log.debug("bounding_box_click: CDP failed, .click() fallback", exc_info=True)
        elements[0].click()

    def cdp_click_absolute(self, x: float, y: float) -> None:
        """Send an absolute-coordinate CDP click."""
        for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            self._driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    def _hesitate_before_submit(self) -> None:
        if self._engine is not None and not self._engine.is_delay_permitted():
            return
        raw = self._persona.get_hesitation_delay() if self._persona else self._get_rng().uniform(3.0, 5.0)
        delay = max(3.0, min(raw, 5.0))
        rnd = self._get_rng()
        elements = self.find_elements(SEL_COMPLETE_PURCHASE)
        rect = None
        if elements:
            try:
                rect = self._driver.execute_script(
                    "var r=arguments[0].getBoundingClientRect();"
                    "return {left:r.left,top:r.top,width:r.width,height:r.height};",
                    elements[0])
            except Exception:  # pylint: disable=broad-except
                _log.debug("_hesitate_before_submit: rect skipped")
        if not rect:
            time.sleep(delay)
            return
        slot = delay / 4.0
        for i in range(4):
            t0 = time.monotonic()
            try:
                if i % 2 == 0 and self._cursor:
                    self._cursor.scroll_wheel(rnd.uniform(-25, 30) * (1 if i == 0 else -1), steps=2)
                elif self._cursor:
                    cx = rect["left"] + rect["width"] / 2 + rnd.uniform(-20, 20)
                    self._cursor.move_to(cx, rect["top"] + rect["height"] / 2 + rnd.uniform(-8, 8))
            except Exception:  # pylint: disable=broad-except
                _log.debug("_hesitate_before_submit: phase %d skipped", i, exc_info=True)
            r = max(0.0, slot - (time.monotonic() - t0))
            if r > 0:
                time.sleep(r)

    # ── Navigation ──────────────────────────────────────────────────────────

    def preflight_geo_check(self) -> str:
        """Navigate to geo-check URL and assert the IP is US-based.

        When the primary geo-check API fails, falls back to MaxMind GeoLite2
        for UTC-offset detection.  If the GeoLite2 database is unavailable,
        malformed, or any other error occurs during the fallback, the offset
        defaults to 0 (UTC) and the method returns ``"UNKNOWN"`` rather than
        raising so that the calling flow can continue safely.

        Returns:
            ``"US"`` when the primary API confirms a US IP, or ``"UNKNOWN"``
            when the API is unavailable and the fallback is used.

        Raises:
            RuntimeError: if the detected country is not ``"US"``.
        """
        self._driver.get(URL_GEO_CHECK)
        try:
            body = self._driver.find_element("tag name", "body").text
            data = _json.loads(body)
            country = data.get("country", "")
            utc_offset = data.get("utc_offset", 0)
            self.set_proxy_utc_offset(int(utc_offset) if utc_offset is not None else 0)
        except Exception as exc:
            _log.warning(
                "preflight_geo_check: API failed, trying MaxMind fallback: %s",
                exc,
            )
            utc_offset = 0
            try:
                detected_ip = _get_current_ip_best_effort()
                if detected_ip:
                    detected_offset = _lookup_maxmind_utc_offset(detected_ip)
                    if detected_offset is None:
                        _log.warning(
                            "preflight_geo_check: MaxMind fallback "
                            "unavailable for IP %s; using UTC offset 0",
                            detected_ip,
                        )
                    utc_offset = detected_offset or 0
                else:
                    _log.warning(
                        "preflight_geo_check: unable to detect "
                        "public IP; using UTC offset 0",
                    )
            except Exception as fallback_exc:  # pylint: disable=broad-except
                _log.warning(
                    "preflight_geo_check: MaxMind fallback raised unexpectedly: %s;"
                    " using UTC offset 0",
                    fallback_exc,
                )
            self.set_proxy_utc_offset(utc_offset)
            return "UNKNOWN"
        if country != "US":
            raise RuntimeError(
                f"Geo-check failed: expected country 'US', got {country!r}"
            )
        return country

    def _clear_browser_state(self) -> None:
        """Clear localStorage, sessionStorage, and cookies (Blueprint §3 Hard-Reset)."""
        try:
            self._driver.execute_script(
                "window.localStorage.clear(); "
                "window.sessionStorage.clear();"
            )
        except Exception:
            _log.debug("_clear_browser_state: script clear skipped", exc_info=True)
        try:
            self._driver.delete_all_cookies()
        except Exception:
            _log.debug(
                "_clear_browser_state: delete_all_cookies skipped",
                exc_info=True,
            )
        _log.debug("_clear_browser_state: browser state cleared for new cycle")

    def navigate_to_egift(self) -> None:
        """Navigate to the Givex base URL and open the eGift purchase page.

        Accepts the cookie banner if present, then clicks the Buy eGift link,
        and navigates directly to the eGift form page.
        """
        self._clear_browser_state()
        self._driver.get(URL_BASE)
        # Dismiss cookie banner if present (best-effort)
        if self.find_elements(SEL_COOKIE_ACCEPT):
            try:
                self.bounding_box_click(SEL_COOKIE_ACCEPT)
            except Exception as exc:  # cookie banner is best-effort; continue navigation
                _log.debug("Cookie banner click skipped: %s", exc)
        self._wait_for_element(SEL_BUY_EGIFT_BTN, timeout=10)
        self.bounding_box_click(SEL_BUY_EGIFT_BTN)
        self._wait_for_url(URL_EGIFT, timeout=15)
        self._clear_browser_state()

    # ── eGift form (Step 1) ─────────────────────────────────────────────────

    def fill_egift_form(self, task, billing_profile) -> None:
        """Fill all fields on the eGift purchase form.

        Args:
            task: WorkerTask with ``recipient_email`` and ``amount``.
            billing_profile: BillingProfile with ``first_name`` and
                ``last_name`` (used as recipient/sender name).
        """
        if self._sm is not None:
            self._sm.transition("FILLING_FORM")
        self._smooth_scroll_to(SEL_GREETING_MSG)
        full_name = f"{billing_profile.first_name} {billing_profile.last_name}"
        self._realistic_type_field(
            SEL_GREETING_MSG, _random_greeting(), field_kind="text",
        )
        self._realistic_type_field(
            SEL_AMOUNT_INPUT, str(task.amount),
            field_kind="amount", typo_rate=0.0,
        )
        self._realistic_type_field(
            SEL_RECIPIENT_NAME, full_name, field_kind="name",
        )
        self._realistic_type_field(
            SEL_RECIPIENT_EMAIL, task.recipient_email, field_kind="text",
        )
        self._realistic_type_field(
            SEL_CONFIRM_RECIPIENT_EMAIL, task.recipient_email,
            field_kind="text",
        )
        self._realistic_type_field(
            SEL_SENDER_NAME, full_name, field_kind="name",
        )

    def add_to_cart_and_checkout(self) -> None:
        """Click Add-to-Cart, wait for Review & Checkout button, then click it.

        After clicking Review & Checkout, waits for the browser to reach
        the cart page (``URL_CART``) before returning.
        """
        self.bounding_box_click(SEL_ADD_TO_CART)
        found = self._wait_for_element(SEL_REVIEW_CHECKOUT, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_REVIEW_CHECKOUT, 10)
        self.bounding_box_click(SEL_REVIEW_CHECKOUT)
        self._wait_for_url(URL_CART, timeout=15)

    # ── Cart & Guest Checkout (Step 2) ───────────────────────────────────────

    def select_guest_checkout(self, guest_email: str) -> None:
        """Click Begin Checkout, expand guest heading, enter email, and continue.

        Steps:
        1. Wait for and click Begin Checkout on the cart page.
        2. Wait for the checkout page (``URL_CHECKOUT``).
        3. Click the guest heading (``#guestHeading``) to expand the
           guest checkout section.
        4. Enter *guest_email* and click Continue.
        5. Wait for the payment page (``URL_PAYMENT``).

        Args:
            guest_email: Email address to enter in the guest checkout field.

        Raises:
            SelectorTimeoutError: if a required element never appears.
            PageStateError: if a required page URL is not reached.
        """
        found = self._wait_for_element(SEL_BEGIN_CHECKOUT, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_BEGIN_CHECKOUT, 10)
        self.bounding_box_click(SEL_BEGIN_CHECKOUT)
        self._wait_for_url(URL_CHECKOUT, timeout=15)

        # Expand the guest checkout section
        found = self._wait_for_element(SEL_GUEST_HEADING, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_GUEST_HEADING, 10)
        self.bounding_box_click(SEL_GUEST_HEADING)

        found = self._wait_for_element(SEL_GUEST_EMAIL, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_GUEST_EMAIL, 10)
        self._cdp_type_field(SEL_GUEST_EMAIL, guest_email)
        self.bounding_box_click(SEL_GUEST_CONTINUE)
        self._wait_for_url(URL_PAYMENT, timeout=15)

    # ── Payment & Billing (Step 4 — same page) ──────────────────────────────

    def fill_payment_and_billing(self, card_info, billing_profile) -> None:
        """Fill both card payment fields and billing address fields.

        Card and billing fields are on the same page (``URL_PAYMENT``).

        Args:
            card_info: CardInfo with ``card_name``, ``card_number``,
                ``exp_month``, ``exp_year``, and ``cvv``.
            billing_profile: BillingProfile with address details.
        """
        if self._sm is not None:
            self._sm.transition("PAYMENT")
        self._realistic_type_field(SEL_CARD_NAME, card_info.card_name, field_kind="name")
        self._realistic_type_field(SEL_CARD_NUMBER, card_info.card_number, use_burst=True, field_kind="card_number")
        self._cdp_select_option(SEL_CARD_EXPIRY_MONTH, card_info.exp_month)
        self._cdp_select_option(SEL_CARD_EXPIRY_YEAR, card_info.exp_year)
        self._realistic_type_field(SEL_CARD_CVV, card_info.cvv, field_kind="cvv")
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
        """Hesitate 3-5s then click the Complete Purchase button (Blueprint §5)."""
        self._hesitate_before_submit()
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
        current_url = self._driver.current_url

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
        page_text = self._driver.find_element("tag name", "body").text.lower()
        if "declined" in page_text or "transaction failed" in page_text:
            return "declined"

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
        if billing_profile.email is None:
            raise ValueError(
                "billing_profile.email must not be None for guest checkout"
            )
        if self._persona is not None:
            _log.debug("run_full_cycle: persona_type=%s", self._persona.persona_type)
        self.preflight_geo_check()
        self.navigate_to_egift()
        self.fill_egift_form(task, billing_profile)
        self.add_to_cart_and_checkout()
        self.select_guest_checkout(billing_profile.email)
        self.fill_payment_and_billing(task.primary_card, billing_profile)
        self.submit_purchase()
        return self.detect_page_state()
