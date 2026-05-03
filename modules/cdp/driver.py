"""GivexDriver — Givex e-gift card purchase automation driver.

Implements the full happy-path flow for purchasing Givex e-gift cards
via Chrome DevTools Protocol (CDP) / Selenium.  All selector constants
are defined at module level so they can be patched in tests without
touching the class.
"""

from __future__ import annotations

import json as _json
import logging
import math
import os
import random as _random
import secrets
import socket
import threading
import time
import datetime
import decimal
import enum
import importlib
import ipaddress
import re
import urllib.parse
import urllib.request
import urllib.error
import warnings
from typing import NoReturn

try:
    from selenium.webdriver.common.action_chains import ActionChains as _ActionChains  # type: ignore[import]
    from selenium.webdriver.common.by import By  # type: ignore[import]
    from selenium.webdriver.support.ui import WebDriverWait  # type: ignore[import]
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore[import]
    from selenium.common.exceptions import TimeoutException  # type: ignore[import]
    from selenium.common.exceptions import WebDriverException  # type: ignore[import]
    from selenium.common.exceptions import NoSuchElementException  # type: ignore[import]
    from selenium.common.exceptions import StaleElementReferenceException  # type: ignore[import]
except ImportError:  # pragma: no cover - selenium always present in prod
    _ActionChains = By = WebDriverWait = EC = None  # type: ignore[assignment,misc]
    TimeoutException = Exception  # type: ignore[assignment,misc]
    WebDriverException = Exception  # type: ignore[assignment,misc]
    NoSuchElementException = Exception  # type: ignore[assignment,misc]
    StaleElementReferenceException = Exception  # type: ignore[assignment,misc]

try:
    from modules.cdp.mouse import GhostCursor as _GhostCursor
    from modules.cdp.keyboard import dispatch_key as _dispatch_key
    from modules.cdp.keyboard import type_value as _type_value
except ImportError:  # pragma: no cover - defensive; mouse.py/keyboard.py always present
    _GhostCursor = None  # type: ignore[assignment,misc]
    _dispatch_key = None  # type: ignore[assignment,misc]
    _type_value = None  # type: ignore[assignment,misc]

from modules.common.exceptions import (
    CDPClickError,
    CDPCommandError,
    CDPError,
    PageStateError,
    SelectorTimeoutError,
    SessionFlaggedError,
)
from modules.common.sanitize import sanitize_error as _sanitize_error  # INV-PII-UNIFIED-01


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
    from modules.delay.state import get_current_sm as _get_current_sm  # type: ignore
    from modules.delay.engine import DelayEngine as _DelayEngine  # type: ignore
    from modules.delay.config import MAX_STEP_DELAY as _MAX_STEP_DELAY  # type: ignore
except ImportError:
    _BiometricProfile = _TemporalModel = None
    _BehaviorStateMachine = _DelayEngine = None
    _MAX_STEP_DELAY = 7.0
    _get_current_sm = None  # type: ignore[assignment]

_GIVEX_REVIEW_CHECKOUT_POLL_DEFAULT_S = 18.0
_GIVEX_CART_STATE_POLL_DEFAULT_S = 18.0
_GIVEX_FANCYBOX_CLICK_ATTEMPTS = 2
_GIVEX_FANCYBOX_CLOSE_VERIFY_S = 0.8
_GIVEX_FANCYBOX_CLOSE_TOTAL_BUDGET_S = 4.0

_log = logging.getLogger(__name__)


class _SubmissionErrorPopupDetected(Exception):
    """Internal signal for the Givex Fancybox submission-error popup."""

    def __init__(
        self,
        reason: str = "givex_fancybox_submission_error",
        *,
        popup_closed: bool = True,
    ):
        super().__init__(reason)
        self.reason = reason
        self.popup_closed = popup_closed


def _sanitize_url_for_log(url: str) -> str:
    if not url:
        return ""
    try:
        s = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((s.scheme, s.netloc, s.path, "", ""))
    except Exception:
        return "<unparseable-url>"


def _short_url(url: str) -> str:
    if not url:
        return ""
    try:
        s = urllib.parse.urlsplit(url)
        last = s.path.rsplit("/", 1)[-1] or s.path or "/"
        return f"{s.netloc}/.../{last}" if s.netloc else last
    except Exception:
        return "<unparseable-url>"


def _failure_screenshot_enabled() -> bool:
    return os.environ.get("FAILURE_SCREENSHOT_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def _failure_screenshot_dir() -> str:
    return os.environ.get("FAILURE_SCREENSHOT_DIR", "failure_screenshots")


def _failure_screenshot_allow_raw() -> bool:
    return os.environ.get("FAILURE_SCREENSHOT_ALLOW_RAW", "0").strip().lower() in ("1", "true", "yes", "on")


# ── MaxMind GeoLite2 singleton ────────────────────────────────────────────
# Loaded once at startup via init_maxmind_reader(); subsequent lookups reuse
# the open Reader object, keeping latency effectively <1ms (RAM only, no I/O).
_MAXMIND_READER = None  # pylint: disable=invalid-name
_MAXMIND_READER_LOCK = threading.Lock()

# Default location for the GeoLite2 City database (relative to repo root).
_DEFAULT_MMDB_PATH = "data/GeoLite2-City.mmdb"


def _resolve_mmdb_path() -> str:
    """Return the configured MaxMind database file path.

    Resolution order:

    1. ``GEOIP_DB_PATH`` environment variable (preferred / canonical name).
    2. ``MAXMIND_DB_PATH`` environment variable (legacy alias accepted for
       compatibility with the spec / blueprint and older operator scripts).
    3. The built-in default ``data/GeoLite2-City.mmdb``.

    When both env vars are set, ``GEOIP_DB_PATH`` wins.
    """
    return (
        os.environ.get("GEOIP_DB_PATH")
        or os.environ.get("MAXMIND_DB_PATH")
        or _DEFAULT_MMDB_PATH
    )


# Public alias of :func:`_resolve_mmdb_path` for callers outside this
# module/package boundary (e.g. the application entrypoint). The
# underscored name is retained for internal call sites and existing tests;
# external callers should prefer :func:`resolve_mmdb_path`.
def resolve_mmdb_path() -> str:
    """Return the configured MaxMind database file path (public API)."""
    return _resolve_mmdb_path()


def init_maxmind_reader(mmdb_path: str | None = None) -> None:
    """Load the GeoLite2-City database into the module-level singleton.

    Call once at application startup, before any :func:`maxmind_lookup_zip` or
    :func:`_lookup_maxmind_utc_offset` calls, to preload the DB into RAM and
    eliminate per-lookup disk I/O.

    Args:
        mmdb_path: Override path to the ``.mmdb`` file.  Falls back to the
            ``GEOIP_DB_PATH`` environment variable (preferred), then to the
            ``MAXMIND_DB_PATH`` legacy alias, then to the default
            ``data/GeoLite2-City.mmdb``.

    Raises:
        FileNotFoundError: If the database file is not found at the resolved path.
        ImportError: If the ``geoip2`` package is not installed.
    """
    global _MAXMIND_READER  # pylint: disable=global-statement,invalid-name
    path = mmdb_path or _resolve_mmdb_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"MaxMind GeoLite2 database not found at '{path}'. "
            "Run scripts/download_maxmind.py (requires MAXMIND_LICENSE_KEY) "
            "to download the database."
        )
    try:
        geoip2_database = importlib.import_module("geoip2.database")
    except ImportError as exc:
        raise ImportError(
            "geoip2 package is required for MaxMind lookups. "
            "Install it with: pip install geoip2"
        ) from exc
    with _MAXMIND_READER_LOCK:
        _MAXMIND_READER = geoip2_database.Reader(path)
    try:
        globals()["_MAXMIND_FILE_MTIME"] = os.path.getmtime(path)
    except OSError:  # pragma: no cover - defensive
        pass
    _log.info("MaxMind GeoLite2 reader initialised from '%s'.", path)


# ── MaxMind hot-reload (D1) ────────────────────────────────────────────────
# In-process background thread that checks mtime on the .mmdb file every
# MAXMIND_RELOAD_INTERVAL_HOURS (default 24) and atomically swaps the reader
# when the file changes.  Complements (but does not require) an external
# cron/systemd refresher — see README "Production Deployment".
_MAXMIND_RELOAD_INTERVAL_HOURS = int(
    os.environ.get("MAXMIND_RELOAD_INTERVAL_HOURS", "24")
)
_MAXMIND_FILE_MTIME: float | None = None
_MAXMIND_RELOAD_THREAD: threading.Thread | None = None
_MAXMIND_RELOAD_STOP = threading.Event()
# Grace period (seconds) before closing the previous reader so in-flight
# lookups that already captured a local reference finish safely.
_MAXMIND_SWAP_GRACE_SECONDS = 5


def _get_mmdb_path() -> str:
    """Return the configured MaxMind database file path.

    Backwards-compatible alias for :func:`_resolve_mmdb_path`.
    """
    return _resolve_mmdb_path()


def _atomic_swap_reader() -> None:
    """Create a fresh Reader from the current mmdb file and swap it in.

    The assignment to ``_MAXMIND_READER`` is a single-opcode rebinding, so
    concurrent readers either see the old or the new reader — never a
    half-constructed object.  The old reader is closed after a short grace
    period on a background thread so in-flight ``maxmind_lookup_zip`` calls
    can complete against their local reference.
    """
    global _MAXMIND_READER, _MAXMIND_FILE_MTIME  # pylint: disable=global-statement
    path = _get_mmdb_path()
    try:
        geoip2_database = importlib.import_module("geoip2.database")
    except ImportError:
        _log.warning("_atomic_swap_reader: geoip2 package missing; skip swap")
        return
    new_reader = geoip2_database.Reader(path)
    old_reader = _MAXMIND_READER
    _MAXMIND_READER = new_reader  # single-opcode rebinding (GIL-atomic)
    try:
        _MAXMIND_FILE_MTIME = os.path.getmtime(path)
    except OSError:
        _MAXMIND_FILE_MTIME = None
    if old_reader is not None and old_reader is not new_reader:
        def _close_after_grace(reader):
            try:
                if _MAXMIND_RELOAD_STOP.wait(_MAXMIND_SWAP_GRACE_SECONDS):
                    # Stop requested during grace period; still close the reader.
                    pass
                close = getattr(reader, "close", None)
                if callable(close):
                    close()
            except Exception as exc:  # pylint: disable=broad-except
                _log.debug("_atomic_swap_reader: close old reader failed: %s", exc)
        threading.Thread(
            target=_close_after_grace,
            args=(old_reader,),
            daemon=True,
            name="maxmind-old-reader-close",
        ).start()
    _log.info("MaxMind reader hot-swapped from '%s'.", path)


def _maxmind_reload_loop() -> None:
    """Background loop: wake every interval, swap reader if mtime changed."""
    interval_s = _MAXMIND_RELOAD_INTERVAL_HOURS * 3600
    while not _MAXMIND_RELOAD_STOP.wait(interval_s):
        try:
            path = _get_mmdb_path()
            mtime = os.path.getmtime(path)
            if _MAXMIND_FILE_MTIME is not None and mtime > _MAXMIND_FILE_MTIME:
                _atomic_swap_reader()
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning("MaxMind reload check failed: %s", exc)


def start_maxmind_auto_reload() -> None:
    """Start the background thread that hot-reloads the MaxMind DB.

    No-op if the thread is already running.  Safe to call at startup after
    :func:`init_maxmind_reader`.  The thread is a daemon and will be torn
    down automatically on process exit; call :func:`stop_maxmind_auto_reload`
    for graceful shutdown.
    """
    global _MAXMIND_RELOAD_THREAD  # pylint: disable=global-statement
    if _MAXMIND_RELOAD_THREAD is not None and _MAXMIND_RELOAD_THREAD.is_alive():
        return
    _MAXMIND_RELOAD_STOP.clear()
    _MAXMIND_RELOAD_THREAD = threading.Thread(
        target=_maxmind_reload_loop,
        daemon=True,
        name="maxmind-auto-reload",
    )
    _MAXMIND_RELOAD_THREAD.start()


def stop_maxmind_auto_reload() -> None:
    """Signal the reload thread to stop and join with a 5s timeout."""
    global _MAXMIND_RELOAD_THREAD  # pylint: disable=global-statement
    _MAXMIND_RELOAD_STOP.set()
    thread = _MAXMIND_RELOAD_THREAD
    if thread is not None:
        thread.join(timeout=5)
    _MAXMIND_RELOAD_THREAD = None


# ── URL constants ─────────────────────────────────────────────────────────
URL_GEO_CHECK = "https://lumtest.com/myip.json"
URL_BASE      = "https://wwws-usa2.givex.com/cws4.0/lushusa/"

# Allowlist of hosts permitted for Givex URL env overrides. A misconfigured
# or malicious env override could otherwise redirect the bot to a wrong host
# (typo-squat / phishing / staging leak into prod). Foreign hosts are only
# accepted when ``ALLOW_NON_PROD_GIVEX_HOSTS`` is truthy (``1``/``true``/
# ``yes``, case-insensitive — same convention as other repo flags such as
# ``ENABLE_PRODUCTION_TASK_FN`` and ``BITBROWSER_POOL_MODE``), in which
# case a WARNING is logged. See issue [P2] A3 audit.
_ALLOWED_GIVEX_HOSTS = ("wwws-usa2.givex.com",)
_ALLOWED_GIVEX_SCHEMES = ("https",)


def _allow_non_prod_givex_hosts() -> bool:
    """Return True when the bypass flag is set to a truthy value.

    Truthy values: ``"1"``, ``"true"``, ``"yes"`` (case-insensitive,
    surrounding whitespace ignored).  Anything else — including unset,
    ``"0"``, ``"false"`` — denies the bypass.  The accepted set matches
    other boolean env flags in this repo so operators see one convention.
    """
    return os.getenv("ALLOW_NON_PROD_GIVEX_HOSTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _validate_url(name: str, url: str) -> str:
    """Validate ``url`` against :data:`_ALLOWED_GIVEX_HOSTS`.

    Returns ``url`` unchanged when the parsed hostname is in the allowlist
    **and** the scheme is ``https`` (no http downgrade, no ``javascript:``
    scheme, no scheme-less / hostname-less path).  If the host is foreign
    and the bypass flag :func:`_allow_non_prod_givex_hosts` is set, a
    WARNING is logged and ``url`` is returned — but the scheme must still
    be ``https``.  Otherwise a :class:`RuntimeError` is raised at module
    import time, with a message naming the offending env var, host and
    scheme so the operator can fix the override quickly.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    scheme = (parsed.scheme or "").lower()

    if scheme not in _ALLOWED_GIVEX_SCHEMES:
        # SECURITY: refuse http downgrade, javascript:, file:, scheme-less
        # paths, etc., regardless of the bypass flag — the bypass is for
        # non-prod *hosts*, never for non-https schemes.
        _log.error(
            "SECURITY: %s rejected — scheme %r not in %r (url=%r)",
            name, scheme, _ALLOWED_GIVEX_SCHEMES, url,
        )
        raise RuntimeError(
            f"{name} scheme {scheme!r} is not allowed; only "
            f"{_ALLOWED_GIVEX_SCHEMES!r} are accepted (url={url!r})."
        )

    if host in _ALLOWED_GIVEX_HOSTS:
        return url

    if _allow_non_prod_givex_hosts():
        _log.warning(
            "INSECURE/DEGRADED: %s host %r is not in Givex allowlist %r; "
            "accepted because ALLOW_NON_PROD_GIVEX_HOSTS is truthy. "
            "This MUST NOT be set in production.",
            name, host, _ALLOWED_GIVEX_HOSTS,
        )
        return url

    _log.error(
        "SECURITY: %s rejected — host %r not in allowlist %r (url=%r)",
        name, host, _ALLOWED_GIVEX_HOSTS, url,
    )
    raise RuntimeError(
        f"{name} host {host!r} is not in the Givex host allowlist "
        f"{_ALLOWED_GIVEX_HOSTS!r}. Set ALLOW_NON_PROD_GIVEX_HOSTS=1 to "
        f"override (non-prod only)."
    )


URL_EGIFT     = _validate_url("GIVEX_EGIFT_URL", os.getenv(
    "GIVEX_EGIFT_URL",
    "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/",
))
URL_CART      = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/shopping-cart.html"
URL_CHECKOUT  = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/checkout.html"
URL_PAYMENT   = _validate_url("GIVEX_PAYMENT_URL", os.getenv(
    "GIVEX_PAYMENT_URL",
    "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html",
))

# ── URL fragments used to detect order confirmation ─────────────────────────
URL_CONFIRM_FRAGMENTS = ("/confirmation", "/order-confirmation", "order-confirm")
# Host/domain that must be present in current_url before DOM/text-based
# confirmation signals (generic CSS class, "thank you for your order")
# are trusted. This avoids false-positives on unrelated pages (e.g. a
# blog post or marketing page that happens to contain similar markup).
URL_CONFIRM_HOST = "givex.com"

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

_SELECTOR_NAMES = dict(
    (sel, name) for sel, name in (
        (SEL_GREETING_MSG, "SEL_GREETING_MSG"), (SEL_AMOUNT_INPUT, "SEL_AMOUNT_INPUT"),
        (SEL_RECIPIENT_NAME, "SEL_RECIPIENT_NAME"), (SEL_RECIPIENT_EMAIL, "SEL_RECIPIENT_EMAIL"),
        (SEL_CONFIRM_RECIPIENT_EMAIL, "SEL_CONFIRM_RECIPIENT_EMAIL"), (SEL_SENDER_NAME, "SEL_SENDER_NAME"),
        (SEL_GUEST_EMAIL, "SEL_GUEST_EMAIL"),
    )
)
_SELECTOR_LOG_NAMES: dict[str, str] = {}


def _selector_name(sel: str) -> str:
    return (
        _SELECTOR_NAMES.get(sel)
        or _SELECTOR_LOG_NAMES.get(sel)
        or "UNREGISTERED_SELECTOR"
    )

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
# Visible Order Total element verified against the captured watchdog/preflight
# total in :meth:`GivexDriver.submit_purchase` right before clicking COMPLETE
# PURCHASE so a mid-cycle cart mutation cannot reach the irreversible click
# (Spec §5 line 287 — E3 audit).
SEL_ORDER_TOTAL_DISPLAY = (
    "#orderTotal, #headingTotal, #cws_lbl_orderTotal, .order-total, .checkout-total, [data-total],"
    " #orderTotalLine, #orderSubtotal, #totalsContent"
)
_SELECTOR_NAMES.update({
    SEL_CARD_NAME: "SEL_CARD_NAME",
    SEL_CARD_NUMBER: "SEL_CARD_NUMBER",
    SEL_CARD_CVV: "SEL_CARD_CVV",
    SEL_BILLING_ADDRESS: "SEL_BILLING_ADDRESS",
    SEL_BILLING_CITY: "SEL_BILLING_CITY",
    SEL_BILLING_ZIP: "SEL_BILLING_ZIP",
    SEL_BILLING_PHONE: "SEL_BILLING_PHONE",
})
_SELECTOR_LOG_NAMES.update({
    SEL_BILLING_COUNTRY: "SEL_BILLING_COUNTRY", SEL_BILLING_STATE: "SEL_BILLING_STATE",
    SEL_CARD_EXPIRY_MONTH: "SEL_CARD_EXPIRY_MONTH", SEL_CARD_EXPIRY_YEAR: "SEL_CARD_EXPIRY_YEAR",
})
# Tolerance for DOM-vs-expected total comparison; absorbs display rounding.
_ORDER_TOTAL_TOLERANCE = decimal.Decimal("0.01")
# Reject pasted card/address payloads but allow long legal names.
_MAX_CARDHOLDER_NAME_LENGTH = 60
# Bound two-digit year expansion to current_year .. current_year + window.
_MIN_EXPIRY_YEAR = 2000
_EXPIRY_YEAR_WINDOW_YEARS = 20
# Cap diagnostic option list so logs stay concise.
_OPTION_DIAGNOSTIC_LIMIT = 24
_MONTH_NAMES = (
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may", "may"), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sep"), ("october", "oct"), ("november", "nov"), ("december", "dec"),
)


def _looks_like_cardholder_name(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 2 or len(s) > _MAX_CARDHOLDER_NAME_LENGTH:
        return False
    digit_chars = sum(1 for c in s if c.isdigit())
    # Names can include a few digits, but half-or-more digits is a strong
    # signal that a card number or other numeric payload polluted the field.
    return digit_chars * 2 < len(s) and any(c.isalpha() for c in s)


def _is_expiry_month_selector(selector: str) -> bool:
    return selector == SEL_CARD_EXPIRY_MONTH or "ccExpMon" in str(selector)


def _is_expiry_year_selector(selector: str) -> bool:
    return selector == SEL_CARD_EXPIRY_YEAR or "ccExpYr" in str(selector)


def _option_value_text(option) -> tuple[str, str]:
    if not isinstance(option, dict):
        return "", ""
    value, text = option.get("value", ""), option.get("text", "")
    return ("" if value is None else str(value), "" if text is None else str(text))


def _numeric_option_key(value: str) -> int | None:
    text = (value or "").strip()
    return int(text) if re.fullmatch(r"\d+", text) else None


def _month_numeric_token_key(value: str) -> int | None:
    text = (value or "").strip().lower()
    matches: list[int] = []
    for token in re.split(r"[_\-/\s]+", text):
        if re.fullmatch(r"\d{1,2}", token):
            number = int(token)
            if 1 <= number <= 12:
                matches.append(number)
    return matches[0] if len(matches) == 1 else None


def _month_name_key(value: str) -> int | None:
    for token in re.findall(r"[a-z]+", (value or "").strip().lower()):
        for idx, (full_name, abbrev) in enumerate(_MONTH_NAMES, start=1):
            if token == full_name or token == abbrev:
                return idx
    return None


def _month_option_key(value: str) -> int | None:
    return _month_name_key(value) or _month_numeric_token_key(value)


def _month_placeholder(value: str, text: str) -> bool:
    if (value or "").strip():
        return False
    return (text or "").strip().lower() in {"", "month", "select month", "--"}


def _has_conflicting_month_name(value: str, text: str, requested_month: int) -> bool:
    for candidate in (value, text):
        month = _month_name_key(candidate)
        if month is not None and month != requested_month:
            return True
    return False


def _expand_two_digit_year(value: int, current_year: int) -> int | None:
    matches = [
        year for year in range(current_year, current_year + _EXPIRY_YEAR_WINDOW_YEARS + 1)
        if year % 100 == value
    ]
    return matches[0] if len(matches) == 1 else None


def _year_option_key(value: str, current_year: int) -> int | None:
    text = (value or "").strip()
    if not re.fullmatch(r"\d+", text):
        return None
    if len(text) == 2:
        return _expand_two_digit_year(int(text), current_year)
    if len(text) == 4:
        if _MIN_EXPIRY_YEAR <= (year := int(text)) <= current_year + _EXPIRY_YEAR_WINDOW_YEARS:
            return year
    return None


def _raise_option_not_found(
    selector: str,
    requested: str,
    options,
    *,
    selected_index: int | None = None,
    current_value: str | None = None,
    disabled: bool | None = None,
) -> NoReturn:
    option_pairs = [_option_value_text(option) for option in options]
    values = [value for value, _text in option_pairs]
    texts = [text for _value, text in option_pairs]
    if _is_expiry_month_selector(selector) or _is_expiry_year_selector(selector):
        raise ValueError(
            f"Option not found selector={_selector_name(selector)} "
            f"option_count={len(options)}, "
            f"Available values={values[:_OPTION_DIAGNOSTIC_LIMIT]}, "
            f"texts={texts[:_OPTION_DIAGNOSTIC_LIMIT]}, "
            f"selectedIndex={selected_index}, current_value={current_value!r}, "
            f"disabled={disabled}"
        )
    raise ValueError(
        f"Option not found selector={_selector_name(selector)} "
        f"option_count={len(options)}, "
        f"value_lengths={[len(v) for v in values][:_OPTION_DIAGNOSTIC_LIMIT]}, "
        f"text_lengths={[len(t) for t in texts][:_OPTION_DIAGNOSTIC_LIMIT]}, "
        f"selectedIndex={selected_index}, "
        f"current_value_len={len(current_value or '')}, disabled={disabled}"
    )


def _find_matching_option_index(
    selector: str,
    requested,
    options,
    *,
    current_year: int | None = None,
) -> int:
    requested_text = "" if requested is None else str(requested)
    option_pairs = [_option_value_text(option) for option in options]
    is_expiry_month = _is_expiry_month_selector(selector)
    requested_month = _month_option_key(requested_text) if is_expiry_month else None

    for idx, (value, text) in enumerate(option_pairs):
        if is_expiry_month:
            if requested_month is None or _month_placeholder(value, text):
                continue
            if _has_conflicting_month_name(value, text, requested_month):
                continue
        if value == requested_text or text.strip() == requested_text:
            return idx

    if is_expiry_month and requested_month is not None:
        for idx, (value, text) in enumerate(option_pairs):
            if _month_placeholder(value, text):
                continue
            if _has_conflicting_month_name(value, text, requested_month):
                continue
            if requested_month in (_month_numeric_token_key(value), _month_numeric_token_key(text)):
                return idx
    elif (requested_numeric := _numeric_option_key(requested_text)) is not None:
        for idx, (value, text) in enumerate(option_pairs):
            if requested_numeric in (_numeric_option_key(value), _numeric_option_key(text)):
                return idx

    if is_expiry_month and requested_month is not None:
        for idx, (value, text) in enumerate(option_pairs):
            if _month_placeholder(value, text):
                continue
            if _has_conflicting_month_name(value, text, requested_month):
                continue
            if requested_month in (_month_name_key(value), _month_name_key(text)):
                return idx

    if _is_expiry_year_selector(selector):
        year = current_year if current_year is not None else datetime.datetime.now().year
        if (requested_year := _year_option_key(requested_text, year)) is not None:
            for idx, (value, text) in enumerate(option_pairs):
                if requested_year in (_year_option_key(value, year), _year_option_key(text, year)):
                    return idx

    _raise_option_not_found(selector, requested_text, options)
    raise AssertionError("unreachable: _raise_option_not_found should always raise")


def _parse_money_text(raw):
    """Locale-aware money parser → :class:`decimal.Decimal` or ``None``.

    Handles US (``"1,234.56"``), European (``"1.234,56"``, ``"49,99"``),
    and accounting negatives (``"($49.99)"``).  When only one separator
    type is present, a single occurrence followed by exactly 3 digits is
    treated as a thousands separator; otherwise as a decimal point.
    """
    if not raw:
        return None
    text = str(raw).strip()
    neg = "(" in text and ")" in text
    keep = re.sub(r"[^\d,.\-+]", "", text)
    if not keep or not any(c.isdigit() for c in keep):
        return None
    sign = keep.startswith("-")
    keep = keep.lstrip("+-")
    has_dot, has_comma = "." in keep, "," in keep
    if has_dot and has_comma:
        if keep.rfind(",") > keep.rfind("."):
            keep = keep.replace(".", "").replace(",", ".")
        else:
            keep = keep.replace(",", "")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = keep.split(sep)
        is_thousands = len(parts) > 2 or (
            len(parts) == 2 and len(parts[-1]) == 3 and parts[0]
        )
        if is_thousands:
            keep = keep.replace(sep, "")
        elif has_comma:
            keep = keep.replace(",", ".")
    try:
        value = decimal.Decimal(keep)
    except decimal.InvalidOperation:
        return None
    if sign:
        value = -value
    if neg and value > 0:
        value = -value
    return value

# ── Post-submit state detection (Step 5) ─────────────────────────────────────
# Maximum seconds to wait for the TransientMonitor daemon thread to exit after
# cancel() is signalled during submit_purchase (Issue #194, PR #206).
# Thread responds to the cancel event within one poll interval (~0.5 s);
# the 10 s cap is a defensive upper bound for unexpected scheduler delays.
_VBV_MONITOR_CANCEL_TIMEOUT_S: float = 10.0

SEL_CONFIRMATION_EL = ".order-confirmation, .confirmation-message"
SEL_DECLINED_MSG    = ".payment-error, .error-message, div[data-error]"
SEL_UI_LOCK_SPINNER = ".loading-overlay, .spinner, div[aria-busy='true']"
SEL_VBV_IFRAME      = "iframe[src*='3dsecure'], iframe[src*='adyen'], iframe[id*='threeds']"
# VBV/3DS cancel button selectors in priority order (Phase 4 audit [D6]).
# Evaluated one-by-one via :meth:`GivexDriver._find_vbv_cancel_button`; first
# match wins so higher-priority selectors (explicit Cancel / Return-to-Merchant)
# are preferred over generic close / X-icon fallbacks.  CSS comma-lists return
# the first DOM-order match and cannot express priority — hence the tuple.
SEL_VBV_CANCEL_BUTTONS = (
    # Priority 1: explicit Cancel
    "button[id*='cancel' i]",
    "a[id*='cancel' i]",
    "button[aria-label*='cancel' i]",
    # Priority 2: Return to Merchant
    "button[id*='return' i]",
    "a[id*='return' i]",
    "button[aria-label*='return' i]",
    # Priority 3: generic Close / X
    "button[aria-label*='close' i]",
    "button.close",
    ".modal-close",
    "[role='button'][aria-label*='close' i]",
    # Priority 4: icon-based X / dismiss
    "button > svg[class*='close']",
    "[class*='icon-close']",
    "[data-dismiss='modal']",
)
# Backward-compat alias — legacy code still reads a comma-joined CSS string.
# New code should prefer :data:`SEL_VBV_CANCEL_BUTTONS` + the helper.
SEL_VBV_CANCEL_BTN  = ", ".join(SEL_VBV_CANCEL_BUTTONS)
SEL_POPUP_CLOSE_BTN = "button.modal-close, button[aria-label='Close'], .modal button[type='button']"
SEL_POPUP_SOMETHING_WRONG = ".modal, .popup, .dialog, .alert, .error-modal"
# P1-1: XPath text-match for "Something went wrong" popup — avoids false
# positives from cookie banners / newsletter modals / success modals that also
# use generic .modal classes. Case-insensitive via translate() on the
# normalized text content of any div/section/dialog descendant.
XPATH_POPUP_SWW = (
    "//*[self::div or self::section or self::dialog]"
    "[contains(translate(normalize-space(.),"
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
    "'something went wrong')]"
)
SEL_POPUP_CLOSE = SEL_POPUP_CLOSE_BTN
# P1-6: XPath fallback for popup close — matches <button>/<a> whose normalized
# text is exactly one of Close/OK/X/Đóng (case-insensitive for ASCII via
# translate()). Used only when the CSS locator above matches nothing, so the
# default selector-driven path is unchanged.
_XPATH_POPUP_CLOSE_LOWER = (
    "translate(normalize-space(.),"
    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
)
XPATH_POPUP_CLOSE = (
    "//*[self::button or self::a]"
    f"[{_XPATH_POPUP_CLOSE_LOWER}='close'"
    f" or {_XPATH_POPUP_CLOSE_LOWER}='ok'"
    f" or {_XPATH_POPUP_CLOSE_LOWER}='x'"
    " or normalize-space(.)='Đóng' or normalize-space(.)='đóng']"
)
SEL_NEUTRAL_DIV     = "body"

# ── Popup text-match patterns (P1-1, Blueprint §6 Fork 3 text-verify) ─────────
# English patterns for error/warning popups
POPUP_TEXT_PATTERNS_EN = (
    "something went wrong",
    "an error occurred",
    "please try again",
    "payment failed",
    "transaction declined",
    "unable to process",
    "session expired",
    "service unavailable",
)

# Vietnamese patterns for error/warning popups
POPUP_TEXT_PATTERNS_VN = (
    "có lỗi xảy ra",
    "vui lòng thử lại",
    "thanh toán thất bại",
    "giao dịch bị từ chối",
    "không thể xử lý",
    "phiên đã hết hạn",
    "dịch vụ không khả dụng",
    "đã xảy ra sự cố",
)

# Combined default pattern set used by check_popup_text_match when no
# explicit patterns are supplied.
POPUP_TEXT_PATTERNS_DEFAULT = (
    POPUP_TEXT_PATTERNS_EN + POPUP_TEXT_PATTERNS_VN
)

# ── Thank-you popup text patterns (P1-2, Blueprint §6 Ngã rẽ 2) ───────────
# English patterns for success/thank-you confirmation popups
THANK_YOU_TEXT_PATTERNS_EN = (
    "thank you for your order",
    "thank you for your purchase",
    "order confirmed",
    "order confirmation",
    "your order has been placed",
    "payment successful",
)

# Vietnamese patterns for success/thank-you confirmation popups
THANK_YOU_TEXT_PATTERNS_VN = (
    "cảm ơn bạn đã đặt hàng",
    "cảm ơn bạn đã mua hàng",
    "đơn hàng đã được xác nhận",
    "xác nhận đơn hàng",
    "thanh toán thành công",
)

# Combined default pattern set used by detect_popup_thank_you when no
# explicit patterns are supplied.
THANK_YOU_TEXT_PATTERNS_DEFAULT = (
    THANK_YOU_TEXT_PATTERNS_EN + THANK_YOU_TEXT_PATTERNS_VN
)

_DEFAULT_GREETINGS = [
    "Happy gifting!",
    "Enjoy this little treat!",
    "Thinking of you!",
    "With love and best wishes!",
    "Hope this brightens your day!",
    "Happy Birthday!",
    "Best wishes",
    "Enjoy your gift!",
    "Thank you for being you",
]

# Env var pointing to an optional UTF-8 file with one extra greeting per
# line.  Spec §4 requires _GREETINGS be extensible without code changes.
_GREETINGS_FILE_ENV = "GIVEX_GREETINGS_FILE"

# Hard caps to prevent a malicious or accidentally-huge greetings file
# from exhausting memory at import time.  These bounds are intentionally
# generous for legitimate operator use (a few thousand short messages).
_GREETINGS_MAX_ENTRIES = 1000
_GREETINGS_MAX_LINE_LENGTH = 500


def _load_greetings(path: str | None = None) -> list[str]:
    """Return greetings = defaults + entries from optional file.

    Reads the path from ``GIVEX_GREETINGS_FILE`` when *path* is None.
    Each non-empty, stripped line of the UTF-8 file is appended after the
    defaults; the merged list is deduplicated while preserving order.

    Lines longer than ``_GREETINGS_MAX_LINE_LENGTH`` are skipped and the
    total number of merged entries is capped at ``_GREETINGS_MAX_ENTRIES``
    to bound memory use.  Any I/O or decoding error is logged at WARNING
    level and the function falls back to the defaults — startup must
    never be blocked by a bad greetings file.
    """
    greetings: list[str] = list(_DEFAULT_GREETINGS)
    file_path = path if path is not None else os.environ.get(_GREETINGS_FILE_ENV)
    if not file_path:
        return greetings
    try:
        with open(file_path, "r", encoding="utf-8-sig") as fh:
            seen = set(greetings)
            truncated = False
            for line in fh:
                if len(greetings) >= _GREETINGS_MAX_ENTRIES:
                    truncated = True
                    break
                entry = line.strip()
                if not entry or len(entry) > _GREETINGS_MAX_LINE_LENGTH:
                    continue
                if entry in seen:
                    continue
                greetings.append(entry)
                seen.add(entry)
            if truncated:
                _log.warning(
                    "_load_greetings: %s=%r exceeded %d entries; truncated",
                    _GREETINGS_FILE_ENV,
                    file_path,
                    _GREETINGS_MAX_ENTRIES,
                )
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning(
            "_load_greetings: cannot read %s=%r (%s); using defaults",
            _GREETINGS_FILE_ENV,
            file_path,
            exc,
        )
        return greetings
    return greetings


_GREETINGS = _load_greetings()


def reload_greetings(path: str | None = None) -> list[str]:
    """Re-read the greetings file and refresh the module-level list.

    Useful when ``GIVEX_GREETINGS_FILE`` is set or its target is updated
    after :mod:`modules.cdp.driver` has already been imported (Spec §4 —
    extensibility without redeploy).  ``_random_greeting`` always reads
    the live ``_GREETINGS`` global so the new list takes effect on the
    next call.
    """
    global _GREETINGS
    _GREETINGS = _load_greetings(path)
    return _GREETINGS

def _random_greeting(rnd=None) -> str:
    """Return a greeting message for the eGift form.

    When *rnd* is provided (typically the persona-seeded
    ``random.Random`` from a :class:`GivexDriver`), the choice is
    deterministic per persona seed — preserving Blueprint §8 consistency.
    Falls back to ``secrets.choice`` for callers that have no persona
    RNG (tests, ad-hoc usage); the cryptographic fallback never crashes.
    """
    if rnd is not None:
        try:
            return rnd.choice(_GREETINGS)
        except Exception as exc:  # pylint: disable=broad-except
            # Defensive fallback: anything goes wrong with the persona
            # RNG, fall back to secrets so the form fill never fails.
            _log.debug("_random_greeting: persona RNG failed (%s); using secrets fallback", exc)
    return secrets.choice(_GREETINGS)


def _lookup_maxmind_utc_offset(ip_addr: str) -> float | None:
    """Look up UTC offset for an IP using MaxMind GeoLite2-City.mmdb.

    Uses the module-level singleton reader when available (initialised via
    :func:`init_maxmind_reader`); falls back to a per-call open for backward
    compatibility in stub/test mode (with a warning log).
    """
    if _ZoneInfo is None:
        return None
    reader = _MAXMIND_READER
    if reader is None:
        # Lazy per-call fallback for test/stub mode; not latency-optimal.
        mmdb_path = _resolve_mmdb_path()
        if not os.path.exists(mmdb_path):
            return None
        try:
            geoip2_database = importlib.import_module("geoip2.database")
        except ImportError:
            return None
        try:
            with geoip2_database.Reader(mmdb_path) as _reader:
                record = _reader.city(ip_addr)
                tz_name = record.location.time_zone
                if tz_name:
                    tz_info = _ZoneInfo(tz_name)
                    now = datetime.datetime.now(tz_info)
                    offset = now.utcoffset()
                    if offset is None:
                        return None
                    return offset.total_seconds() / 3600.0
        except Exception as exc:  # pylint: disable=broad-except
            _log.debug("MaxMind lookup failed for %s: %s", ip_addr, exc)
        return None
    # Singleton path: no disk I/O, <1ms latency.
    try:
        record = reader.city(ip_addr)
        tz_name = record.location.time_zone
        if tz_name:
            tz_info = _ZoneInfo(tz_name)
            now = datetime.datetime.now(tz_info)
            offset = now.utcoffset()
            if offset is None:
                return None
            return offset.total_seconds() / 3600.0
    except Exception as exc:  # pylint: disable=broad-except
        _log.debug("MaxMind lookup failed for %s: %s", ip_addr, exc)
    return None


def maxmind_lookup_zip(ip_addr: str) -> str | None:
    """Look up postal/zip code for an IP using MaxMind GeoLite2-City.mmdb.

    Uses the module-level singleton reader when available (initialised via
    :func:`init_maxmind_reader` at startup); falls back to a per-call open
    for backward compatibility in stub/test mode (with a warning log).

    Args:
        ip_addr: IPv4 or IPv6 address string.

    Returns:
        A postal/zip code string (e.g. ``"10001"``) or ``None`` when the
        database is absent, ``geoip2`` is not installed, the record carries
        no postal code, or any lookup error occurs.
    """
    reader = _MAXMIND_READER
    if reader is None:
        # Lazy per-call fallback for test/stub mode.
        mmdb_path = _resolve_mmdb_path()
        if not os.path.exists(mmdb_path):
            return None
        try:
            geoip2_database = importlib.import_module("geoip2.database")
        except ImportError:
            return None
        try:
            with geoip2_database.Reader(mmdb_path) as _reader:
                record = _reader.city(ip_addr)
                postal_code = record.postal.code
                if postal_code:
                    return postal_code
        except Exception as exc:  # pylint: disable=broad-except
            _log.debug("MaxMind zip lookup failed for %s: %s", ip_addr, exc)
        return None
    # Singleton path: no disk I/O, <1ms latency.
    try:
        record = reader.city(ip_addr)
        postal_code = record.postal.code
        return postal_code or None
    except Exception as exc:  # pylint: disable=broad-except
        _log.debug("MaxMind zip lookup failed for %s: %s", ip_addr, exc)
    return None


def _safe_cdp_cmd(driver, command: str, params: dict) -> object:
    """Execute a CDP command with structured exception detection and logging.

    Wraps ``driver.execute_cdp_cmd(command, params)`` with:

    * **PII-safe logging** — error messages are passed through
      :func:`_sanitize_error` before logging so that card numbers,
      CVV values, and email addresses are never written to logs.
    * **Typed exceptions** — connection-level failures
      (``OSError``, ``ConnectionError``, ``TimeoutError``) are
      wrapped as :exc:`~modules.common.exceptions.SessionFlaggedError`
      to signal a broken session; all other failures are wrapped as
      :exc:`~modules.common.exceptions.CDPCommandError`.

    Args:
        driver: Raw Selenium WebDriver exposing ``execute_cdp_cmd``.
        command: CDP method name, e.g. ``"Input.dispatchMouseEvent"``.
        params: Parameter dict forwarded verbatim to the CDP call.

    Returns:
        The value returned by ``execute_cdp_cmd`` on success.

    Raises:
        SessionFlaggedError: On connection / transport failures.
        CDPCommandError: On non-retryable command-level failures.
    """
    try:
        return driver.execute_cdp_cmd(command, params)
    except (OSError, ConnectionError, TimeoutError) as exc:
        detail = _sanitize_error(str(exc))
        _log.error(
            "cdp_connect_error cmd=%r detail=%r",
            command,
            detail,
        )
        raise SessionFlaggedError(
            f"CDP connection error on '{command}': {detail}"
        ) from exc
    except Exception as exc:  # pylint: disable=broad-except
        detail = _sanitize_error(str(exc))
        _log.error(
            "cdp_command_error cmd=%r detail=%r",
            command,
            detail,
        )
        raise CDPCommandError(command, detail) from exc


def _dispatch_cdp_click_sequence(
        driver,
        abs_x: float,
        abs_y: float,
        *,
        rng: _random.Random | None = None,
        jitter: bool = False,
) -> None:
    """Dispatch a 3-event CDP mouse click (``mouseMoved`` → ``Pressed`` → ``Released``).

    Emits ``Input.dispatchMouseEvent`` at ``(abs_x, abs_y)`` for each event
    type so the target receives a proper hover-then-click sequence (matching
    real user input). When ``jitter`` is True, a small sub-pixel offset is
    added to each successive event to better mimic human cursor drift.

    Args:
        driver: Raw Selenium WebDriver exposing ``execute_cdp_cmd``.
        abs_x: Absolute X coordinate in viewport pixels.
        abs_y: Absolute Y coordinate in viewport pixels.
        rng: Optional ``random.Random``-compatible instance used when
            ``jitter`` is True. If ``None``, a fresh per-call
            ``random.Random()`` instance is used so no module-level RNG
            state is shared across threads.
        jitter: When True, apply up to ±0.5px per-event drift.
    """
    rnd = rng if rng is not None else _random.Random()
    for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
        event_x, event_y = abs_x, abs_y
        if jitter:
            event_x += rnd.uniform(-0.5, 0.5)
            event_y += rnd.uniform(-0.5, 0.5)
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": event_x,
                "y": event_y,
                "button": "left",
                "clickCount": 1,
            },
        )


def _get_proxy_ip(proxy_str: str | None = None) -> str | None:
    """Extract the proxy host IP from a proxy string via local DNS only.

    No external HTTP requests are made.  The proxy string is parsed using
    :func:`urllib.parse.urlparse`; if the host is already an IPv4/IPv6
    address it is returned directly.  Otherwise a single local DNS resolution
    via :func:`socket.gethostbyname` is performed.

    Args:
        proxy_str: Proxy URL/address, e.g. ``http://user:pass@1.2.3.4:8080``,
            ``1.2.3.4:8080``, or a hostname:port pair.  When ``None``, the
            ``PROXY_SERVER`` environment variable is consulted as a fallback.

    Returns:
        IPv4 string on success, or ``None`` if no proxy is configured or
        the host cannot be resolved.
    """
    raw = proxy_str
    if not raw:
        raw = os.environ.get("PROXY_SERVER", "").strip()
    if not raw:
        return None
    # Ensure urlparse receives a scheme so hostname is parsed correctly.
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urllib.parse.urlparse(raw)
        host = parsed.hostname
        if not host:
            return None
        # Try to validate as a literal IP address first.
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        # Resolve hostname via local DNS (no external geo API involved).
        return socket.gethostbyname(host)
    except Exception as exc:  # pylint: disable=broad-except
        _log.debug("_get_proxy_ip: could not extract IP from proxy '%s': %s", proxy_str, exc)
        return None


def _get_current_ip_best_effort() -> str | None:
    """Return proxy IP from PROXY_SERVER env var. No external HTTP calls.

    .. deprecated::
        Previously called ``api.ipify.org`` (external geo service).  That
        dependency has been removed.  Use :func:`_get_proxy_ip` directly for
        new code.  This wrapper reads ``PROXY_SERVER`` from the environment
        and delegates to :func:`_get_proxy_ip`.
    """
    return _get_proxy_ip()


# ── Session init helpers (Blueprint §2, §3, §6) ────────────────────────────

# URL schemes that identify internal browser windows (DevTools targets,
# chrome:// pages, extensions).  BitBrowser/chromedriver attach can expose
# these as ``window_handles`` entries that must not be treated as real
# content tabs during tab-janitor or preflight window selection.
_INTERNAL_WINDOW_SCHEMES = (
    "devtools://",
    "chrome://",
    "chrome-extension://",
)


def _is_internal_browser_window_url(url: str) -> bool:
    """Return True if *url* belongs to an internal browser window scheme.

    BitBrowser/chromedriver attach can expose DevTools or chrome:// targets
    as window_handles entries; these must not be treated as the main
    content tab during tab-janitor or preflight selection.
    """
    return str(url or "").lower().startswith(_INTERNAL_WINDOW_SCHEMES)


def _select_real_content_window(driver):
    """Switch to and return the first non-internal browser window handle.

    Iterates ``driver.window_handles`` in order, switching to each handle
    just long enough to read ``current_url``. Returns the first handle
    whose URL does NOT match :data:`_INTERNAL_WINDOW_SCHEMES`. The driver
    is left focused on that handle on success. Returns ``None`` when no
    real content window is found (caller decides how to react — the
    janitor returns 0 with a warning; preflight raises a clear error).

    Defensive against transient ``switch_to.window`` failures: any
    exception during URL probing is logged and the loop continues.
    """
    try:
        handles = list(driver.window_handles)
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("_select_real_content_window: window_handles failed: %s", exc)
        return None
    selected = None
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            url = driver.current_url or ""
        except Exception as exc:  # pylint: disable=broad-except
            _log.debug(
                "_select_real_content_window: probe failed for %s: %s",
                handle, exc,
            )
            continue
        if not _is_internal_browser_window_url(url):
            selected = handle
            break
    if selected is None:
        return None
    try:
        driver.switch_to.window(selected)
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning(
            "_select_real_content_window: failed to focus selected handle %s: %s",
            selected, exc,
        )
        return None
    return selected


def close_extra_tabs(driver) -> int:
    """Close all browser tabs except the first real content tab.

    BitBrowser/chromedriver attach can expose a ``devtools://`` target as
    ``window_handles[0]``. The previous implementation kept ``handles[0]``
    and closed everything else, which closed every real content tab and
    caused Chrome to exit (kills the Selenium session). The new logic
    selects the first non-internal-scheme window as the main tab and
    leaves internal windows (DevTools, chrome://, chrome-extension://)
    untouched. If no real content tab exists the function logs a warning
    and returns 0 without closing anything.

    Blueprint §2 Tab Janitor: BitBrowser profiles often open with extra
    ad/junk tabs.  The janitor must close them BEFORE pre-flight geo check
    so ``window_handles`` count does not confuse ``detect_page_state``.

    Args:
        driver: A Selenium-compatible driver exposing ``window_handles``,
            ``switch_to.window(handle)``, ``current_url`` and ``close()``.

    Returns:
        The number of extra tabs successfully closed.  Individual close
        failures are swallowed with a warning log so the janitor never
        crashes the calling flow.
    """
    try:
        handles = list(driver.window_handles)
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("close_extra_tabs: window_handles failed: %s", exc)
        return 0
    if len(handles) <= 1:
        return 0

    # Classify handles by URL.
    classified = []  # list of (handle, url, is_internal)
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            url = driver.current_url or ""
        except Exception as exc:  # pylint: disable=broad-except
            _log.debug(
                "close_extra_tabs: could not probe handle %s: %s", handle, exc,
            )
            # A probe failure may be a DevTools target, stale handle, or
            # transient CDP error. Treat as internal-like: never select it as
            # main and never close it.
            classified.append((handle, "", True))
            continue
        classified.append((handle, url, _is_internal_browser_window_url(url)))

    real = [h for (h, _u, internal) in classified if not internal]
    if not real:
        _log.warning(
            "close_extra_tabs: no real content windows found among %d handles; skipping",
            len(handles),
        )
        return 0

    main = real[0]
    closed = 0
    for handle, _url, is_internal in classified:
        if handle == main or is_internal:
            # Never close the chosen main tab and never close internal
            # browser windows (DevTools etc.) — closing internal targets
            # can also kill the session on some Chrome builds.
            continue
        try:
            driver.switch_to.window(handle)
            driver.close()
            closed += 1
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning("close_extra_tabs: failed to close %s: %s", handle, exc)
    try:
        driver.switch_to.window(main)
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("close_extra_tabs: failed to switch back to main: %s", exc)
    return closed


def handle_ui_lock_focus_shift(driver) -> bool:
    """Focus-Shift Retry per Blueprint §6 Ngã rẽ 1.

    Steps:
      1. Click ``SEL_NEUTRAL_DIV`` (``body``) via CDP to shift focus away
         from the locked submit button.
      2. Wait 0.5s to let any onBlur animation settle.
      3. Re-click ``SEL_COMPLETE_PURCHASE`` via CDP.

    Both clicks are dispatched through :meth:`GivexDriver.bounding_box_click`
    (CDP ``Input.dispatchMouseEvent`` with ``isTrusted=True``) instead of
    Selenium ``ActionChains`` — the latter emits ``isTrusted=False`` and is
    an anti-bot fingerprint (Phase 4 audit [B2]).

    This helper executes **exactly once** per invocation and never retries
    internally — the caller is responsible for enforcing the one-retry-per-
    cycle cap (Blueprint rule).  Returns ``True`` on apparent success and
    ``False`` on any exception (already logged).

    Args:
        driver: A :class:`GivexDriver` or any object exposing a
            ``bounding_box_click(selector)`` method.  A raw Selenium driver
            is also accepted for backward compatibility; in that case the
            call is rejected with a warning because only CDP clicks are
            permitted in the Fork-1 retry path.
    """
    bbox_click = getattr(driver, "bounding_box_click", None)
    if not callable(bbox_click):  # pragma: no cover - defensive
        _log.warning(
            "handle_ui_lock_focus_shift: driver has no bounding_box_click; "
            "Fork-1 retry requires a GivexDriver-compatible wrapper"
        )
        return False
    # Step 1: click neutral region (body) via CDP to shift focus.
    try:
        bbox_click(SEL_NEUTRAL_DIV)
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("UI lock neutral-click failed: %s", exc)
        return False

    time.sleep(0.5)  # settle onBlur

    # Step 2: re-click Complete Purchase via CDP (not ActionChains).
    try:
        bbox_click(SEL_COMPLETE_PURCHASE)
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("UI lock re-click failed: %s", exc)
        return False
    return True


def vbv_dynamic_wait(rng: _random.Random | None = None) -> float:
    """Wait 8–12s for VBV/3DS iframe to load (Blueprint §6 Fork 3, no DOM)."""
    duration = (rng or _random).uniform(8.0, 12.0)
    time.sleep(duration)
    return duration


def cdp_click_iframe_element(
        driver, iframe_selector: str, element_selector: str,
        rng: _random.Random | None = None,
) -> tuple[float, float]:
    """Click element inside iframe via CDP absolute coordinates (Blueprint §6 Fork 3)."""
    # Input.dispatchMouseEvent yields isTrusted=True and bypasses iframe sandbox.
    # Use a fresh per-call RNG when caller did not supply one so we never mutate
    # the module-level ``random`` singleton (no shared state across threads).
    rng = rng if rng is not None else _random.Random()
    base = getattr(driver, "_driver", driver)
    by_css = By.CSS_SELECTOR if By is not None else "css selector"
    iframe = base.find_element(by_css, iframe_selector)
    base.switch_to.frame(iframe)
    elem_rect = None
    try:
        elem = base.find_element(by_css, element_selector)
        elem_rect = base.execute_script(
            "const r=arguments[0].getBoundingClientRect();"
            "return {left:r.left,top:r.top,width:r.width,height:r.height};",
            elem,
        )
    finally:
        base.switch_to.default_content()
    if elem_rect is None:
        raise RuntimeError(
            "Failed to resolve iframe element rect for selector: "
            f"{element_selector}"
        )
    iframe_rect = base.execute_script(
        "const r=arguments[0].getBoundingClientRect();"
        "return {left:r.left,top:r.top};",
        iframe,
    )
    abs_x = (
        iframe_rect["left"]
        + elem_rect["left"]
        + elem_rect["width"] / 2
        + rng.uniform(-15, 15)
    )
    abs_y = (
        iframe_rect["top"]
        + elem_rect["top"]
        + elem_rect["height"] / 2
        + rng.uniform(-5, 5)
    )
    _dispatch_cdp_click_sequence(base, abs_x, abs_y, rng=rng, jitter=True)
    return abs_x, abs_y


def _popup_use_xpath() -> bool:
    """Return True if the XPath text-match locator should be used (P1-1).

    Default: True. Set env ``POPUP_USE_XPATH=0`` (or ``false``/``no``) to
    roll back to the legacy CSS selector (:data:`SEL_POPUP_SOMETHING_WRONG`).
    """
    raw = os.environ.get("POPUP_USE_XPATH", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _popup_clear_after_close() -> bool:
    """Return True if card fields should be cleared after popup close (P1-2).

    Default: True. Set env ``POPUP_CLEAR_AFTER_CLOSE=0`` (or ``false``/``no``)
    to roll back to legacy behavior (close popup only, no card-field clear).
    """
    raw = os.environ.get("POPUP_CLEAR_AFTER_CLOSE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


# P1-? — Max retry count for closing the "Something went wrong" popup.
# The popup can re-appear immediately after a single close click (animation
# race, re-render on the same error), so one click is not always enough.
# Cap the retry loop at a small constant to avoid unbounded click storms.
_POPUP_CLOSE_MAX_RETRIES_DEFAULT = 3
_POPUP_CLOSE_VERIFY_TIMEOUT = 0.5


def _popup_close_max_retries() -> int:
    """Return the max number of close-button click attempts.

    Default: 3. Set env ``POPUP_CLOSE_MAX_RETRIES`` to override (clamped
    to ``[1, 10]``). Values that fail to parse fall back to the default.
    """
    raw = os.environ.get("POPUP_CLOSE_MAX_RETRIES", "").strip()
    if not raw:
        return _POPUP_CLOSE_MAX_RETRIES_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _POPUP_CLOSE_MAX_RETRIES_DEFAULT
    if value < 1:
        return 1
    if value > 10:
        return 10
    return value


def _popup_still_present(base_driver, locator, timeout: float) -> bool:
    """Return True if the popup matched by ``locator`` is still present.

    Uses a short :class:`WebDriverWait` so we do not block the retry loop
    when the popup has been successfully dismissed.
    """
    if WebDriverWait is None or EC is None:  # pragma: no cover - import guard
        return False
    try:
        WebDriverWait(base_driver, timeout).until(
            EC.presence_of_element_located(locator)
        )
    except Exception:  # pylint: disable=broad-except
        # TimeoutException → popup gone; any other selenium error →
        # assume gone rather than loop forever.
        return False
    return True


class PopupCloseOutcome(enum.Enum):
    """Signal returned by :func:`handle_something_wrong_popup` (P1-2).

    Allows the orchestrator retry loop to distinguish "popup was not
    present" (no-op) from "popup closed — card fields wiped, re-fill
    required" so a fresh card from the order queue can be submitted
    instead of silently re-submitting the stale value.

    The enum is bool-compatible: ``CLOSED_NEEDS_REFILL`` is truthy and
    every other value is falsy, preserving ``if handle_...():`` call
    sites that existed before P1-2.
    """

    NOT_PRESENT = "not_present"
    CLOSED_NEEDS_REFILL = "closed_needs_refill"
    CLOSE_FAILED = "close_failed"

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self is PopupCloseOutcome.CLOSED_NEEDS_REFILL


def _popup_xpath_click_close(driver) -> bool:
    """P1-6 / D7: XPath-based fallback close that ONLY uses CDP clicks.

    Tries each element matching :data:`XPATH_POPUP_CLOSE` in document order
    and dispatches a CDP ``Input.dispatchMouseEvent`` sequence
    (``isTrusted=True``) at a randomized point inside its bounding rect via
    :func:`_dispatch_cdp_click_sequence`. Returns ``True`` on the first
    successful dispatch, ``False`` if no element matches or every attempt
    raises.

    Native Selenium ``element.click()`` is intentionally **not** used in
    the FSM handler path — it would emit ``isTrusted=False`` events and
    degrade anti-bot quality (audit finding [D7]).

    Each candidate element is first scrolled into the viewport via
    ``scrollIntoView({block:'center'})`` before its bounding rect is read,
    so an off-screen close control still receives a hit-testable click —
    matching the implicit behavior of Selenium ``element.click()``.
    """
    base = getattr(driver, "_driver", driver)
    try:
        elements = base.find_elements("xpath", XPATH_POPUP_CLOSE)
    except WebDriverException as exc:
        _log.warning("popup XPath close: find_elements failed: %s", exc)
        return False
    rnd = getattr(driver, "_rnd", None) or _random.Random()
    for element in elements:
        try:
            rect = base.execute_script(
                "arguments[0].scrollIntoView({block:'center',inline:'center'});"
                "var r=arguments[0].getBoundingClientRect();"
                "return {left:r.left,top:r.top,width:r.width,height:r.height};",
                element,
            )
        except WebDriverException as exc:
            _log.debug(
                "popup XPath close: getBoundingClientRect failed: %s", exc
            )
            continue
        if (
            not rect
            or rect.get("width", 0) == 0
            or rect.get("height", 0) == 0
        ):
            _log.debug(
                "popup XPath close: skipping element with missing/zero rect: %r",
                rect,
            )
            continue
        offset_x = max(-15.0, min(15.0, rnd.uniform(-15, 15)))
        offset_y = max(-5.0, min(5.0, rnd.uniform(-5, 5)))
        center_x = rect["left"] + rect["width"] / 2
        center_y = rect["top"] + rect["height"] / 2
        abs_x = max(
            rect["left"], min(center_x + offset_x, rect["left"] + rect["width"])
        )
        abs_y = max(
            rect["top"], min(center_y + offset_y, rect["top"] + rect["height"])
        )
        try:
            _dispatch_cdp_click_sequence(base, abs_x, abs_y, rng=rnd, jitter=True)
            return True
        except WebDriverException as exc:
            _log.debug(
                "popup XPath close: CDP dispatch failed on element: %s", exc
            )
        except Exception as exc:  # pylint: disable=broad-except
            _log.debug(
                "popup XPath close: CDP dispatch raised on element: %s", exc
            )
    return False


def handle_something_wrong_popup(
    driver, timeout: float = 2.0
) -> "PopupCloseOutcome":
    """Click Close on the 'Something went wrong' popup if present (Blueprint §6 Fork 3).

    P1-1: By default uses an XPath text-match locator so generic ``.modal`` /
    ``.popup`` elements (cookie banners, newsletter modals, success modals)
    without the target text do NOT trigger a false-positive close. Set env
    ``POPUP_USE_XPATH=0`` to roll back to the legacy CSS selector.

    P1-2: After a successful close the driver's card-number and CVV fields
    are wiped via :meth:`GivexDriver.clear_card_fields_cdp` so the stale
    card that triggered the error is not re-submitted on the next attempt.
    The return value switches from ``bool`` to :class:`PopupCloseOutcome`
    so the orchestrator retry loop can tell "no popup" apart from
    "popup closed — re-fill required". The enum is bool-compatible to
    preserve existing ``if handle_...():`` call sites. Set env
    ``POPUP_CLEAR_AFTER_CLOSE=0`` to disable the clear step.

    Retry: the close button is clicked up to ``POPUP_CLOSE_MAX_RETRIES``
    times (default 3, clamped to ``[1, 10]``) because the popup can
    re-render immediately after a single click. After each click we
    re-check presence with a short timeout; if the popup has gone we
    return ``CLOSED_NEEDS_REFILL``. If it is still present after the
    final attempt a warning is logged and ``CLOSE_FAILED`` is returned.
    """
    if WebDriverWait is None or EC is None or By is None:
        return PopupCloseOutcome.NOT_PRESENT
    base = getattr(driver, "_driver", driver)
    if _popup_use_xpath():
        locator = (By.XPATH, XPATH_POPUP_SWW)
    else:
        locator = (By.CSS_SELECTOR, SEL_POPUP_SOMETHING_WRONG)
    try:
        WebDriverWait(base, timeout).until(
            EC.presence_of_element_located(locator))
    except TimeoutException:
        return PopupCloseOutcome.NOT_PRESENT
    # Retry loop: the popup may re-appear immediately after a single
    # close click (animation race / re-render). Try up to N times and
    # log a warning if it's still present after the final attempt.
    max_retries = _popup_close_max_retries()
    closed = False
    last_exc: "Exception | None" = None
    for attempt in range(1, max_retries + 1):
        try:
            driver.bounding_box_click(SEL_POPUP_CLOSE)
        except SelectorTimeoutError as exc:
            # CSS close selector did not match — try the XPath text-match
            # fallback (button/anchor with Close/OK/X/Đóng text). On first
            # CSS miss we stop retrying CSS and let the fallback decide.
            _log.info(
                "popup CSS close missed (attempt %d/%d): %s — trying XPath fallback",
                attempt, max_retries, exc,
            )
            if _popup_xpath_click_close(driver):
                # D7 review follow-up: a successful CDP dispatch does NOT
                # guarantee the popup actually closed (the click may have
                # landed on an occluded/transformed element). Verify the
                # popup is gone before declaring success, matching the
                # CSS-path retry contract below.
                if not _popup_still_present(
                        base, locator, _POPUP_CLOSE_VERIFY_TIMEOUT):
                    closed = True
                else:
                    last_exc = exc
                    _log.warning(
                        "popup XPath close dispatched but popup still"
                        " present (attempt %d/%d)",
                        attempt, max_retries,
                    )
            else:
                last_exc = exc
            break
        except Exception as exc:  # pylint: disable=broad-except
            last_exc = exc
            _log.warning(
                "popup close failed (attempt %d/%d): %s",
                attempt, max_retries, exc,
            )
            continue
        # Re-check whether the popup is still present. If it has gone,
        # we are done; otherwise loop and click again.
        if not _popup_still_present(
                base, locator, _POPUP_CLOSE_VERIFY_TIMEOUT):
            closed = True
            break
        _log.warning(
            "popup still present after close attempt %d/%d — retrying",
            attempt, max_retries,
        )
    if not closed:
        if last_exc is not None:
            return PopupCloseOutcome.CLOSE_FAILED
        _log.warning(
            "popup still present after %d close attempts — giving up",
            max_retries,
        )
        return PopupCloseOutcome.CLOSE_FAILED
    if _popup_clear_after_close():
        clear = getattr(driver, "clear_card_fields_cdp", None)
        if callable(clear):
            try:
                clear()
            except Exception as exc:  # pylint: disable=broad-except
                # Never let a clear failure mask the close-success signal —
                # the orchestrator will attempt its own clear during the
                # swap path (P0-2 retry loop).
                _log.warning(
                    "clear_card_fields_cdp after popup close failed: %s", exc
                )
    return PopupCloseOutcome.CLOSED_NEEDS_REFILL


def _get_shadow_text(base_driver, selector: str) -> str:
    """Extract text content from an element, including shadow-DOM children.

    Uses JavaScript to traverse shadow roots and collect all visible text.

    Args:
        base_driver: Raw Selenium WebDriver instance.
        selector: CSS selector for the host element.

    Returns:
        Concatenated text content, empty string if element not found or JS fails.
    """
    js = """
(function(selector) {
    function collectText(node) {
        if (!node) return '';
        let text = '';
        if (node.shadowRoot) {
            text += collectText(node.shadowRoot);
        }
        if (node.childNodes) {
            for (let i = 0; i < node.childNodes.length; i++) {
                const child = node.childNodes[i];
                if (child.nodeType === 3) {
                    text += child.textContent || '';
                } else if (child.nodeType === 1) {
                    text += collectText(child);
                }
            }
        }
        return text;
    }
    const els = document.querySelectorAll(selector);
    let result = '';
    for (let j = 0; j < els.length; j++) {
        result += collectText(els[j]) + ' ';
    }
    return result;
})(arguments[0]);
"""
    try:
        return base_driver.execute_script(js, selector) or ""
    except Exception:  # pylint: disable=broad-except
        return ""


def check_popup_text_match(
    driver,
    patterns=None,
    *,
    selector: str = SEL_POPUP_SOMETHING_WRONG,
    shadow_root: bool = True,
) -> str | None:
    """Check whether any currently visible popup element contains a known error pattern.

    Supports multi-language matching (EN + VN) and optionally traverses shadow-DOM
    children to find text hidden behind a shadow root.

    Args:
        driver: GivexDriver wrapper or raw Selenium WebDriver.
        patterns: Tuple of lowercase substrings to match against popup text.  Falls
            back to :data:`POPUP_TEXT_PATTERNS_DEFAULT` when ``None``.
        selector: CSS selector used to locate candidate popup elements.
        shadow_root: When ``True`` (default) the match also scans text inside any
            shadow-DOM children attached to matched elements.

    Returns:
        The first matching pattern string if any popup text matches; ``None`` if no
        match is found or no popup is currently visible.
    """
    if patterns is None:
        patterns = POPUP_TEXT_PATTERNS_DEFAULT
    base = getattr(driver, "_driver", driver)

    # Collect raw text from the DOM (with or without shadow traversal)
    if shadow_root:
        raw_text = _get_shadow_text(base, selector)
    else:
        try:
            # By may be None when selenium is not installed (module-level try/except)
            elements = base.find_elements(By.CSS_SELECTOR, selector) if By is not None else []
        except Exception:  # pylint: disable=broad-except
            elements = []
        raw_text = " ".join(el.text for el in elements if el.text)

    normalised = raw_text.lower().strip()

    if not normalised:
        _log.debug("popup text-match: no popup text found (selector=%r)", selector)
        return None

    for pat in patterns:
        if pat in normalised:
            _log.debug("popup text-match: MATCH pattern=%r in popup text", pat)
            return pat

    _log.debug(
        "popup text-match: NO MATCH — popup present but none of %d patterns found "
        "(selector=%r, text_snippet=%r)",
        len(patterns),
        selector,
        normalised[:120],
    )
    return None


def detect_popup_thank_you(
    driver,
    *,
    patterns=None,
    shadow_root: bool = False,
    selector: str = SEL_CONFIRMATION_EL,
) -> bool:
    """Detect whether the current page shows a "Thank you" success confirmation.

    Checks both the page URL (for known confirmation URL fragments) and the
    visible body text of the page (for localised success phrases in EN/VN).
    Optionally traverses shadow-DOM children of a confirmation element when
    ``shadow_root=True`` (P1-3 coverage).

    This function is used as the trigger for the P1-2 clear/refill workflow:
    after a "Thank you" confirmation is detected, the orchestrator clears
    card fields and refills from the next order in the queue.

    Args:
        driver: GivexDriver wrapper or raw Selenium WebDriver.
        patterns: Tuple of lowercase substrings to match against page text.
            Falls back to :data:`THANK_YOU_TEXT_PATTERNS_DEFAULT` when ``None``.
        shadow_root: When ``True``, also scan text hidden inside shadow-DOM
            children of elements matched by ``selector``.  Defaults to
            ``False`` to preserve existing behaviour.
        selector: CSS selector used for shadow-DOM traversal when
            ``shadow_root=True``.  Defaults to
            :data:`SEL_CONFIRMATION_EL`.

    Returns:
        ``True`` if the page URL contains a confirmation fragment, the page
        body text contains a known thank-you pattern, or (when
        ``shadow_root=True``) the shadow-DOM subtree contains a known
        pattern; ``False`` otherwise.
    """
    if patterns is None:
        patterns = THANK_YOU_TEXT_PATTERNS_DEFAULT
    base = getattr(driver, "_driver", driver)

    # 1 — URL-based detection (fastest signal)
    try:
        current_url = base.current_url or ""
        if any(frag in current_url for frag in URL_CONFIRM_FRAGMENTS):
            _log.debug("detect_popup_thank_you: URL match (%r)", current_url)
            return True
    except Exception:  # pylint: disable=broad-except
        _log.debug(
            "detect_popup_thank_you: current_url access failed; falling through to text check",
            exc_info=True,
        )

    # 2 — Page body text detection
    try:
        body_text = base.find_element("tag name", "body").text.lower()
    except Exception:  # pylint: disable=broad-except
        body_text = ""

    for pat in patterns:
        if pat in body_text:
            _log.debug("detect_popup_thank_you: body text MATCH pattern=%r", pat)
            return True

    # 3 — Shadow-DOM traversal (optional, P1-3)
    if shadow_root:
        shadow_text = _get_shadow_text(base, selector).lower()
        for pat in patterns:
            if pat in shadow_text:
                _log.debug(
                    "detect_popup_thank_you: shadow-DOM MATCH pattern=%r selector=%r",
                    pat,
                    selector,
                )
                return True

    _log.debug("detect_popup_thank_you: no thank-you signal found")
    return False


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
        # Per-cycle de-dup flag for preflight_geo_check: set to True after a
        # successful geo-check so that downstream callers (orchestrator
        # ``run_preflight_and_fill``, ``run_full_cycle``) can skip the
        # redundant second call.  See issue: geo-check should run immediately
        # after BitBrowserSession.__enter__ (not inside run_preflight_and_fill).
        self._geo_checked_this_cycle: bool = False
        # E3 audit: captured Phase A pricing total (watchdog/preflight) used by
        # :meth:`submit_purchase` to cross-check the on-page Order Total via
        # DOM right before clicking COMPLETE PURCHASE.  ``None`` means the
        # orchestrator has not wired the expected total yet — verification is
        # then skipped (preserves legacy callers and unit tests that drive
        # ``submit_purchase`` directly without a Phase A capture).
        self._expected_total: decimal.Decimal | None = None
        if persona is not None and _BehaviorStateMachine is not None:
            # Phase 5A Task 1: prefer the SM published by the behaviour
            # wrapper (via :func:`modules.delay.state.set_current_sm`)
            # so transitions and critical-section flips applied here
            # affect the same instance the delay engine consults.
            shared_sm = _get_current_sm() if _get_current_sm is not None else None
            self._sm = shared_sm if shared_sm is not None else _BehaviorStateMachine()
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

    def _find_vbv_cancel_button(self):
        """Locate a VBV/3DS cancel button using priority-ordered selectors.

        Iterates :data:`SEL_VBV_CANCEL_BUTTONS` in order and returns the first
        selector whose ``find_elements`` query is non-empty.  Returns
        ``(element, selector)`` on success, or ``(None, None)`` when no
        candidate matches (Phase 4 audit [D6]).

        Note: this queries the *current* document/frame context — the caller
        is responsible for having switched into the VBV iframe beforehand if
        the cancel button lives there.
        """
        for sel in SEL_VBV_CANCEL_BUTTONS:
            try:
                elements = self.find_elements(sel)
            except Exception:  # pylint: disable=broad-except
                continue
            if elements:
                _log.debug("VBV cancel matched selector: %s", sel)
                return elements[0], sel
        return None, None

    def handle_vbv_challenge(self) -> str:
        """Cancel VBV/3DS iframe challenge (Blueprint §6 Fork 3).

        Returns:
            'cancelled'       — successfully cancelled the 3DS challenge.
            'iframe_missing'  — benign; no iframe to cancel (likely already gone).
            'cdp_fail'        — CDP/WebDriver error; caller may retry.
            'error'           — other unexpected error; caller decides.
        """
        # Phase 5A Tasks 2B / 3: transition into VBV (audit [C2]) and arm
        # the CRITICAL_SECTION flag for the duration of the iframe
        # interaction so the delay layer skips any injection.  Surface
        # rejected transitions as a WARNING (review fix [F3]) so a stale
        # FSM state — e.g. POST_ACTION already set by submit_purchase —
        # is observable instead of silently swallowed; the CS flag still
        # applies regardless so iframe interaction stays delay-safe.
        result: str = "error"
        if self._sm is not None:
            if not self._sm.transition("VBV"):
                _log.warning(
                    "handle_vbv_challenge: SM rejected VBV transition from %s",
                    self._sm.get_state(),
                )
            self._sm.enter_critical_zone("vbv_iframe")
        try:
            try:
                vbv_dynamic_wait(rng=self._get_rng())
                # Phase 4 audit [D6]: iterate SEL_VBV_CANCEL_BUTTONS in priority
                # order (Cancel → Return → Close → X-icon).  First selector whose
                # CDP click succeeds wins; remaining selectors are not attempted.
                last_exc: Exception | None = None
                for sel in SEL_VBV_CANCEL_BUTTONS:
                    try:
                        cdp_click_iframe_element(
                            self, SEL_VBV_IFRAME, sel, rng=self._get_rng(),
                        )
                        _log.debug("VBV cancel clicked via selector: %s", sel)
                        break
                    except (NoSuchElementException, StaleElementReferenceException) as exc:
                        last_exc = exc
                        continue
                else:
                    if last_exc is not None:
                        raise last_exc
                handle_something_wrong_popup(self)
                result = "cancelled"
                return result
            except (NoSuchElementException, StaleElementReferenceException) as exc:
                _log.info("handle_vbv_challenge iframe missing: %s", _sanitize_error(str(exc)))
                result = "iframe_missing"
                return result
            except WebDriverException as exc:
                _log.warning("handle_vbv_challenge CDP fail: %s", _sanitize_error(str(exc)))
                result = "cdp_fail"
                return result
            except Exception as exc:  # pylint: disable=broad-except
                _log.error("handle_vbv_challenge unexpected: %s", _sanitize_error(str(exc)))
                result = "error"
                return result
        finally:
            if self._sm is not None:
                self._sm.exit_critical_zone()
                # Review fix [F2]: only advance to POST_ACTION when the
                # iframe was successfully cancelled.  On `iframe_missing`
                # / `cdp_fail` / `error` the FSM must remain in its
                # current (e.g. VBV / PAYMENT) state so future delay-
                # permission checks reflect the real progress through the
                # checkout flow rather than a forced post-submit state.
                if result == "cancelled":
                    if not self._sm.transition("POST_ACTION"):
                        _log.warning(
                            "handle_vbv_challenge: SM rejected POST_ACTION transition from %s",
                            self._sm.get_state(),
                        )

    # ── Low-level helpers ────────────────────────────────────────────────────

    def _get_rng(self):
        """Return the persona RNG or a SystemRandom fallback."""
        if self._rnd is not None:
            return self._rnd
        import random as _random  # noqa: PLC0415
        return _random.SystemRandom()

    def _engine_aware_sleep(self, low: float, high: float, reason: str) -> float:
        rnd = self._get_rng()
        low, high = (high, low) if high < low else (low, high)
        requested = max(0.0, min(float(rnd.uniform(low, high)), float(_MAX_STEP_DELAY)))
        if self._engine is not None:
            if not self._engine.is_delay_permitted():
                requested = 0.0
            else:
                requested = self._engine.accumulate_delay(requested)
        _log.debug("engine_aware_sleep: reason=%s delay=%.3f", reason, requested)
        if requested > 0:
            time.sleep(requested)
        return requested if requested > 0 else 0.0

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

    def _is_interactable(self, elem) -> bool:
        try:
            if not elem.is_displayed():
                return False
        except Exception:  # pylint: disable=broad-except
            return False
        try:
            if not elem.is_enabled():
                return False
        except Exception:  # pylint: disable=broad-except
            pass
        js = ("const e=arguments[0],s=getComputedStyle(e),r=e.getBoundingClientRect();"
              "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'"
              "&&s.pointerEvents!=='none'&&e.getAttribute('aria-disabled')!=='true'&&!e.disabled;")
        try:
            return bool(self._driver.execute_script(js, elem))
        except Exception:  # pylint: disable=broad-except
            return False

    def _wait_for_interactable(self, selector: str, timeout: int = 10) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elements = self.find_elements(selector)
            for elem in elements:
                try:
                    if self._is_interactable(elem):
                        return True
                except (StaleElementReferenceException, NoSuchElementException) as exc:
                    _log.debug(
                        "_wait_for_interactable: ignored transient interactability error for %s: %s",
                        _selector_name(selector),
                        _sanitize_error(str(exc)),
                    )
            time.sleep(0.5)
        return False

    def _field_value_length(self, selector: str) -> int:
        js = "const el=document.querySelector(arguments[0]);return el&&typeof el.value==='string'?el.value.length:-1;"
        try:
            return int(self._driver.execute_script(js, selector))
        except Exception:  # pylint: disable=broad-except
            return -1

    def _field_value(self, selector: str) -> str | None:
        """Read element value via JS. Callers must never log this PII-bearing value."""
        try:
            value = self._driver.execute_script(
                "const el=document.querySelector(arguments[0]);"
                "return el&&typeof el.value==='string'?el.value:null;",
                selector,
            )
            return value if isinstance(value, str) else None
        except Exception:  # pylint: disable=broad-except
            return None

    def _form_validation_diagnostics(self) -> dict:
        js = 'const known=new Map([[arguments[0],"SEL_GREETING_MSG"],[arguments[1],"SEL_AMOUNT_INPUT"],[arguments[2],"SEL_RECIPIENT_NAME"],[arguments[3],"SEL_RECIPIENT_EMAIL"],[arguments[4],"SEL_CONFIRM_RECIPIENT_EMAIL"],[arguments[5],"SEL_SENDER_NAME"]]);const sym=(el)=>{for(const [sel,name] of known.entries()){try{if(el.matches(sel))return name;}catch(e){}}return null;};const desc=(el)=>{const v=el.validity||{},value=typeof el.value==="string"?el.value:"",msg=typeof el.validationMessage==="string"?el.validationMessage:"";return {selector_name:sym(el),tag:(el.tagName||"").toLowerCase(),type:el.getAttribute("type")||"",id_len:(el.id||"").length,name_len:(el.getAttribute("name")||"").length,value_len:value.length,validity:{valid:Boolean(v.valid),valueMissing:Boolean(v.valueMissing),typeMismatch:Boolean(v.typeMismatch),patternMismatch:Boolean(v.patternMismatch),rangeUnderflow:Boolean(v.rangeUnderflow),rangeOverflow:Boolean(v.rangeOverflow),tooShort:Boolean(v.tooShort),tooLong:Boolean(v.tooLong),customError:Boolean(v.customError)},validationMessage_len:msg.length};};return {forms:Array.from(document.forms||[]).map((form)=>({checkValidity:(()=>{try{return Boolean(form.checkValidity())}catch(e){return false}})(),elements_length:(form.elements||[]).length,elements:Array.from(form.elements||[]).map(desc)}))};'
        try:
            data = self._driver.execute_script(js, SEL_GREETING_MSG, SEL_AMOUNT_INPUT, SEL_RECIPIENT_NAME, SEL_RECIPIENT_EMAIL, SEL_CONFIRM_RECIPIENT_EMAIL, SEL_SENDER_NAME)
            return data if isinstance(data, dict) else {"forms": []}
        except Exception: return {"forms": []}  # noqa: E701  # pylint: disable=broad-except

    def _review_checkout_diagnostics(self) -> dict:
        """Return PII-safe diagnostics for the Add-to-Cart → Review Checkout handoff.

        The returned mapping contains ``cookie_count``, storage lengths, and
        structural-only element snapshots for ``add_to_cart_span``,
        ``add_to_cart_parent``, and ``review_checkout``.  Element snapshots log
        booleans, style flags, dimensions, and text/class lengths only.
        """
        cookie_count = -1
        try:
            cookie_count = len(self._driver.get_cookies())
        except Exception:  # pylint: disable=broad-except
            _log.debug("review_checkout_diagnostics: cookie count unavailable", exc_info=True)
        try:
            data = self._driver.execute_script(
                """
                const classLength = (el) => {
                    const rawClass = el.className;
                    if (rawClass && typeof rawClass.baseVal === "string") {
                        return rawClass.baseVal.length;
                    }
                    if (typeof rawClass === "string") {
                        return rawClass.length;
                    }
                    const classAttr = el.getAttribute("class");
                    return typeof classAttr === "string" ? classAttr.length : 0;
                };
                const describe = (el) => {
                    if (!el) {
                        return {
                            present: false, enabled: false, disabled: null, aria_disabled: null,
                            pointer_events: null, display: null, visibility: null,
                            rect_w: 0, rect_h: 0, text_len: 0, class_len: 0
                        };
                    }
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const text = typeof el.innerText === "string" ? el.innerText : "";
                    const visible = rect.width > 0 && rect.height > 0
                        && style.display !== "none" && style.visibility !== "hidden";
                    return {
                        present: true,
                        enabled: !el.disabled && el.getAttribute("aria-disabled") !== "true"
                            && style.pointerEvents !== "none" && visible,
                        disabled: Boolean(el.disabled),
                        aria_disabled: el.getAttribute("aria-disabled"),
                        pointer_events: style.pointerEvents,
                        display: style.display,
                        visibility: style.visibility,
                        rect_w: Math.round(rect.width),
                        rect_h: Math.round(rect.height),
                        text_len: text.length,
                        class_len: classLength(el)
                    };
                };
                const addToCartSpan = document.querySelector(arguments[0]);
                const addToCartParent = addToCartSpan
                    ? addToCartSpan.closest('button,a,[role="button"],.btn')
                    : null;
                const reviewCheckout = document.querySelector(arguments[1]);
                const visible = (el) => { const s = window.getComputedStyle(el), r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden"; };
                const totalLike = document.querySelector('[class*="total"],[id*="total"]');
                const explicitCartLineItems = document.querySelectorAll('[class*="cart-item"],[class*="lineItem"],[class*="product"][class*="row"]');
                return {
                    localStorage_length: (() => {
                        try {
                            return window.localStorage ? window.localStorage.length : -1;
                        } catch (e) {
                            // PII-safe sentinel for restricted storage contexts.
                            return -1;
                        }
                    })(),
                    sessionStorage_length: (() => {
                        try {
                            return window.sessionStorage ? window.sessionStorage.length : -1;
                        } catch (e) {
                            // PII-safe sentinel for restricted storage contexts.
                            return -1;
                        }
                    })(),
                    add_to_cart_span: describe(addToCartSpan),
                    add_to_cart_parent: describe(addToCartParent),
                    review_checkout: describe(reviewCheckout),
                    cart_like_count: document.querySelectorAll('[class*="cart"],[id*="cart"]').length,
                    cart_like_visible_count: Array.from(document.querySelectorAll('[class*="cart"],[id*="cart"]')).filter(visible).length,
                    explicit_cart_line_item_count: explicitCartLineItems.length,
                    explicit_cart_line_item_visible_count: Array.from(explicitCartLineItems).filter(visible).length,
                    error_like_count: document.querySelectorAll('[class*="error"],[role="alert"]').length,
                    error_like_visible_count: Array.from(document.querySelectorAll('[class*="error"],[role="alert"]')).filter(visible).length,
                    total_like_present: Boolean(totalLike),
                    total_like_text_len: totalLike && typeof totalLike.innerText === "string" ? totalLike.innerText.length : 0
                };
                """,
                SEL_ADD_TO_CART,
                SEL_REVIEW_CHECKOUT,
            )
            if isinstance(data, dict):
                data["cookie_count"] = cookie_count
                data["form_validation"] = self._form_validation_diagnostics()
                return data
        except Exception:  # pylint: disable=broad-except
            _log.debug("review_checkout_diagnostics: DOM snapshot unavailable", exc_info=True)
        return {
            "cookie_count": cookie_count, "localStorage_length": -1,
            "sessionStorage_length": -1, "add_to_cart_span": {},
            "add_to_cart_parent": {}, "review_checkout": {},
        }

    def _log_review_checkout_diagnostics(self) -> None:
        data = self._review_checkout_diagnostics()
        logged_keys = {"cookie_count", "localStorage_length", "sessionStorage_length", "add_to_cart_span", "add_to_cart_parent", "review_checkout"}  # fields expanded in log slots below
        extra = {k: v for k, v in data.items() if k not in logged_keys}
        _log.error(
            "add_to_cart_and_checkout: Review-Checkout diagnostics "
            "cookie_count=%s localStorage.length=%s sessionStorage.length=%s "
            "add_to_cart_span=%s add_to_cart_parent=%s review_checkout=%s extra=%s",
            data.get("cookie_count"),
            data.get("localStorage_length"),
            data.get("sessionStorage_length"),
            data.get("add_to_cart_span"),
            data.get("add_to_cart_parent"),
            data.get("review_checkout"),
            extra,
        )

    def _verify_field_value_length(self, sel: str, expected_len: int, selector_name: str) -> None:
        actual_len = self._field_value_length(sel)
        _log.info("_realistic_type_field: field=%s expected_len=%d actual_len=%d", selector_name, expected_len, actual_len)
        if actual_len < 0:
            self._capture_failure_screenshot(f"type_field_unreadable_{selector_name}")
            raise SessionFlaggedError(
                f"Field {selector_name} value unreadable (JS read failed); expected_len={expected_len}"
            )
        if expected_len <= 0:
            return
        if sel == SEL_AMOUNT_INPUT:
            failed, label = actual_len <= 0, "empty"
        else:
            failed, label = actual_len < max(1, int(expected_len * 0.7)), "short"
        if failed:
            self._capture_failure_screenshot(f"type_field_value_{label}_{selector_name}")
            raise SessionFlaggedError(f"Field {selector_name} did not receive typed value (expected_len={expected_len} actual_len={actual_len})")

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
        last_url = last_non_empty_url = ""
        transitions = 0
        expected_short = _short_url(url_fragment)
        started = time.monotonic()
        while time.monotonic() < deadline:
            current = ""
            try:
                current = self._driver.current_url
            except Exception:  # URL briefly unavailable during page transition
                _log.debug("URL check deferred: page transition in progress")
            if current != last_url:
                if current:
                    transitions += 1
                    _log.info(
                        "_wait_for_url[expecting=%s]: URL transitioned to %s (transition #%d, t+%.1fs)",
                        expected_short, _sanitize_url_for_log(current), transitions, time.monotonic() - started,
                    )
                    last_non_empty_url = current
                last_url = current
            if url_fragment in current:
                _log.info(
                    "_wait_for_url[%s]: matched after %d transitions, %.1fs elapsed",
                    expected_short, transitions, time.monotonic() - started,
                )
                return
            if self._detect_givex_submission_error_popup():
                closed = self._close_givex_submission_error_popup()
                try:
                    current = self._driver.current_url
                except Exception:
                    current = ""
                if closed and url_fragment in current:
                    return
                raise _SubmissionErrorPopupDetected(popup_closed=closed)
            time.sleep(0.5)
        raise PageStateError(f"url_wait expected={expected_short} last_seen={_sanitize_url_for_log(last_non_empty_url)} transitions={transitions}")

    def _detect_givex_submission_error_popup(self) -> bool:
        """Return True if visible Fancybox modal contains submission error text."""
        try:
            return self._driver.execute_script(
                """
                var el = document.querySelector('.fancybox-wrap.fancybox-opened');
                if (!el || el.offsetParent === null) { return false; }
                return /something went wrong/i.test(el.innerText || '');
                """
            ) is True
        except Exception as exc:  # pylint: disable=broad-except
            _log.debug(
                "GIVEX_SUBMISSION_ERROR_POPUP detect skipped js_error_type=%s",
                type(exc).__name__,
            )
            return False

    def _is_givex_fancybox_open(self) -> bool:
        try:
            return bool(self._driver.execute_script(
                "return !!document.querySelector('.fancybox-wrap.fancybox-opened');"
            ))
        except Exception as exc:  # pylint: disable=broad-except
            _log.debug(
                "GIVEX_FANCYBOX_CLOSE verify skipped js_error_type=%s",
                type(exc).__name__,
            )
            # Fail as "still open" so a verification error cannot be mistaken
            # for a successful dismissal.
            return True

    def _wait_for_givex_fancybox_closed(
        self, max_wait: float = _GIVEX_FANCYBOX_CLOSE_VERIFY_S,
    ) -> bool:
        deadline = time.monotonic() + max_wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._is_givex_fancybox_open():
                return True
            time.sleep(min(0.2, remaining))
        return not self._is_givex_fancybox_open()

    def _close_givex_submission_error_popup(self) -> bool:
        """Click Fancybox close button via existing CDP click path. Returns True on success."""
        selectors = (
            ".fancybox-wrap.fancybox-opened a.fancybox-item.fancybox-close",
            "a.fancybox-close",
            ".fancybox-close",
            ".fancybox-item.fancybox-close",
        )
        deadline = time.monotonic() + _GIVEX_FANCYBOX_CLOSE_TOTAL_BUDGET_S
        for selector_index, selector in enumerate(selectors):
            if time.monotonic() >= deadline:
                break
            for attempt in range(_GIVEX_FANCYBOX_CLICK_ATTEMPTS):
                if time.monotonic() >= deadline:
                    break
                try:
                    self.bounding_box_click(selector)
                except Exception:  # pylint: disable=broad-except
                    _log.debug(
                        "GIVEX_FANCYBOX_CLOSE click failed selector_index=%d attempt=%d",
                        selector_index, attempt + 1,
                    )
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._wait_for_givex_fancybox_closed(
                    max_wait=min(_GIVEX_FANCYBOX_CLOSE_VERIFY_S, remaining),
                ):
                    _log.info("GIVEX_FANCYBOX_CLOSE dismissed via click")
                    return True
                _log.debug(
                    "GIVEX_FANCYBOX_CLOSE persisted after click selector_index=%d attempt=%d",
                    selector_index, attempt + 1,
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _log.warning("GIVEX_FANCYBOX_CLOSE close budget exhausted before Escape")
            return False
        _log.warning("GIVEX_FANCYBOX_CLOSE click retries failed; dispatching Escape")
        try:
            if _dispatch_key is None:
                _log.warning("GIVEX_FANCYBOX_CLOSE Escape dispatch unavailable")
            elif not _dispatch_key(self._driver, "Escape"):
                _log.warning("GIVEX_FANCYBOX_CLOSE Escape dispatch returned_false")
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning(
                "GIVEX_FANCYBOX_CLOSE Escape dispatch exception_type=%s",
                type(exc).__name__,
            )
        remaining = deadline - time.monotonic()
        if remaining > 0 and self._wait_for_givex_fancybox_closed(
            max_wait=min(_GIVEX_FANCYBOX_CLOSE_VERIFY_S, remaining),
        ):
            _log.info("GIVEX_FANCYBOX_CLOSE dismissed via Escape")
            return True
        _log.warning("GIVEX_FANCYBOX_CLOSE popup persisted after click and Escape")
        return False

    def _capture_failure_screenshot(self, label: str) -> None:
        """Best-effort failure PNG capture; never raises."""
        if not _failure_screenshot_enabled():
            return
        try:
            from modules.notification.screenshot_blur import capture_blurred_only  # noqa: PLC0415
            from pathlib import Path  # noqa: PLC0415

            png = capture_blurred_only(self._driver)
            if png is None:
                if not _failure_screenshot_allow_raw():
                    _log.warning(
                        "Failure screenshot skipped (Pillow missing or blur failed). "
                        "FAILURE_SCREENSHOT_ALLOW_RAW=1 is local-debug only and NOT production-safe."
                    )
                    return
                _log.warning(
                    "FAILURE_SCREENSHOT_ALLOW_RAW=1 — saving RAW screenshot "
                    "(PRIVACY RISK: PII may be readable). Local-debug only."
                )
                try:
                    png = self._driver.get_screenshot_as_png()
                except Exception:
                    return
                if not png:
                    return
            outdir = Path(_failure_screenshot_dir())
            outdir.mkdir(parents=True, exist_ok=True)
            path = outdir / f"{label}_{int(time.time())}_{os.getpid()}.png"
            path.write_bytes(png)
            _log.error("Failure screenshot saved: %s", path)
        except Exception:
            _log.warning("Failure screenshot capture failed", exc_info=True)

    def _wait_for_url_or_capture(self, url_fragment: str, label: str, timeout: int = 15) -> None:
        try:
            self._wait_for_url(url_fragment, timeout=timeout)
        except PageStateError:
            self._capture_failure_screenshot(label)
            raise

    def _cdp_type_field(self, selector: str, value: str) -> None:
        """DEPRECATED: use ``_realistic_type_field(field_kind="text")`` instead.

        This legacy helper bypassed CDP ``Input.dispatchKeyEvent`` and emitted
        Selenium-native ``send_keys`` (``isTrusted=False``).  All §5 production
        call-sites have been migrated to :meth:`_realistic_type_field`, which
        routes through :func:`modules.cdp.keyboard.type_value`.

        In strict mode (``ENFORCE_CDP_TYPING_STRICT=1``, the default for
        production), this method raises :class:`RuntimeError` so a stray
        regression cannot silently downgrade anti-fraud quality on a
        production hot-path.  In non-strict mode it emits a
        :class:`DeprecationWarning` and falls back to ``send_keys`` so legacy
        tests that patch this method continue to work.

        Args:
            selector: CSS selector for the input/textarea element.
            value: Text to type.

        Raises:
            RuntimeError: in strict mode (production hot-path enforcement).
            SelectorTimeoutError: if no matching element is found.
        """
        if os.environ.get("ENFORCE_CDP_TYPING_STRICT", "1") == "1":
            raise RuntimeError(
                "_cdp_type_field called in strict mode; "
                "all text fields must route through _realistic_type_field"
            )
        warnings.warn(
            "_cdp_type_field is deprecated; use _realistic_type_field",
            DeprecationWarning,
            stacklevel=2,
        )
        self._send_keys_fallback(selector, value)

    def _send_keys_fallback(self, selector: str, value: str) -> None:
        """Internal Selenium ``send_keys`` fallback.

        Used only when the CDP keyboard module (``_type_value``) is
        unavailable at import time — never on the production hot-path.
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)
        el = elements[0]
        try:
            el.clear()
        except Exception:  # clear() is best-effort; send_keys still runs
            _log.debug("Element clear() skipped in _send_keys_fallback")
        el.send_keys(value)

    def _realistic_type_field(
            self, sel, val, *, use_burst=False,
            field_kind="text", typo_rate=None,
    ):
        els = self.find_elements(sel)
        if not els: raise SelectorTimeoutError(sel, 0)  # noqa: E701
        selector_name = _selector_name(sel)
        expected_len = len(str(val))
        # IMPORTANT: To enable focus-before-type + length verification on a NEW field,
        # add its selector + symbolic name to the module-level _SELECTOR_NAMES registry
        # defined immediately after SEL_GUEST_EMAIL. Unregistered fields keep legacy
        # no-verify typing.
        verify_value = sel in _SELECTOR_NAMES
        if verify_value:
            self._human_scroll_to(sel)
            self._wait_scroll_stable()
            self._engine_aware_sleep(0.08, 0.20, "pre_focus_pause")
            self.bounding_box_click(sel)
            self._engine_aware_sleep(0.08, 0.25, "post_focus_pause")
        if _type_value is None:
            if self._strict:
                _log.warning("_realistic_type_field: keyboard unavailable (strict)")
            self._send_keys_fallback(sel, val)
            if verify_value:
                self._verify_field_value_length(sel, expected_len, selector_name)
                self._engine_aware_sleep(0.08, 0.25, "post_type_pause")
            return
        typo_prob = self._persona.get_typo_probability() if self._persona else 0.0
        # Apply explicit ``typo_rate`` override first (callers who pass an
        # explicit rate own the rate exactly — no NIGHT bonus is layered on
        # top of an explicit override). The critical-section guard below is
        # still authoritative: an explicit rate cannot re-enable typo
        # injection in a safe-zone contract.
        if typo_rate is not None:
            typo_prob = typo_rate
            apply_night_bonus = False
        else:
            apply_night_bonus = True
        # Phase 10 §10 / audit [L3]: NIGHT typo bonus is gated by the engine's
        # delay-permitted check so that VBV / POST_ACTION / Phase-9
        # CRITICAL_SECTION never see *any* typo behaviour modulation. The
        # critical-section guard is applied *after* the explicit ``typo_rate``
        # override so it is authoritative: callers cannot re-enable typo
        # injection in a safe-zone contract by passing ``typo_rate``. This
        # also zeroes the persona base rate while in critical section, since
        # typo injection is itself a behaviour modulation that must not fire
        # in safe-zone contracts.
        delay_permitted = (
            self._engine is None or self._engine.is_delay_permitted()
        )
        if not delay_permitted:
            typo_prob = 0.0
        elif apply_night_bonus and self._persona and self._temporal:
            typo_prob += self._temporal.get_night_typo_increase(self._utc_offset_hours)
        # Clamp into a valid probability range after the additive NIGHT bonus.
        typo_prob = max(0.0, min(1.0, typo_prob))
        # Phase 5A Task 4 / audit [J2]: when the engine reports that
        # delay is not permitted (e.g. CRITICAL_SECTION active, VBV /
        # POST_ACTION state, or accumulator exhausted), skip the
        # biometric pattern generation entirely and fall through to
        # deterministic fast typing.  The keyboard layer also zeroes
        # per-keystroke sleeps via ``engine.is_delay_permitted``; this
        # extra guard ensures the biometric RNG advance is also avoided
        # so the production path matches the safe-zone contract.
        if self._engine is not None and not self._engine.is_delay_permitted():
            dl = None
        elif self._bio and use_burst and len(val) >= 16:
            dl = self._bio.generate_4x4_pattern()
        elif self._bio:
            dl = self._bio.generate_burst_pattern(len(val))
        else:
            dl = None
        _type_value(
            self._driver, els[0], val, self._get_rng(),
            typo_rate=typo_prob, delays=dl, strict=self._strict,
            field_kind=field_kind, engine=self._engine,
        )
        if verify_value:
            self._verify_field_value_length(sel, expected_len, selector_name)
            self._engine_aware_sleep(0.08, 0.25, "post_type_pause")

    def _cdp_select_option(self, selector: str, value: str) -> None:
        """CDP-keynav to exact value/text or normalized numeric/month/year option."""
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)

        # Step 1 — open/focus the dropdown via a real CDP mouse click.
        self.bounding_box_click(selector)

        # Step 2 — read option metadata, then locate the target index in Python.
        js = (
            "const sel = document.querySelector(arguments[0]);"
            "if (!sel) return [-1, '', false, -1];"
            "const opts = Array.from(sel.options);"
            "const currentIdx = sel.selectedIndex;"
            "return [currentIdx, sel.value || '', !!sel.disabled, "
            "opts.map(o => ({value: o.value, text: (o.textContent || o.innerText || '')}))];"
        )
        result = self._driver.execute_script(js, selector)
        try:
            current_idx = int(result[0])
            current_value = "" if result[1] is None else str(result[1])
            disabled = bool(result[2])
            options = result[3]
            if not isinstance(options, list):
                raise TypeError("option metadata is not a list")
        except (TypeError, ValueError, IndexError) as exc:
            result_len = len(result) if isinstance(result, (list, tuple)) else None
            result_items = result[:2] if isinstance(result, (list, tuple)) else ()
            item_types = [type(item).__name__ for item in result_items]
            raise ValueError(
                f"_cdp_select_option: unexpected option metadata result "
                f"type={type(result).__name__} len={result_len} "
                f"item_types={item_types} selector={_selector_name(selector)}"
            ) from exc
        try:
            target_idx = _find_matching_option_index(selector, value, options)
        except ValueError as exc:
            _raise_option_not_found(
                selector,
                "" if value is None else str(value),
                options,
                selected_index=current_idx,
                current_value=current_value,
                disabled=disabled,
            )

        # Step 3 — keyboard-navigate from current to target.
        from modules.cdp.keyboard import dispatch_key  # local import: avoid cycle
        if current_idx < 0:
            # No prior selection — ArrowDown lands on the first option (index 0),
            # so we still need ``target_idx`` ArrowDowns after that initial step.
            steps = target_idx + 1
            key = "ArrowDown"
        else:
            delta = target_idx - current_idx
            steps = abs(delta)
            key = "ArrowDown" if delta >= 0 else "ArrowUp"
        for _ in range(steps):
            # Audit finding [F1]: ``dispatch_key`` returns False on CDP
            # failure.  We MUST NOT silently continue (and especially must
            # not dispatch the confirming Enter) — that would leave the
            # dropdown unchanged while the caller believes selection
            # succeeded, breaking the anti-detect contract for [G2].
            if not dispatch_key(self._driver, key):
                raise CDPCommandError(
                    "Input.dispatchKeyEvent",
                    f"_cdp_select_option: failed to dispatch {key!r} for {_selector_name(selector)}",
                )
            jitter = self._rnd.uniform(0.0, 0.05) if self._rnd is not None else 0.0
            time.sleep(0.05 + jitter)

        # Step 4 — confirm the selection.  Same hardening: a failed Enter
        # leaves the highlight unchanged and must surface to the caller.
        if not dispatch_key(self._driver, "Enter"):
            raise CDPCommandError(
                "Input.dispatchKeyEvent",
                f"_cdp_select_option: failed to dispatch Enter for {_selector_name(selector)}",
            )

    def _wait_for_select_options(
        self,
        selector: str,
        min_options: int = 2,
        timeout: float = 8.0,
        target_value: str | None = None,
    ) -> None:
        """Poll until the dropdown has min_options and target_value is matchable."""
        deadline = time.monotonic() + timeout
        last_count = -1
        last_target_present = False

        while time.monotonic() < deadline:
            try:
                result = self._driver.execute_script(
                    "const el=document.querySelector(arguments[0]);"
                    "if (!el || !el.options) return [0, []];"
                    "return [el.options.length, "
                    "Array.from(el.options).map(o => ({"
                    "value: o.value, text: (o.textContent || o.innerText || '')"
                    "}))];",
                    selector,
                )
                count = int(result[0]) if result else 0
                options = result[1] if (result and len(result) > 1) else []
                last_count = count
                if count >= min_options:
                    if target_value is None:
                        return
                    try:
                        _find_matching_option_index(selector, target_value, options)
                        return
                    except ValueError:
                        last_target_present = False
            except (WebDriverException, TypeError, ValueError) as exc:
                _log.debug(
                    "_wait_for_select_options: transient poll error selector=%s error_type=%s",
                    _selector_name(selector), type(exc).__name__,
                )
            time.sleep(0.25)

        raise SelectorTimeoutError(
            selector,
            timeout,
            reason=(
                f"select options did not reach {min_options} "
                f"or target_value not present; last_count={last_count}, "
                f"target_value_present={last_target_present}"
            ),
        )

    def _wait_scroll_stable(self, timeout: float = 2.0, stable_ms: float | None = None) -> bool:
        stable_window = (stable_ms if stable_ms is not None else self._get_rng().uniform(350, 600)) / 1000.0
        deadline, last, stable_since = time.monotonic() + timeout, None, None
        while time.monotonic() < deadline:
            try:
                current = tuple(self._driver.execute_script(
                    "return [window.scrollY||0,document.documentElement?document.documentElement.scrollTop||0:0,"
                    "document.body?document.body.scrollTop||0:0];"
                ))
            except Exception: current = None  # noqa: E701  # pylint: disable=broad-except
            now = time.monotonic()
            if current is not None and current == last:
                stable_since = now if stable_since is None else stable_since
                if now - stable_since >= stable_window:
                    return True
            else:
                last, stable_since = current, now if current is not None else None
            time.sleep(0.1)  # raw poll cadence; not behavioral pacing/DelayEngine budget
        _log.warning("_wait_scroll_stable: timeout after %.2fs", timeout)
        return False

    def _blur_active_field_naturally(self) -> bool:
        active_js = "const el=document.activeElement;return el?{tag:(el.tagName||'').toLowerCase(),id_len:(el.id||'').length}:null;"
        def active_snapshot():
            try:
                data = self._driver.execute_script(active_js)
            except Exception: return None  # noqa: E701  # pylint: disable=broad-except
            return data if isinstance(data, dict) else None

        before = active_snapshot()
        changed = False
        from modules.cdp.keyboard import dispatch_key  # local import: avoid cycle
        if dispatch_key(self._driver, "Tab"):
            changed = (after := active_snapshot()) is not None and after != before
        if not changed:
            try:
                point = self._driver.execute_script(
                    "const unsafe=(el)=>{if(!el)return true;const tag=(el.tagName||'').toLowerCase();"
                    "return ['input','button','a','select','textarea'].includes(tag)||el.getAttribute('role')==='button'};"
                    "const c=[[Math.floor(window.innerWidth/2),Math.floor(window.innerHeight/2)],[24,24],"
                    "[window.innerWidth-24,24],[24,window.innerHeight-24],[window.innerWidth-24,window.innerHeight-24]];"
                    "for(const [x,y] of c){const el=document.elementFromPoint(x,y);if(!unsafe(el))return {x,y};}return null;"
                )
                if isinstance(point, dict) and "x" in point and "y" in point:
                    self.cdp_click_absolute(float(point["x"]), float(point["y"]))
                    changed = (after := active_snapshot()) is not None and after != before
            except Exception: pass  # noqa: E701  # pylint: disable=broad-except
        self._engine_aware_sleep(1.0, 2.2, "blur_sender_name_settle")
        return changed

    def _wait_for_review_checkout_enabled(self, timeout: float) -> tuple[bool, bool]:
        start = time.monotonic()
        deadline, present, disabled_seen = start + timeout, False, False
        while time.monotonic() < deadline:
            try:
                state = self._driver.execute_script(
                    "const btn=document.querySelector(arguments[0]);if(!btn)return {present:false,enabled:false};"
                    "const style=window.getComputedStyle(btn),rect=btn.getBoundingClientRect();"
                    "return {present:true,enabled:!btn.disabled&&btn.getAttribute('aria-disabled')!=='true'"
                    "&&style.pointerEvents!=='none'&&style.display!=='none'&&style.visibility!=='hidden'&&rect.width>0&&rect.height>0};",
                    SEL_REVIEW_CHECKOUT,
                )
                if isinstance(state, dict):
                    present = present or bool(state.get("present"))
                    disabled_seen = disabled_seen or (bool(state.get("present")) and not bool(state.get("enabled")))
                    if state.get("enabled"):
                        if disabled_seen:
                            _log.info(
                                "add_to_cart_and_checkout: review_checkout disabled True→False t+%.1fs",
                                time.monotonic() - start,
                            )
                        return True, True
            except Exception: pass  # noqa: E701  # pylint: disable=broad-except
            time.sleep(0.5)  # raw poll cadence; not behavioral pacing/DelayEngine budget
        return False, present

    @staticmethod
    def _snapshot_int(snapshot: dict, key: str) -> int:
        try: return int(snapshot.get(key, 0) or 0)  # noqa: E701
        except (TypeError, ValueError): return 0  # noqa: E701

    @staticmethod
    def _cart_log_snapshot(snapshot: dict) -> dict:
        keys = ("cookie_count", "localStorage_length", "sessionStorage_length", "cart_like_count", "cart_like_visible_count", "explicit_cart_line_item_count", "explicit_cart_line_item_visible_count", "error_like_count", "error_like_visible_count", "total_like_present", "total_like_text_len")
        return {key: snapshot.get(key) for key in keys if isinstance(snapshot, dict) and key in snapshot}

    def _cart_state_snapshot(self) -> dict:
        try:
            data = self._driver.execute_script("const v=(e)=>{if(!e)return false;const s=getComputedStyle(e),r=e.getBoundingClientRect();return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};const t=document.querySelector('[class*=\"total\"],[id*=\"total\"]');const l=document.querySelectorAll('[class*=\"cart-item\"],[class*=\"lineItem\"],[class*=\"product\"][class*=\"row\"]');const c=document.querySelectorAll('[class*=\"cart\"],[id*=\"cart\"]');const e=document.querySelectorAll('[class*=\"error\"],[role=\"alert\"]');const b=document.querySelector(arguments[0]);return {total_like_present:Boolean(t),total_like_text_len:t&&typeof t.innerText==='string'?t.innerText.length:0,explicit_cart_line_item_count:l.length,explicit_cart_line_item_visible_count:Array.from(l).filter(v).length,cart_like_visible_count:Array.from(c).filter(v).length,error_like_visible_count:Array.from(e).filter(v).length,review_checkout:{present:Boolean(b),enabled:!!b&&!b.disabled&&b.getAttribute('aria-disabled')!=='true'&&getComputedStyle(b).pointerEvents!=='none'&&v(b),disabled:b?Boolean(b.disabled):null}};", SEL_REVIEW_CHECKOUT)
            return data if isinstance(data, dict) else {}
        except Exception: return {}  # noqa: E701  # pylint: disable=broad-except

    def _cart_dom_audit(self) -> dict:
        """PII-safe structural DOM audit run once on cart-state timeout.

        Returns a dict of counts, booleans, lengths, and known-prefix id strings only.
        Never returns innerText, outerHTML, form values, cookies, or storage values.
        Wraps the single execute_script call in try/except so audit failure never breaks
        the timeout error path.
        """
        try:
            return self._driver.execute_script(
                "const v=(e)=>{"
                "if(!e)return false;"
                "const s=getComputedStyle(e),r=e.getBoundingClientRect();"
                "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';"
                "};"
                "const atcEl=document.querySelector('#cws_btn_gcBuyAdd');"
                "const rcEl=document.querySelector('#cws_btn_gcBuyCheckout');"
                "const cwsIds=Array.from(document.querySelectorAll('[id^=\"cws_\"]'));"
                "const safeCwsId=(id)=>/^cws_/i.test(id||'')&&!/(cc(num|cvv|exp|name)|password|ssn)/i.test(id||'');"
                "const cartContainers=document.querySelectorAll('[id*=\"cart\" i],[class*=\"cart\" i]');"
                "return {"
                "current_url_path:location.pathname,"
                "body_html_len:document.body.innerHTML.length,"
                "cws_id_count:document.querySelectorAll('[id^=\"cws_\"]').length,"
                "cws_class_count:document.querySelectorAll('[class*=\"cws_\"]').length,"
                "add_to_cart_present:!!atcEl,"
                "add_to_cart_state:{present:!!atcEl,enabled:!!atcEl&&!atcEl.disabled&&atcEl.getAttribute('aria-disabled')!=='true'&&getComputedStyle(atcEl).pointerEvents!=='none'&&v(atcEl),disabled:atcEl?Boolean(atcEl.disabled):null,visible:v(atcEl)},"
                "review_checkout_present:!!rcEl,"
                "review_checkout_state:{present:!!rcEl,enabled:!!rcEl&&!rcEl.disabled&&rcEl.getAttribute('aria-disabled')!=='true'&&getComputedStyle(rcEl).pointerEvents!=='none'&&v(rcEl),disabled:rcEl?Boolean(rcEl.disabled):null},"
                "cart_container_count:cartContainers.length,"
                "cart_container_visible_count:Array.from(cartContainers).filter(v).length,"
                "alt_line_item_patterns:{"
                "cws_underscored:document.querySelectorAll('[class*=\"cws_\"][class*=\"item\" i],[class*=\"cws_\"][class*=\"row\" i],[id*=\"cws_\"][id*=\"item\" i],[id*=\"cws_\"][id*=\"row\" i]').length,"
                "table_rows_in_cart:document.querySelectorAll('[id*=\"cart\" i] tr,[class*=\"cart\" i] tr').length,"
                "list_items_in_cart:document.querySelectorAll('[id*=\"cart\" i] li,[class*=\"cart\" i] li').length"
                "},"
                "alt_total_patterns:{"
                "grand:document.querySelectorAll('[class*=\"grandTotal\" i],[id*=\"grandTotal\" i],[class*=\"grand_total\" i],[id*=\"grand_total\" i]').length,"
                "sub:document.querySelectorAll('[class*=\"subtotal\" i],[id*=\"subtotal\" i],[class*=\"sub_total\" i],[id*=\"sub_total\" i]').length,"
                "order:document.querySelectorAll('[class*=\"orderTotal\" i],[id*=\"orderTotal\" i],[class*=\"order_total\" i],[id*=\"order_total\" i]').length,"
                "cws:document.querySelectorAll('[id^=\"cws_\"][id*=\"otal\" i],[class*=\"cws_\"][class*=\"otal\" i]').length"
                "},"
                "sample_cws_ids:cwsIds.map(e=>e.id||'').filter(safeCwsId).slice(0,30),"
                "alert_count:document.querySelectorAll('[role=\"alert\"]').length,"
                "alert_visible_count:Array.from(document.querySelectorAll('[role=\"alert\"]')).filter(v).length"
                "};"
            )
        except Exception:  # pylint: disable=broad-except
            return {"audit_failed": True}

    def _wait_for_cart_state_after_atc(self, baseline: dict, timeout: float) -> tuple[bool, dict]:
        """Poll for delta-based cart/total materialization after Add-to-Cart."""
        start = time.monotonic()
        deadline = start + timeout
        baseline = baseline if isinstance(baseline, dict) else {}
        baseline_total = bool(baseline.get("total_like_present"))
        baseline_line_count = self._snapshot_int(baseline, "explicit_cart_line_item_count")
        baseline_line_visible = self._snapshot_int(baseline, "explicit_cart_line_item_visible_count")
        baseline_cart_visible = self._snapshot_int(baseline, "cart_like_visible_count")
        baseline_review = baseline.get("review_checkout")
        baseline_review_disabled = isinstance(baseline_review, dict) and bool(baseline_review.get("present")) and not bool(baseline_review.get("enabled"))
        last_snapshot = baseline

        while time.monotonic() < deadline:
            snapshot = self._cart_state_snapshot()
            if isinstance(snapshot, dict):
                last_snapshot = snapshot
            total_like_present = bool(last_snapshot.get("total_like_present"))
            line_count_delta = self._snapshot_int(last_snapshot, "explicit_cart_line_item_count") - baseline_line_count
            line_visible_delta = self._snapshot_int(last_snapshot, "explicit_cart_line_item_visible_count") - baseline_line_visible
            cart_visible_delta = self._snapshot_int(last_snapshot, "cart_like_visible_count") - baseline_cart_visible

            signal = None
            if total_like_present and not baseline_total:
                signal = "total_like_present"
            elif line_visible_delta > 0 or line_count_delta > 0:
                signal = "explicit_cart_line_item"
            else:
                review = last_snapshot.get("review_checkout")
                if baseline_review_disabled and isinstance(review, dict) and bool(review.get("present")) and bool(review.get("enabled")):
                    signal = "review_checkout_enabled_without_total"

            if signal:
                if signal == "review_checkout_enabled_without_total" and baseline_review_disabled:
                    _log.info("add_to_cart_and_checkout: review_checkout disabled True→False t+%.1fs", time.monotonic() - start)
                _log.info(
                    "add_to_cart_and_checkout: cart_state materialized t+%.1fs signal=%s "
                    "total_like_present=%s total_like_baseline=%s "
                    "explicit_line_count_delta=%s explicit_line_visible_delta=%s "
                    "cart_like_visible_delta=%s",
                    time.monotonic() - start,
                    signal,
                    total_like_present,
                    baseline_total,
                    line_count_delta,
                    line_visible_delta,
                    cart_visible_delta,
                )
                return True, last_snapshot

            time.sleep(0.5)  # raw poll cadence; not behavioral pacing/DelayEngine budget

        _log.error(
            "add_to_cart_and_checkout: cart_state timeout after %.0fs "
            "snapshot=%s cart_like_visible_delta=%s",
            timeout,
            self._cart_log_snapshot(last_snapshot),
            self._snapshot_int(last_snapshot, "cart_like_visible_count") - baseline_cart_visible,
        )
        try:
            audit = self._cart_dom_audit()
        except Exception:  # pylint: disable=broad-except
            audit = {"audit_failed": True}
        if isinstance(audit, dict):
            audit["cart_state_snapshot_keys_present"] = (
                sorted(last_snapshot.keys()) if isinstance(last_snapshot, dict) else []
            )
        _log.error("add_to_cart_and_checkout: cart_state timeout dom_audit=%s", audit)
        return False, last_snapshot

    def _human_scroll_to(self, selector: str, *, max_steps: int = 12) -> None:
        elements = self.find_elements(selector)
        if not elements:
            return
        rnd = self._get_rng()
        stage = "init"
        try:
            stage = "rect_read"
            rect = self._driver.execute_script("const r=arguments[0].getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height};", elements[0])
            stage = "viewport_read"
            viewport_h = self._driver.execute_script("return window.innerHeight") or 720
            center = rect["top"] + rect["height"] / 2
            delta = center - viewport_h * rnd.uniform(0.45, 0.65)
            if abs(delta) >= 80:
                pixels_per_tick = rnd.uniform(70, 115)
                ticks = max(1, min(max_steps * 4, math.ceil(abs(delta) / pixels_per_tick)))  # cap each step to 4 micro-ticks
                direction = 1 if delta > 0 else -1
                for _tick_index in range(ticks):
                    dy = direction * rnd.uniform(70, 120)
                    stage = "wheel_dispatch"
                    self._driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseWheel", "x": rnd.uniform(300, 900), "y": rnd.uniform(250, 650), "deltaX": 0, "deltaY": dy},
                    )
                    self._engine_aware_sleep(0.04, 0.11, "scroll_micro_tick")
            stage = "scroll_final_settle"  # used by the except log if final settle raises
            self._engine_aware_sleep(0.9, 1.8, "scroll_final_settle")
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning(
                "CDP wheel scroll failed at stage=%s (%s); falling back to JS scrollIntoView",
                stage,
                _sanitize_error(str(exc)),
            )
            try:
                self._driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth', block:'center'});", elements[0])
            except Exception:  # pylint: disable=broad-except
                _log.debug("JS scroll fallback also failed", exc_info=True)

    def _smooth_scroll_to(self, selector: str) -> None:
        self._human_scroll_to(selector)

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

    def _click_closest_control_for(self, span_selector: str) -> None:
        """Click the nearest clickable control for a span locator."""
        try:
            rects = self._driver.execute_script(
                "const s=document.querySelector(arguments[0]);"
                "if(!s)return null;"
                "const c=s.closest('button,a,[role=\"button\"],.btn,#cws_btn_gcBuyAdd')||s;"
                "const sr=s.getBoundingClientRect(),cr=c.getBoundingClientRect();"
                "return {span:{x:sr.x,y:sr.y,w:sr.width,h:sr.height},"
                "control:{x:cr.x,y:cr.y,w:cr.width,h:cr.height}};",
                span_selector,
            )
        except Exception as exc:  # pylint: disable=broad-except
            if self._strict:
                raise CDPClickError(f"closest-control rect failed for {span_selector}: {exc}") from exc
            _log.warning(
                "_click_closest_control_for: rect lookup failed for %r; falling back to locator click",
                span_selector,
                exc_info=True,
            )
            self.bounding_box_click(span_selector)
            return

        if not isinstance(rects, dict) or not isinstance(rects.get("control"), dict):
            if rects is None:
                raise SelectorTimeoutError(span_selector, 0)
            if self._strict:
                raise CDPClickError(f"closest-control rect missing for {span_selector}: {rects}")
            self.bounding_box_click(span_selector)
            return

        control = rects["control"]
        try:
            left = float(control["x"])
            top = float(control["y"])
            width = float(control["w"])
            height = float(control["h"])
        except (KeyError, TypeError, ValueError) as exc:
            if self._strict:
                raise CDPClickError(f"closest-control rect invalid for {span_selector}: {rects}") from exc
            self.bounding_box_click(span_selector)
            return

        if width <= 0 or height <= 0:
            if self._strict:
                raise CDPClickError(f"closest-control rect zero-size for {span_selector}: {rects}")
            self.bounding_box_click(span_selector)
            return

        rnd = self._get_rng()
        inset_x = min(width / 2.0, max(1.0, width * 0.12))
        inset_y = min(height / 2.0, max(1.0, height * 0.18))
        x_low, x_high = left + inset_x, left + width - inset_x
        y_low, y_high = top + inset_y, top + height - inset_y
        x = (left + width / 2.0) if x_low > x_high else rnd.uniform(x_low, x_high)
        y = (top + height / 2.0) if y_low > y_high else rnd.uniform(y_low, y_high)
        # Minimal adaptation to live DOM — span used as locator, click resolved to
        # nearest control because diagnostic shows span 35×19 < control 313×37.
        self._ghost_move_to(span_selector)
        self.cdp_click_absolute(float(x), float(y))

    def bounding_box_click(self, selector: str) -> None:
        """Click using randomized bounding-box coordinates, with safe fallbacks.

        In strict mode (``self._strict=True``, the default), every error path
        raises :class:`CDPClickError` instead of falling back to Selenium
        native ``element.click()`` — which would emit ``isTrusted=False``
        events and degrade anti-fraud quality (audit finding [D3]).

        Non-strict mode is retained only for test/debug contexts and emits a
        WARNING before each Selenium fallback.

        Args:
            selector: CSS selector for the element to click.

        Raises:
            SelectorTimeoutError: if no matching element is found.
            CDPClickError: in strict mode, on any of the four CDP failure
                paths (rect-fetch error, zero-size rect, missing RNG,
                CDP dispatch error).
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)

        self._ghost_move_to(selector)

        # Branch 1: getBoundingClientRect failure
        try:
            rect = self._driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {left:r.left,top:r.top,width:r.width,height:r.height};",
                elements[0],
            )
        except Exception as exc:  # pylint: disable=broad-except
            if self._strict:
                raise CDPClickError(
                    f"getBoundingClientRect failed for {selector}: {exc}"
                ) from exc
            _log.warning(
                "bounding_box_click: getBoundingClientRect raised for selector %r;"
                " falling back to plain click",
                selector,
                exc_info=True,
            )
            elements[0].click()
            return

        # Branch 2: rect missing or zero-size
        if not rect or rect.get("width", 0) == 0 or rect.get("height", 0) == 0:
            if self._strict:
                raise CDPClickError(
                    f"Element {selector} has zero-size or missing rect: {rect}"
                )
            _log.warning(
                "bounding_box_click: rect missing/zero-size for selector %r;"
                " falling back to plain click",
                selector,
            )
            elements[0].click()
            return

        # Branch 3: persona RNG unavailable
        if self._rnd is None:
            if self._strict:
                raise CDPClickError(
                    f"bounding_box_click for {selector} requires persona RNG in strict mode"
                )
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

        # Branch 4: CDP dispatch failure
        try:
            _dispatch_cdp_click_sequence(self._driver, abs_x, abs_y)
            return
        except Exception as exc:  # pylint: disable=broad-except
            if self._strict:
                raise CDPClickError(
                    f"CDP click dispatch failed for {selector}: {exc}"
                ) from exc
            _log.warning(
                "bounding_box_click: CDP dispatch failed for selector %r;"
                " falling back to plain click",
                selector,
                exc_info=True,
            )
            elements[0].click()

    def cdp_click_absolute(self, x: float, y: float) -> None:
        """Send an absolute-coordinate CDP click.

        Raises:
            SessionFlaggedError: On CDP connection / transport failure.
            CDPCommandError: On non-retryable CDP command failure.
        """
        for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            _safe_cdp_cmd(
                self._driver,
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

    def _run_tab_janitor(self) -> None:
        """Close extra tabs, navigate to about:blank, and wait 2 s to settle.

        Blueprint §2 Tab Janitor: must run before the pre-flight geo check so
        that a real content window is focused and in a clean state.
        """
        close_extra_tabs(self._driver)
        # _select_real_content_window returns the focused handle; its side
        # effect of switching focus is required before navigating.
        selected = _select_real_content_window(self._driver)
        if selected is None:
            raise RuntimeError(
                "_run_tab_janitor: no real content window available "
                "after close_extra_tabs; only internal or probe-failed "
                "handles remain"
            )
        self._driver.get("about:blank")
        time.sleep(2)

    def preflight_geo_check(self) -> str:
        """Navigate to geo-check URL and assert the IP is US-based.

        Per Blueprint §2, the check is retried up to two times (three total
        attempts) with a 2 s pause and a main-window switch between
        attempts.  If every attempt fails, the method raises so the caller
        can rotate proxy or abort the session.  This method never returns
        ``"UNKNOWN"``: failures are always surfaced so proxy mistakes are
        not silently propagated.

        Returns:
            ``"US"`` when the geo-check API confirms a US IP.

        Raises:
            RuntimeError: if the detected country is not ``"US"`` or the
                API remains unreachable after two retries.
        """
        try:
            self._run_tab_janitor()
        except Exception as exc:
            raise RuntimeError(
                f"preflight_geo_check failed: tab janitor could not prepare "
                f"a real browser window for geo check: {exc}"
            ) from exc
        max_attempts = 3  # 1 initial + 2 retries
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            # Always ensure focus on the first real content window on each
            # attempt so that a stray popup, DevTools target, or closed tab
            # does not starve the check.  BitBrowser/chromedriver attach can
            # expose a devtools:// target as window_handles[0]; using
            # _select_real_content_window skips internal-scheme handles and
            # returns the first genuine content window.  If we cannot focus
            # a real window, the attempt itself is considered failed: running
            # geo-check on the wrong context would defeat the safeguard.
            try:
                selected = _select_real_content_window(self._driver)
                if selected is None:
                    raise RuntimeError(
                        "preflight_geo_check: no real content window available"
                    )
            except Exception as switch_exc:  # pylint: disable=broad-except
                last_exc = switch_exc
                _log.warning(
                    "preflight_geo_check: attempt %d/%d main-window "
                    "switch failed: %s",
                    attempt, max_attempts, switch_exc,
                )
                if attempt < max_attempts:
                    time.sleep(2)
                continue
            try:
                self._driver.get(URL_GEO_CHECK)
                body = self._driver.find_element("tag name", "body").text
                data = _json.loads(body)
                country = data.get("country", "")
                utc_offset = data.get("utc_offset", 0)
                self.set_proxy_utc_offset(
                    int(utc_offset) if utc_offset is not None else 0
                )
                if country != "US":
                    raise RuntimeError(
                        f"Geo-check failed: expected country 'US', "
                        f"got {country!r}"
                    )
                # Per-cycle de-dup: subsequent callers (e.g. orchestrator
                # ``run_preflight_and_fill``, ``run_full_cycle``) skip when
                # the flag is set.  Only set on a successful US confirm so
                # any failure path still allows a retry within the same
                # cycle if the caller chooses.
                self._geo_checked_this_cycle = True
                return country
            except Exception as exc:  # pylint: disable=broad-except
                last_exc = exc
                _log.warning(
                    "preflight_geo_check: attempt %d/%d failed: %s",
                    attempt, max_attempts, exc,
                )
                if attempt < max_attempts:
                    time.sleep(2)
        raise RuntimeError(
            f"preflight_geo_check failed after {max_attempts} attempts: "
            f"{last_exc}"
        ) from last_exc

    def _clear_browser_state(self) -> None:
        """Clear localStorage, sessionStorage, and cookies (Blueprint §3 Hard-Reset).

        In addition to the Selenium ``delete_all_cookies`` call (which only
        wipes cookies of the *current* browsing context), this method also
        issues CDP-level ``Network.clearBrowserCookies`` and
        ``Network.clearBrowserCache`` commands.  These wipe cookies/cache
        across *all* origins (e.g. ``wwws-usa2.givex.com`` vs
        ``lushusa.com``), closing the cross-origin gap surfaced by audit
        finding [B3] (Blueprint §7 end-of-cycle hard-reset).
        """
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
        # Belt-and-braces: CDP-level wipe covers cross-origin cookies that
        # ``delete_all_cookies`` (current-context only) would miss.
        try:
            self._driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning(
                "_clear_browser_state: CDP Network.clearBrowserCookies failed: %s",
                exc,
            )
        # Also clear the HTTP cache to match Blueprint §7 hard-reset.
        # Audit finding [F3]: cache wipe failure must be observable at
        # production log level — silent debug logging hides a real
        # half-broken hard-reset that lets cross-origin cache survive
        # into the next cycle.
        try:
            self._driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning(
                "_clear_browser_state: CDP Network.clearBrowserCache failed: %s",
                exc,
            )
        _log.debug("_clear_browser_state: browser state cleared for new cycle")

    def navigate_to_egift(self) -> None:
        """Navigate to the Givex base URL and open the eGift purchase page.

        Accepts the cookie banner if present, then clicks the Buy eGift link,
        and navigates directly to the eGift form page.
        """
        _log.info("navigate_to_egift: started")
        self._clear_browser_state()
        _log.info("navigate_to_egift: get(%s)", _short_url(URL_BASE))
        self._driver.get(URL_BASE)
        # Dismiss cookie banner if present (best-effort)
        if self.find_elements(SEL_COOKIE_ACCEPT):
            _log.info("navigate_to_egift: cookie banner detected")
            try:
                self.bounding_box_click(SEL_COOKIE_ACCEPT)
                _log.info("navigate_to_egift: cookie banner dismissed")
            except Exception as exc:  # cookie banner is best-effort; continue navigation
                _log.debug("Cookie banner click skipped: %s", exc)
        else:
            _log.info("navigate_to_egift: cookie banner absent")
        if self._wait_for_element(SEL_BUY_EGIFT_BTN, timeout=10):
            _log.info("navigate_to_egift: Buy-eGift visible")
        self.bounding_box_click(SEL_BUY_EGIFT_BTN)
        _log.info("navigate_to_egift: Buy-eGift clicked")
        self._wait_for_url_or_capture(URL_EGIFT, "url_egift_not_reached")
        _log.info("navigate_to_egift: URL_EGIFT reached")
        _log.info("navigate_to_egift: completed")

    # ── eGift form (Step 1) ─────────────────────────────────────────────────

    def fill_egift_form(self, task, billing_profile) -> None:
        """Fill all fields on the eGift purchase form.

        Args:
            task: WorkerTask with ``recipient_email`` and ``amount``.
            billing_profile: BillingProfile with ``first_name`` and
                ``last_name`` (used as recipient/sender name).
        """
        _log.info("fill_egift_form: started")
        if self._sm is not None:
            self._sm.transition("FILLING_FORM")
        self._engine_aware_sleep(0.9, 2.2, "egift_observation_before_form_scroll")
        self._select_card_design_if_required()
        self._smooth_scroll_to(SEL_GREETING_MSG)
        full_name = f"{billing_profile.first_name} {billing_profile.last_name}"
        greeting = _random_greeting(self._rnd)
        _log.info("fill_egift_form: field=SEL_GREETING_MSG len=%d", len(greeting))
        self._realistic_type_field(
            SEL_GREETING_MSG, greeting, field_kind="text",
        )
        _log.info("fill_egift_form: field=SEL_AMOUNT_INPUT len=%d", len(str(task.amount)))
        self._realistic_type_field(
            SEL_AMOUNT_INPUT, str(task.amount),
            field_kind="amount", typo_rate=0.0,
        )
        _log.info("fill_egift_form: field=SEL_RECIPIENT_NAME len=%d", len(full_name))
        self._realistic_type_field(
            SEL_RECIPIENT_NAME, full_name, field_kind="name",
        )
        _log.info("fill_egift_form: field=SEL_RECIPIENT_EMAIL len=%d", len(task.recipient_email))
        self._realistic_type_field(
            SEL_RECIPIENT_EMAIL, task.recipient_email, field_kind="text",
        )
        _log.info("fill_egift_form: field=SEL_CONFIRM_RECIPIENT_EMAIL len=%d", len(task.recipient_email))
        self._realistic_type_field(
            SEL_CONFIRM_RECIPIENT_EMAIL, task.recipient_email,
            field_kind="text",
        )
        _log.info("fill_egift_form: field=SEL_SENDER_NAME len=%d", len(full_name))
        self._realistic_type_field(
            SEL_SENDER_NAME, full_name, field_kind="name",
        )
        if not self._blur_active_field_naturally():
            _log.warning("fill_egift_form: blur active field failed after Tab and safe body-click fallback")
        _log.info("fill_egift_form: running final validation pass")
        fields = (SEL_GREETING_MSG, SEL_AMOUNT_INPUT, SEL_RECIPIENT_NAME, SEL_RECIPIENT_EMAIL, SEL_CONFIRM_RECIPIENT_EMAIL, SEL_SENDER_NAME)
        final_lens = {sel: self._field_value_length(sel) for sel in fields}
        for sel, ln in final_lens.items():
            _log.info("fill_egift_form: final_check field=%s value_len=%d", _selector_name(sel), ln)
            if ln < 0:
                self._capture_failure_screenshot(f"final_check_unreadable_{_selector_name(sel)}")
                raise SessionFlaggedError(
                    f"Final validation: field {_selector_name(sel)} unreadable (JS read error, not auto-clear)"
                )
            if ln == 0:
                self._capture_failure_screenshot(f"final_check_empty_{_selector_name(sel)}")
                raise SessionFlaggedError(f"Final validation: field {_selector_name(sel)} is empty after fill_egift_form completed (Givex auto-clear suspected)")
        recipient = self._field_value(SEL_RECIPIENT_EMAIL)
        confirm = self._field_value(SEL_CONFIRM_RECIPIENT_EMAIL)
        if recipient is None or confirm is None:
            _log.warning("fill_egift_form: final_check recipient/confirm email unreadable")
            self._capture_failure_screenshot("final_check_email_unreadable")
            raise SessionFlaggedError("Final validation: recipient/confirm email unreadable")
        if recipient != confirm:
            _log.warning("fill_egift_form: final_check recipient/confirm email mismatch detected")
            self._capture_failure_screenshot("final_check_email_mismatch")
            raise SessionFlaggedError("Final validation: recipient email and confirm email values differ")
        _log.info("fill_egift_form: completed (final validation passed)")

    # ── eGift card design picker (Step 1 sub-step) ──────────────────────────

    def _card_design_state_snapshot(self, label_id: str | None = None) -> dict:
        """Return a PII-safe length-only card design state snapshot."""
        if label_id is not None:
            radio_check_js = (
                "const radioId_=arguments[0].replace(/^cws_lbl_/,'');const radio_=document.getElementById(radioId_);"
                "const clickedChecked_=!!(radio_&&radio_.checked);"
            )
        else:
            radio_check_js = "const clickedChecked_=false;"
        state_js = (
            "(function(labelId_arg){"
            + radio_check_js
            + "const checkedCount_=document.querySelectorAll('#form--select-card input[type=\"radio\"]:checked,"
            "#cardsContainer input[type=\"radio\"]:checked').length;"
            "const preview_=document.getElementById('cardPreview');"
            "const previewName_=document.getElementById('cardPreviewName');"
            "const valEl_=document.querySelector('#cws_val_cardDesign');"
            "const v_=(e)=>{"
            "if(!e)return false;"
            "const s=getComputedStyle(e),r=e.getBoundingClientRect();"
            "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};"
            "return{"
            "clicked_radio_checked:clickedChecked_,"
            "checked_count:checkedCount_,"
            "preview_src_len:preview_?(preview_.getAttribute('src')||'').length:-1,"
            "preview_name_len:previewName_?(previewName_.textContent||'').length:-1,"
            "value_present:!!valEl_,"
            "value_text_len:valEl_?(valEl_.textContent||'').length:0,"
            "value_value_len:valEl_&&'value' in valEl_?(valEl_.value||'').length:0,"
            "value_attr_len:valEl_?Array.from(valEl_.attributes).reduce((s,a)=>s+(a.value||'').length,0):0,"
            "selected_like_count:document.querySelectorAll("
            "'[aria-selected=\"true\"],[aria-checked=\"true\"],.selected,.active,[data-selected=\"true\"]').length,"
            "visible_option_count:Array.from(document.querySelectorAll('[id^=\"cws_lbl_\"]'))"
            r".filter(e=>/^cws_lbl_\d{6}$/.test(e.id)&&v_(e)).length"
            "};})(arguments[0]);"
        )
        try:
            result = self._driver.execute_script(state_js, label_id or "")
            if isinstance(result, dict):
                return result
            return {}
        except Exception:  # pylint: disable=broad-except
            return {}

    def _select_card_design_if_required(self) -> None:
        """Click a visible card-design label if the picker is present."""
        detect_js = (
            "(function(){"
            "const v=(e)=>{"
            "if(!e)return false;"
            "const s=getComputedStyle(e),r=e.getBoundingClientRect();"
            "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};"
            "const cont=document.querySelector('#form--select-card')||document.querySelector('#cardsContainer');"
            "if(!cont)return null;"
            "const labels=Array.from(cont.querySelectorAll('label[id^=\"cws_lbl_\"]'));"
            "const valid=labels.filter(e=>{"
            r"if(!/^cws_lbl_\d{6}$/.test(e.id))return false;"
            "if(!v(e))return false;"
            "const radioId=e.id.replace(/^cws_lbl_/,'');const radio=document.getElementById(radioId);"
            "return !!(radio&&radio.type==='radio');"
            "});"
            "return valid.slice(0,20).map(e=>{"
            "const r=e.getBoundingClientRect();"
            "const radioId=e.id.replace(/^cws_lbl_/,'');const radio=document.getElementById(radioId);"
            "return{id:e.id,radio_id_len:radio.id.length,x:r.x,y:r.y,w:r.width,h:r.height};"
            "});})();"
        )

        raw: list | None = None
        try:
            raw = self._driver.execute_script(detect_js)
        except Exception as exc:  # pylint: disable=broad-except
            _log.info(
                "fill_egift_form: card_design detect_error reason_type=%s reason_len=%d, skipping",
                type(exc).__name__,
                len(str(exc)),
            )
            return

        if not isinstance(raw, list):
            _log.info("fill_egift_form: card_design no_picker_detected, skipping")
            return

        if not any(isinstance(c, dict) and c.get("id") for c in raw):
            # Container may still be rendering labels.
            _pick_deadline, _pick_retries = time.monotonic() + 3.0, 0
            while _pick_retries < 10:
                time.sleep(0.3)
                _pick_retries += 1
                try:
                    raw = self._driver.execute_script(detect_js)
                except Exception:  # pylint: disable=broad-except
                    raw = None
                if not isinstance(raw, list):
                    break
                if any(isinstance(c, dict) and c.get("id") for c in raw):
                    break
                if time.monotonic() >= _pick_deadline:
                    break

        candidates = [
            c for c in (raw if isinstance(raw, list) else [])
            if isinstance(c, dict) and c.get("id")
        ]
        if not candidates:
            _log.info("fill_egift_form: card_design no_picker_detected, skipping")
            return

        rng = self._rnd if self._rnd is not None else _random.Random(0)
        idx = rng.randrange(len(candidates))
        chosen = candidates[idx]
        chosen_id = chosen["id"]

        _log.debug(
            "fill_egift_form: card_design candidates=%r choosing idx=%d",
            [c.get("id") for c in candidates],
            idx,
        )

        state_before = self._card_design_state_snapshot()

        try:
            self._driver.execute_script(
                "const el=document.getElementById(arguments[0]);"
                "if(el)el.scrollIntoView({block:'center',inline:'center'});",
                chosen_id,
            )
        except Exception:  # pylint: disable=broad-except
            pass  # best-effort scroll

        post_rect = None
        try:
            post_rect = self._driver.execute_script(
                "const el=document.getElementById(arguments[0]);"
                "if(!el)return null;"
                "const r=el.getBoundingClientRect();"
                "const cx=r.x+r.width/2,cy=r.y+r.height/2;"
                "return{x:r.x,y:r.y,w:r.width,h:r.height,"
                "in_viewport:r.width>0&&r.height>0&&"
                "cx>=0&&cx<window.innerWidth&&"
                "cy>=0&&cy<window.innerHeight};",
                chosen_id,
            )
        except Exception:  # pylint: disable=broad-except
            pass

        if not isinstance(post_rect, dict) or post_rect.get("w", 0) <= 0 or post_rect.get("h", 0) <= 0:
            self._capture_failure_screenshot("card_design_post_rect_unavailable")
            raise SessionFlaggedError("Card design post-scroll rect unavailable")

        if not post_rect.get("in_viewport"):
            self._capture_failure_screenshot("card_design_offscreen")
            raise SessionFlaggedError("Card design candidate offscreen after scroll")

        click_x = post_rect["x"] + post_rect["w"] / 2.0
        click_y = post_rect["y"] + post_rect["h"] / 2.0
        self.cdp_click_absolute(click_x, click_y)

        state_after = self._card_design_state_snapshot(label_id=chosen_id)

        clicked_radio_checked = bool(state_after.get("clicked_radio_checked", False))
        after_checked_count = state_after.get("checked_count", 0)
        before_checked_count = state_before.get("checked_count", 0)
        after_preview_src = state_after.get("preview_src_len", -1)
        before_preview_src = state_before.get("preview_src_len", -1)
        after_preview_name = state_after.get("preview_name_len", -1)
        before_preview_name = state_before.get("preview_name_len", -1)
        selection_verified = (
            clicked_radio_checked
            or after_checked_count > before_checked_count
            or (before_checked_count == 0 and after_checked_count > 0)
            or (after_preview_src != before_preview_src and after_preview_src != -1)
            or (after_preview_name != before_preview_name and after_preview_name != -1)
            or state_after.get("value_text_len", 0) != state_before.get("value_text_len", 0)
            or state_after.get("value_value_len", 0) != state_before.get("value_value_len", 0)
            or state_after.get("value_attr_len", 0) != state_before.get("value_attr_len", 0)
            or state_after.get("selected_like_count", 0) > state_before.get("selected_like_count", 0)
        )

        _log.info(
            "fill_egift_form: card_design selected_index=%d visible_options=%d "
            "checked_count_before=%d checked_count_after=%d "
            "clicked_radio_checked=%s "
            "preview_src_len_before=%d preview_src_len_after=%d "
            "preview_name_len_before=%d preview_name_len_after=%d",
            idx,
            len(candidates),
            before_checked_count,
            after_checked_count,
            clicked_radio_checked,
            before_preview_src,
            after_preview_src,
            before_preview_name,
            after_preview_name,
        )

        if not selection_verified:
            self._capture_failure_screenshot("card_design_not_verified")
            raise SessionFlaggedError("Card design selection required but not verified")

    def _verify_atc_hittable(self) -> None:
        """Scroll ATC into view and verify center hit-test before clicking."""
        try:
            self._driver.execute_script(
                "document.querySelector('#cws_btn_gcBuyAdd')"
                "?.scrollIntoView({block:'center',inline:'center'});"
            )
        except Exception:  # pylint: disable=broad-except
            pass

        time.sleep(0.3)

        hittest_js = (
            "(function(){"
            "const el=document.querySelector('#cws_btn_gcBuyAdd');"
            "if(!el)return null;"
            "const r=el.getBoundingClientRect();"
            "if(r.width<=0||r.height<=0)"
            "return{in_viewport:false,hittest_pass:false,w:r.width,h:r.height};"
            "const cx=r.x+r.width/2,cy=r.y+r.height/2;"
            "const inView=cx>=0&&cx<window.innerWidth&&cy>=0&&cy<window.innerHeight;"
            "const hit=document.elementFromPoint(cx,cy);"
            "const hitPass=!!(hit&&(hit===el||el.contains(hit)||hit.contains(el)));"
            "return{in_viewport:inView,hittest_pass:hitPass,w:r.width,h:r.height};"
            "})()"
        )
        try:
            result = self._driver.execute_script(hittest_js)
        except Exception:  # pylint: disable=broad-except
            result = None

        if not isinstance(result, dict):
            _log.warning(
                "add_to_cart_and_checkout: ATC hittability check inconclusive"
                " (element absent or JS error); proceeding"
            )
            return

        if "in_viewport" not in result:
            _log.warning(
                "add_to_cart_and_checkout: ATC hittability check inconclusive"
                " (unexpected result shape); proceeding"
            )
            return

        in_viewport = bool(result.get("in_viewport"))
        hittest_pass = bool(result.get("hittest_pass"))
        _log.info(
            "add_to_cart_and_checkout: ATC hittest in_viewport=%s hittest_pass=%s"
            " w=%.0f h=%.0f",
            in_viewport,
            hittest_pass,
            result.get("w", 0),
            result.get("h", 0),
        )

        if not in_viewport or not hittest_pass:
            self._capture_failure_screenshot("add_to_cart_not_hittable")
            raise SessionFlaggedError("Add-to-Cart not hittable after scroll")

    def _verify_begin_checkout_hittable(self) -> None:
        """Scroll Begin Checkout into view and verify center hit-test before clicking."""
        try:
            self._driver.execute_script(
                "document.querySelector('#cws_btn_cartCheckout')"
                "?.scrollIntoView({block:'center',inline:'center'});"
            )
        except Exception:  # pylint: disable=broad-except
            pass

        time.sleep(0.3)

        hittest_js = (
            "(function(){"
            "const el=document.querySelector('#cws_btn_cartCheckout');"
            "if(!el)return null;"
            "const r=el.getBoundingClientRect();"
            "if(r.width<=0||r.height<=0)"
            "return{in_viewport:false,hittest_pass:false,w:r.width,h:r.height};"
            "const cx=r.x+r.width/2,cy=r.y+r.height/2;"
            "const inView=cx>=0&&cx<window.innerWidth&&cy>=0&&cy<window.innerHeight;"
            "const hit=document.elementFromPoint(cx,cy);"
            "const hitPass=!!(hit&&(hit===el||el.contains(hit)||hit.contains(el)));"
            "return{in_viewport:inView,hittest_pass:hitPass,w:r.width,h:r.height};"
            "})()"
        )
        try:
            result = self._driver.execute_script(hittest_js)
        except Exception:  # pylint: disable=broad-except
            result = None

        if not isinstance(result, dict):
            _log.warning(
                "select_guest_checkout: Begin Checkout hittability check inconclusive"
                " (element absent or JS error); proceeding"
            )
            return

        if "in_viewport" not in result:
            _log.warning(
                "select_guest_checkout: Begin Checkout hittability check inconclusive"
                " (unexpected result shape); proceeding"
            )
            return

        in_viewport = bool(result.get("in_viewport"))
        hittest_pass = bool(result.get("hittest_pass"))
        _log.info(
            "select_guest_checkout: Begin Checkout hittest in_viewport=%s hittest_pass=%s"
            " w=%.0f h=%.0f",
            in_viewport,
            hittest_pass,
            result.get("w", 0),
            result.get("h", 0),
        )

        if not in_viewport or not hittest_pass:
            self._capture_failure_screenshot("begin_checkout_not_hittable")
            raise SessionFlaggedError("Begin Checkout not hittable after scroll")

    def add_to_cart_and_checkout(self) -> None:
        """Click Add-to-Cart, wait for Review & Checkout button, then click it.

        After clicking Review & Checkout, waits for the browser to reach
        the cart page (``URL_CART``) before returning.
        """
        _log.info("add_to_cart_and_checkout: started")
        add_to_cart_timeout = 8
        if not self._wait_for_interactable(SEL_ADD_TO_CART, timeout=add_to_cart_timeout):
            raise SelectorTimeoutError(SEL_ADD_TO_CART, add_to_cart_timeout)
        atc_deadline = time.monotonic() + add_to_cart_timeout
        atc_ready_js = "const el=document.querySelector(arguments[0]);const c=el?el.closest('button,a,[role=\"button\"],.btn,#cws_btn_gcBuyAdd'):null;return !!c&&!c.disabled&&c.getAttribute('aria-disabled')!=='true';"
        while True:
            try:
                if bool(self._driver.execute_script(atc_ready_js, SEL_ADD_TO_CART)):
                    break
            except Exception: pass  # noqa: E701  # pylint: disable=broad-except
            if time.monotonic() >= atc_deadline:
                raise SelectorTimeoutError(SEL_ADD_TO_CART, add_to_cart_timeout, reason="add-to-cart present but disabled")
            time.sleep(0.25)
        self._engine_aware_sleep(0.8, 1.8, "atc_ready_before_click")
        cart_baseline = self._review_checkout_diagnostics()
        atc_flow_start = time.monotonic()
        self._verify_atc_hittable()
        self._click_closest_control_for(SEL_ADD_TO_CART)
        delay = 3.0 + self._get_rng().uniform(0.1, 0.8)
        _log.info(
            "add_to_cart_and_checkout: ATC clicked t+%.1fs baseline=%s; "
            "waiting %.2fs per blueprint",
            time.monotonic() - atc_flow_start,
            self._cart_log_snapshot(cart_baseline),
            delay,
        )
        time.sleep(delay)
        try:
            cart_timeout = float(os.environ.get("GIVEX_CART_STATE_POLL_S", str(_GIVEX_CART_STATE_POLL_DEFAULT_S)))
        except ValueError:
            cart_timeout = _GIVEX_CART_STATE_POLL_DEFAULT_S
        cart_poll_start = time.monotonic()
        cart_materialized, _cart_snapshot = self._wait_for_cart_state_after_atc(
            cart_baseline,
            timeout=cart_timeout,
        )
        cart_poll_elapsed = time.monotonic() - cart_poll_start
        if not cart_materialized:
            self._capture_failure_screenshot("cart_state_not_materialized")
            raise SelectorTimeoutError(
                SEL_REVIEW_CHECKOUT,
                int(time.monotonic() - atc_flow_start),
                reason="cart total not materialized",
            )
        _log.info("add_to_cart_and_checkout: settled, waiting Review-Checkout interactable")
        try:
            interactable_timeout = float(os.environ.get("GIVEX_REVIEW_CHECKOUT_POLL_S", str(_GIVEX_REVIEW_CHECKOUT_POLL_DEFAULT_S)))
        except ValueError:
            interactable_timeout = _GIVEX_REVIEW_CHECKOUT_POLL_DEFAULT_S
        review_poll_start = time.monotonic()
        found, present = self._wait_for_review_checkout_enabled(timeout=interactable_timeout)
        review_poll_elapsed = time.monotonic() - review_poll_start
        if not found:
            flavor = "present but disabled" if present else "not found"
            total_elapsed = time.monotonic() - atc_flow_start
            _log.error(
                "add_to_cart_and_checkout: Review-Checkout %s blueprint_wait=%.2fs cart_poll_elapsed=%.2fs review_poll=%.2fs total_elapsed=%.2fs",
                flavor,
                delay,
                cart_poll_elapsed,
                review_poll_elapsed,
                total_elapsed,
            )
            self._log_review_checkout_diagnostics()
            self._capture_failure_screenshot("review_checkout_not_interactable")
            timeout = int(total_elapsed)
            if present:
                raise SelectorTimeoutError(SEL_REVIEW_CHECKOUT, timeout, reason="present but disabled")
            raise SelectorTimeoutError(SEL_REVIEW_CHECKOUT, timeout, reason="review checkout absent")
        _log.info("add_to_cart_and_checkout: Review-Checkout interactable")
        self.bounding_box_click(SEL_REVIEW_CHECKOUT)
        _log.info("add_to_cart_and_checkout: Review-Checkout clicked")
        _log.info("add_to_cart_and_checkout: waiting URL_CART")
        self._wait_for_url_or_capture(URL_CART, "url_cart_not_reached")
        _log.info("add_to_cart_and_checkout: completed (URL_CART reached)")

    # ── Cart & Guest Checkout (Step 2) ───────────────────────────────────────

    def _is_selector_present_visible(self, selector: str) -> bool:
        """Return True when selector exists with non-zero visible dimensions."""
        try:
            result = self._driver.execute_script(
                "const el=document.querySelector(arguments[0]);"
                "if(!el)return false;"
                "const s=getComputedStyle(el),r=el.getBoundingClientRect();"
                "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';",
                selector,
            )
            return result is True
        except Exception:  # pylint: disable=broad-except
            return False

    def _wait_for_checkout_or_guest_inline(self, timeout: int = 15) -> str:
        """Wait for checkout URL or inline guest checkout controls.

        Returns:
            ``"url"`` when ``URL_CHECKOUT`` is reached, ``"guest_heading"``
            when the guest accordion heading is visible inline, or
            ``"guest_email"`` when the guest email field is visible inline.
        """
        deadline = time.monotonic() + timeout
        last_url = last_non_empty_url = ""
        transitions = 0
        expected_short = _short_url(URL_CHECKOUT)
        started = time.monotonic()
        while time.monotonic() < deadline:
            current = ""
            try:
                current = self._driver.current_url or ""
            except WebDriverException:  # URL briefly unavailable during page transition
                _log.debug("Checkout/guest inline wait deferred: URL unavailable")
            except Exception as exc:  # pylint: disable=broad-except
                _log.warning(
                    "Checkout/guest inline wait URL read failed: %s",
                    _sanitize_error(str(exc)),
                )
            if current != last_url:
                if current:
                    transitions += 1
                    _log.info(
                        "_wait_for_checkout_or_guest_inline[expecting=%s or_guest_inline]: "
                        "URL transitioned to %s (transition #%d, t+%.1fs)",
                        expected_short,
                        _sanitize_url_for_log(current),
                        transitions,
                        time.monotonic() - started,
                    )
                    last_non_empty_url = current
                last_url = current
            if URL_CHECKOUT in current:
                return "url"
            if self._is_selector_present_visible(SEL_GUEST_HEADING):
                return "guest_heading"
            if self._is_selector_present_visible(SEL_GUEST_EMAIL):
                return "guest_email"
            time.sleep(0.5)
        self._capture_failure_screenshot("url_checkout_not_reached")
        raise PageStateError(
            "url_wait expected="
            f"{expected_short} or_guest_inline "
            f"last_seen={_sanitize_url_for_log(last_non_empty_url)} "
            f"transitions={transitions}"
        )

    def select_guest_checkout(self, guest_email: str) -> None:
        """Click Begin Checkout, expand guest heading, enter email, and continue.

        Steps:
        1. Wait for and click Begin Checkout on the cart page.
        2. Verify Begin Checkout is hittable after scroll before clicking.
        3. Wait for the checkout page (``URL_CHECKOUT``) or inline guest
           checkout controls on the cart page.
        4. Click the guest heading (``#guestHeading``) if the guest email
           field is not already visible.
        5. Enter *guest_email* and click Continue.
        6. Wait for the payment page (``URL_PAYMENT``).

        Args:
            guest_email: Email address to enter in the guest checkout field.

        Raises:
            SelectorTimeoutError: if a required element never appears.
            PageStateError: if a required page URL is not reached.
        """
        _log.info("select_guest_checkout: started")
        found = self._wait_for_interactable(SEL_BEGIN_CHECKOUT, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_BEGIN_CHECKOUT, 10)
        _log.info("select_guest_checkout: Begin-Checkout interactable")
        self._verify_begin_checkout_hittable()
        self.bounding_box_click(SEL_BEGIN_CHECKOUT)
        _log.info("select_guest_checkout: Begin-Checkout clicked")
        signal = self._wait_for_checkout_or_guest_inline(timeout=15)
        if signal == "url":
            _log.info("select_guest_checkout: URL_CHECKOUT reached")
        else:
            _log.info(
                "select_guest_checkout: guest checkout inline visible on cart page signal=%s",
                signal,
            )

        if not self._is_selector_present_visible(SEL_GUEST_EMAIL):
            found = self._wait_for_element(SEL_GUEST_HEADING, timeout=10)
            if not found:
                raise SelectorTimeoutError(SEL_GUEST_HEADING, 10)
            self.bounding_box_click(SEL_GUEST_HEADING)
            _log.info("select_guest_checkout: Guest heading expanded")
        else:
            _log.info("select_guest_checkout: Guest email already visible; skipping heading click")

        found = self._wait_for_element(SEL_GUEST_EMAIL, timeout=10)
        if not found:
            raise SelectorTimeoutError(SEL_GUEST_EMAIL, 10)
        _log.info("select_guest_checkout: email len=%d", len(guest_email))
        self._realistic_type_field(SEL_GUEST_EMAIL, guest_email, field_kind="text")
        self.bounding_box_click(SEL_GUEST_CONTINUE)
        _log.info("select_guest_checkout: Continue clicked")
        self._wait_for_url_or_capture(URL_PAYMENT, "url_payment_not_reached")
        _log.info("select_guest_checkout: URL_PAYMENT reached")
        _log.info("select_guest_checkout: completed")

    # ── Payment & Billing (Step 4 — same page) ──────────────────────────────

    def fill_payment_and_billing(self, card_info, billing_profile) -> None:
        """Fill card (and, if given, billing) fields on the shared payment page."""
        if self._sm is not None:
            self._sm.transition("PAYMENT")
        card_name_source = "card_info"
        card_name = (card_info.card_name or "").strip()
        if not _looks_like_cardholder_name(card_name):
            if billing_profile is None:
                raise ValueError(
                    "Invalid cardholder name and no billing profile available; "
                    "refusing to type card number to avoid form pollution"
                )
            card_name = f"{billing_profile.first_name} {billing_profile.last_name}".strip()
            card_name_source = "billing_profile"
            if not _looks_like_cardholder_name(card_name):
                raise ValueError(
                    "Invalid cardholder name and invalid billing profile name; "
                    "refusing to type card number to avoid form pollution"
                )
        _log.info(
            "fill_payment_and_billing: field=SEL_CARD_NAME source=%s len=%d",
            card_name_source,
            len(card_name),
        )
        self._realistic_type_field(SEL_CARD_NAME, card_name, field_kind="name")
        self._realistic_type_field(SEL_CARD_NUMBER, card_info.card_number, use_burst=True, field_kind="card_number")
        self._cdp_select_option(SEL_CARD_EXPIRY_MONTH, card_info.exp_month)
        self._cdp_select_option(SEL_CARD_EXPIRY_YEAR, card_info.exp_year)
        self._realistic_type_field(SEL_CARD_CVV, card_info.cvv, field_kind="cvv")
        if billing_profile is None:
            return
        # Billing section — all text fields routed through CDP Input.dispatchKeyEvent
        # via _realistic_type_field (Phase 3A Task 1, INV-PAYMENT-01 anti-detect).
        self._realistic_type_field(SEL_BILLING_ADDRESS, billing_profile.address, field_kind="text")
        self._cdp_select_option(SEL_BILLING_COUNTRY, billing_profile.country)
        self._wait_for_select_options(
            SEL_BILLING_STATE,
            min_options=2,
            timeout=8.0,
            target_value=billing_profile.state,
        )
        self._cdp_select_option(SEL_BILLING_STATE, billing_profile.state)
        self._realistic_type_field(SEL_BILLING_CITY, billing_profile.city, field_kind="text")
        self._realistic_type_field(SEL_BILLING_ZIP, billing_profile.zip_code, field_kind="amount")
        if billing_profile.phone:
            self._realistic_type_field(SEL_BILLING_PHONE, billing_profile.phone, field_kind="amount")

    def fill_card_fields(self, card_info) -> None:
        """Fill card fields only (no billing); used after VBV reload."""
        self.fill_payment_and_billing(card_info, billing_profile=None)

    def fill_card(self, card_info) -> None:
        """Backward-compatibility alias for :meth:`fill_card_fields`.

        Preserves the ``modules.cdp.main.fill_card(card_info, worker_id)``
        contract published in ``spec/interface.md`` and
        ``spec/integration/interface.md``.  Delegates to
        :meth:`fill_card_fields` so the public wrapper remains functional
        against a real :class:`GivexDriver` instance.
        """
        self.fill_card_fields(card_info)

    def fill_billing(self, billing_profile) -> None:
        """Backward-compatibility method that fills only billing fields.

        .. deprecated::
            Use ``fill_payment_and_billing(card_info, billing_profile)`` instead.
        """
        self._realistic_type_field(SEL_BILLING_ADDRESS, billing_profile.address, field_kind="text")
        self._cdp_select_option(SEL_BILLING_COUNTRY, billing_profile.country)
        self._wait_for_select_options(
            SEL_BILLING_STATE,
            min_options=2,
            timeout=8.0,
            target_value=billing_profile.state,
        )
        self._cdp_select_option(SEL_BILLING_STATE, billing_profile.state)
        self._realistic_type_field(SEL_BILLING_CITY, billing_profile.city, field_kind="text")
        self._realistic_type_field(SEL_BILLING_ZIP, billing_profile.zip_code, field_kind="amount")
        if billing_profile.phone:
            self._realistic_type_field(SEL_BILLING_PHONE, billing_profile.phone, field_kind="amount")

    def fill_billing_form(self, billing_profile) -> None:
        """Backward-compatibility alias for ``fill_billing``."""
        self.fill_billing(billing_profile)

    # ── Order Total cross-check (E3 audit) ──────────────────────────────────

    def set_expected_total(self, value) -> None:
        """Record the watchdog/preflight total for the submit-time cross-check.

        Called by the orchestrator after Phase A's ``wait_for_total``.  ``None``
        clears.  Raises ``ValueError`` if *value* is not a finite number.
        """
        if value is None:
            self._expected_total = None
            return
        try:
            # ``str()`` so a binary-float like 49.99 keeps its literal form.
            parsed = decimal.Decimal(str(value))
        except (decimal.InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                f"set_expected_total: cannot parse {value!r} as Decimal"
            ) from exc
        if not parsed.is_finite():
            raise ValueError(
                f"set_expected_total: non-finite total {value!r} rejected"
            )
        self._expected_total = parsed

    def _read_dom_order_total(self) -> decimal.Decimal:
        """Read the visible Order Total via :func:`_parse_money_text`.

        Raises ``PageStateError`` if no element matches
        :data:`SEL_ORDER_TOTAL_DISPLAY` or its text is unparseable.
        """
        elements = self.find_elements(SEL_ORDER_TOTAL_DISPLAY)
        if not elements:
            raise PageStateError(f"order_total:{SEL_ORDER_TOTAL_DISPLAY}")
        try:
            raw = elements[0].text or ""
        except Exception as exc:  # pylint: disable=broad-except
            raise PageStateError(
                f"order_total:read:{_sanitize_error(str(exc))}"
            ) from exc
        value = _parse_money_text(raw)
        if value is None:
            raise PageStateError(f"order_total:parse:{SEL_ORDER_TOTAL_DISPLAY}")
        return value

    def submit_purchase(self) -> None:
        """Hesitate 3-5s, cross-check Order Total, then click COMPLETE PURCHASE.

        Spec §5 line 287.  Before the irreversible click, the DOM Order Total
        is compared to the value wired via :meth:`set_expected_total`; drift
        greater than :data:`_ORDER_TOTAL_TOLERANCE` raises
        :class:`SessionFlaggedError` and skips the click.  No-op when no
        expected total has been wired (legacy callers).
        """
        self._hesitate_before_submit()
        # E3: re-check Order Total *after* the hesitation window so any cart
        # mutation during the 3-5 s pause is caught before the click.
        if self._expected_total is not None:
            dom_total = self._read_dom_order_total()
            drift = abs(dom_total - self._expected_total)
            if drift > _ORDER_TOTAL_TOLERANCE:
                _log.warning(
                    "submit_purchase: Order Total mismatch — dom=%s expected=%s drift=%s; "
                    "refusing to click COMPLETE PURCHASE",
                    dom_total, self._expected_total, drift,
                )
                raise SessionFlaggedError(
                    f"Order Total mismatch: dom={dom_total} "
                    f"expected={self._expected_total} drift={drift}"
                )
        # Wire active-poll monitor to catch late-appearing VBV iframe after submit.
        # See PR #206 for TransientMonitor class; this is the follow-up wiring step.
        # Late import avoids A1 cross-module isolation violation flagged by
        # check_import_scope; cdp→monitor is a permitted one-way dependency.
        monitor = None
        try:
            from modules.monitor.main import TransientMonitor as _TransientMonitor  # noqa: PLC0415
            monitor = _TransientMonitor(
                detector=lambda: bool(self.find_elements(SEL_VBV_IFRAME)),
                interval=0.5,
            )
            monitor.start()
        except ImportError:  # pragma: no cover - monitor always present in prod
            pass
        try:
            # Phase 5A Task 2A: gate the irreversible submit click behind
            # the CRITICAL_SECTION flag so any concurrent delay-injection
            # site (e.g. background biometric typing) is forced to zero.
            # Review fix [F1]: only advance the FSM to POST_ACTION when
            # the click actually succeeded — otherwise an unrelated mid-
            # submit failure would mark the worker as post-submit and
            # incorrectly lock out future delay injection.
            if self._sm is not None:
                self._sm.enter_critical_zone("payment_submit")
            click_succeeded = False
            try:
                self.bounding_box_click(SEL_COMPLETE_PURCHASE)
                click_succeeded = True
            finally:
                if self._sm is not None:
                    self._sm.exit_critical_zone()
                    if click_succeeded:
                        if not self._sm.transition("POST_ACTION"):
                            _log.warning(
                                "submit_purchase: SM rejected POST_ACTION transition from %s",
                                self._sm.get_state(),
                            )
        finally:
            if monitor is not None:
                monitor.cancel(timeout=_VBV_MONITOR_CANCEL_TIMEOUT_S)

    def clear_card_fields_cdp(self) -> None:
        """Clear card number + CVV via CDP Ctrl+A + Backspace (Blueprint §6 Fork 4).

        Raises:
            CDPError: If the underlying CDP command fails. Swallowing the
                error would leave stale card data in the form and risk a
                double-charge on submit (P1-4).
        """
        for selector in (SEL_CARD_NUMBER, SEL_CARD_CVV):
            try:
                if not self.find_elements(selector):
                    continue
                self.bounding_box_click(selector)
                for key, code, vk, mods in (("a", "KeyA", 65, 2), ("Backspace", "Backspace", 8, 0)):
                    evt = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
                    if mods:
                        evt["modifiers"] = mods
                    for t in ("keyDown", "keyUp"):
                        self._driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": t, **evt})
            except Exception as exc:  # pylint: disable=broad-except
                _detail = _sanitize_error(str(exc))
                _log.warning("clear_card_fields_cdp failed; aborting")
                raise CDPError(
                    f"clear_card_fields_cdp failed: {_detail}"
                ) from exc

    def clear_card_fields(self) -> None:
        """Clear all card form fields (best-effort)."""
        self.clear_card_fields_cdp()

    # ── Post-submit state detection (Step 5) ─────────────────────────────────

    def detect_page_state(self) -> str:
        """Inspect the current page and return the FSM state name.

        Detection order:
        1. ``success``   — URL contains a confirmation fragment, OR
                           (URL is on the Givex host AND
                            ``.order-confirmation`` element is present), OR
                           (URL is on the Givex host AND page text contains
                            "thank you for your order" — SPA fallback when
                            URL path does not change).
                           The host gate prevents false-positives when
                           generic CSS classes or similar marketing copy
                           appear on non-checkout pages.
        2. ``vbv_3ds``   — A 3-D Secure / Adyen iframe is present.
        3. ``declined``  — URL contains ``error=vv`` (Givex VBV/3DS failure
                           signal), OR a payment-error element is present, OR
                           page text contains "declined" / "transaction failed".
        4. ``ui_busy``   — A loading overlay or spinner is present and the page
                           is still actively processing the submit.
        5. ``ui_lock``   — No spinner/state change appears for 3 seconds after
                           submit (stuck UI per Blueprint §6).
        6. Raises ``PageStateError`` if none of the above matched.

        Returns:
            One of: ``"success"``, ``"vbv_3ds"``, ``"declined"``,
            ``"ui_busy"``, ``"ui_lock"``.

        Raises:
            PageStateError: if the page state cannot be determined.
        """
        state = self._detect_non_popup_page_state()
        if state is not None:
            return state

        if self._detect_givex_submission_error_popup():
            closed = self._close_givex_submission_error_popup()
            if closed:
                state = self._detect_non_popup_page_state()
                if state is not None:
                    return state
            raise _SubmissionErrorPopupDetected(popup_closed=closed)

        # 4 — ui_busy (spinner visible means active loading, not a stuck UI)
        if self.find_elements(SEL_UI_LOCK_SPINNER):
            return "ui_busy"

        # 5 — 3s timeout fallback: sustained spinner-absent stall → ui_lock
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.3)
            state = self._detect_non_popup_page_state()
            if state is not None:
                return state
            if self._detect_givex_submission_error_popup():
                closed = self._close_givex_submission_error_popup()
                if closed:
                    state = self._detect_non_popup_page_state()
                    if state is not None:
                        return state
                raise _SubmissionErrorPopupDetected(popup_closed=closed)
            if self.find_elements(SEL_UI_LOCK_SPINNER):
                return "ui_busy"
        # After 3s with no spinner/state change → treat as stuck ui_lock
        return "ui_lock"

    def _detect_non_popup_page_state(self) -> str | None:
        current_url = self._driver.current_url
        if any(frag in current_url for frag in URL_CONFIRM_FRAGMENTS):
            return "success"
        on_givex_host = URL_CONFIRM_HOST in current_url.lower()
        if on_givex_host and self.find_elements(SEL_CONFIRMATION_EL):
            return "success"
        page_text = self._driver.find_element("tag name", "body").text.lower()
        if on_givex_host and "thank you for your order" in page_text:
            return "success"
        if self.find_elements(SEL_VBV_IFRAME):
            return "vbv_3ds"
        if "error=vv" in current_url.lower():
            return "declined"
        if self.find_elements(SEL_DECLINED_MSG):
            return "declined"
        if "declined" in page_text or "transaction failed" in page_text:
            return "declined"
        return None

    # ── Split-phase orchestrator helpers ─────────────────────────────────────

    def run_pre_card_checkout_prepare(self, task, billing_profile) -> None:
        """Run all pre-card steps: geo check, navigate, eGift form, cart, guest checkout.

        Performs steps 1–5 of the purchase sequence — everything up to and
        including guest-checkout selection — but intentionally omits
        ``fill_payment_and_billing`` so that the Phase A pricing watchdog can
        confirm the order total BEFORE any card/billing data is typed
        (INV-PAYMENT-01 / Blueprint §5).

        Geo-check is idempotent: if ``_geo_checked_this_cycle`` is already
        ``True`` on this driver instance the check is skipped.

        Args:
            task: WorkerTask with purchase details.
            billing_profile: BillingProfile with address and email.

        Raises:
            ValueError: if ``billing_profile.email`` is ``None``.
        """
        if billing_profile.email is None:
            raise ValueError(
                "billing_profile.email must not be None for guest checkout"
            )
        _log.info("run_pre_card_checkout_prepare: started (geo_checked=%s)", self._geo_checked_this_cycle)
        if self._geo_checked_this_cycle is not True:
            _log.info("run_pre_card_checkout_prepare: running preflight_geo_check")
            self.preflight_geo_check()
            _log.info("run_pre_card_checkout_prepare: preflight_geo_check completed")
        else:
            _log.info("run_pre_card_checkout_prepare: preflight_geo_check skipped")

        def run_step(name, fn, *args):
            _log.info("run_pre_card_checkout_prepare: %s started", name)
            fn(*args)
            _log.info("run_pre_card_checkout_prepare: %s completed", name)

        run_step("navigate_to_egift", self.navigate_to_egift)
        run_step("fill_egift_form", self.fill_egift_form, task, billing_profile)
        run_step("add_to_cart_and_checkout", self.add_to_cart_and_checkout)
        run_step("select_guest_checkout", self.select_guest_checkout, billing_profile.email)
        _log.info("run_pre_card_checkout_prepare: completed")

    def run_payment_card_fill(self, card_info, billing_profile) -> None:
        """Fill card and billing payment fields (INV-PAYMENT-01 gate step).

        Delegates to ``fill_payment_and_billing``.  MUST only be called AFTER
        the Phase A pricing watchdog has confirmed the order total so that no
        card data is ever typed on an unconfirmed-total page.

        Args:
            card_info: CardInfo with card number, expiry, CVV.
            billing_profile: BillingProfile with billing address.
        """
        _log.info("run_payment_card_fill: started")
        self.fill_payment_and_billing(card_info, billing_profile)
        _log.info("run_payment_card_fill: completed")

    def run_preflight_and_fill(self, task, billing_profile) -> None:
        """Backward-compatible alias: run_pre_card_checkout_prepare + run_payment_card_fill.

        Executes steps 1–6 of the full purchase sequence in order.  Retained
        for external callers and older tests that call this method directly;
        new code should prefer the two split methods so that the Phase A
        pricing watchdog can be waited between them.

        Args:
            task: WorkerTask with purchase details.
            billing_profile: BillingProfile with address and email.
        """
        self.run_pre_card_checkout_prepare(task, billing_profile)
        self.run_payment_card_fill(task.primary_card, billing_profile)

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
            _log.debug(
                "run_full_cycle: persona_archetype=%s persona_type=%s",
                self._persona.persona_archetype, self._persona.persona_type,
            )
        # Geo-check is a per-cycle invariant: skip when the worker entrypoint
        # (``integration.worker_task``) already ran it immediately after
        # ``BitBrowserSession.__enter__``.  The flag is reset implicitly per
        # cycle because a fresh ``GivexDriver`` is constructed per cycle.
        if self._geo_checked_this_cycle is not True:
            self.preflight_geo_check()
        self.navigate_to_egift()
        self.fill_egift_form(task, billing_profile)
        self.add_to_cart_and_checkout()
        self.select_guest_checkout(billing_profile.email)
        self.fill_payment_and_billing(task.primary_card, billing_profile)
        # E3 audit: wire ``task.amount`` (source-of-truth expected total)
        # before ``submit_purchase`` so the in-driver full-cycle path gets
        # the same DOM cross-check the orchestrator path gets via
        # ``set_expected_total``.  Skip only when a caller already pre-wired.
        if self._expected_total is None and getattr(task, "amount", None) is not None:
            try:
                self.set_expected_total(task.amount)
            except ValueError:
                _log.warning(
                    "run_full_cycle: task.amount=%r not parseable; "
                    "submit will proceed without DOM cross-check",
                    getattr(task, "amount", None),
                )
        self.submit_purchase()
        return self.detect_page_state()
