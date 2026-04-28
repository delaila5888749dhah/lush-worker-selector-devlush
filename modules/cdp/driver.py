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
    from modules.cdp.keyboard import type_value as _type_value
except ImportError:  # pragma: no cover - defensive; mouse.py/keyboard.py always present
    _GhostCursor = None  # type: ignore[assignment,misc]
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
except ImportError:
    _BiometricProfile = _TemporalModel = None
    _BehaviorStateMachine = _DelayEngine = None
    _get_current_sm = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

# ── MaxMind GeoLite2 singleton ────────────────────────────────────────────
# Loaded once at startup via init_maxmind_reader(); subsequent lookups reuse
# the open Reader object, keeping latency effectively <1ms (RAM only, no I/O).
_MAXMIND_READER = None  # pylint: disable=invalid-name
_MAXMIND_READER_LOCK = threading.Lock()


def init_maxmind_reader(mmdb_path: str | None = None) -> None:
    """Load the GeoLite2-City database into the module-level singleton.

    Call once at application startup, before any :func:`maxmind_lookup_zip` or
    :func:`_lookup_maxmind_utc_offset` calls, to preload the DB into RAM and
    eliminate per-lookup disk I/O.

    Args:
        mmdb_path: Override path to the ``.mmdb`` file.  Falls back to the
            ``GEOIP_DB_PATH`` environment variable, then the default
            ``data/GeoLite2-City.mmdb``.

    Raises:
        FileNotFoundError: If the database file is not found at the resolved path.
        ImportError: If the ``geoip2`` package is not installed.
    """
    global _MAXMIND_READER  # pylint: disable=global-statement,invalid-name
    path = mmdb_path or os.environ.get("GEOIP_DB_PATH", "data/GeoLite2-City.mmdb")
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
    """Return the configured MaxMind database file path."""
    return os.environ.get("GEOIP_DB_PATH", "data/GeoLite2-City.mmdb")


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
    "#cws_lbl_orderTotal, .order-total, .checkout-total, [data-total]"
)
# Tolerance for DOM-vs-expected total comparison; absorbs display rounding.
_ORDER_TOTAL_TOLERANCE = decimal.Decimal("0.01")


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
        mmdb_path = os.environ.get("GEOIP_DB_PATH", "data/GeoLite2-City.mmdb")
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
        mmdb_path = os.environ.get("GEOIP_DB_PATH", "data/GeoLite2-City.mmdb")
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

def close_extra_tabs(driver) -> int:
    """Close all browser tabs except the first one. Return count closed.

    Blueprint §2 Tab Janitor: BitBrowser profiles often open with extra
    ad/junk tabs.  The janitor must close them BEFORE pre-flight geo check
    so ``window_handles`` count does not confuse ``detect_page_state``.

    Args:
        driver: A Selenium-compatible driver exposing ``window_handles``,
            ``switch_to.window(handle)`` and ``close()``.

    Returns:
        The number of extra tabs successfully closed.  Individual close
        failures are swallowed with a warning log so the janitor never
        crashes the calling flow.
    """
    handles = driver.window_handles
    if len(handles) <= 1:
        return 0
    main = handles[0]
    closed = 0
    for handle in handles[1:]:
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
            self._sm.set_critical_section(True)
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
                self._sm.set_critical_section(False)
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
        if _type_value is None:
            if self._strict:
                _log.warning("_realistic_type_field: keyboard unavailable (strict)")
            self._send_keys_fallback(sel, val)
            return
        typo_prob = self._persona.get_typo_probability() if self._persona else 0.0
        if self._persona and self._temporal:
            typo_prob += self._temporal.get_night_typo_increase(self._utc_offset_hours)
        if typo_rate is not None:
            typo_prob = typo_rate
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
        else:
            dl = (self._bio.generate_4x4_pattern() if self._bio and use_burst and len(val) >= 16 else self._bio.generate_burst_pattern(len(val)) if self._bio else None)
        _type_value(
            self._driver, els[0], val, self._get_rng(),
            typo_rate=typo_prob, delays=dl, strict=self._strict,
            field_kind=field_kind, engine=self._engine,
        )

    def _cdp_select_option(self, selector: str, value: str) -> None:
        """Select the option matching *value* in a ``<select>`` element.

        Implementation strategy (audit finding [G2]):
          1. ``bounding_box_click`` opens/focuses the dropdown — produces an
             ``isTrusted=True`` mouse event indistinguishable from a user.
          2. A read-only ``execute_script`` query locates the target option
             index relative to the currently-selected one.
          3. ``ArrowDown`` / ``ArrowUp`` named-key events advance the
             highlight via CDP (``Input.dispatchKeyEvent`` with proper DOM
             ``code`` / virtual-key code → ``isTrusted=True``).
          4. ``Enter`` confirms the selection.

        This deliberately avoids ``Select.select_by_value`` (Selenium
        helper) because its ``change`` event fires with ``isTrusted=False``
        — a fingerprint that anti-fraud heuristics flag.

        Args:
            selector: CSS selector for the ``<select>`` element.
            value: The option ``value`` attribute to select.

        Raises:
            SelectorTimeoutError: if no matching element is found.
            ValueError: if no option with *value* exists in the select.
        """
        elements = self.find_elements(selector)
        if not elements:
            raise SelectorTimeoutError(selector, 0)

        # Step 1 — open/focus the dropdown via a real CDP mouse click.
        self.bounding_box_click(selector)

        # Step 2 — locate target index relative to currently-selected option.
        js = (
            "const sel = document.querySelector(arguments[0]);"
            "if (!sel) return [-1, -1];"
            "const opts = Array.from(sel.options);"
            "const currentIdx = sel.selectedIndex;"
            "const targetIdx = opts.findIndex(o => o.value === arguments[1]);"
            "return [currentIdx, targetIdx];"
        )
        result = self._driver.execute_script(js, selector, value)
        try:
            current_idx, target_idx = int(result[0]), int(result[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError(
                f"_cdp_select_option: unexpected option-index result "
                f"{result!r} for selector {selector!r}"
            ) from exc
        if target_idx < 0:
            raise ValueError(
                f"Option value={value!r} not found in {selector}"
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
                    f"_cdp_select_option: failed to dispatch {key!r} for {selector!r}",
                )
            jitter = self._rnd.uniform(0.0, 0.05) if self._rnd is not None else 0.0
            time.sleep(0.05 + jitter)

        # Step 4 — confirm the selection.  Same hardening: a failed Enter
        # leaves the highlight unchanged and must surface to the caller.
        if not dispatch_key(self._driver, "Enter"):
            raise CDPCommandError(
                "Input.dispatchKeyEvent",
                f"_cdp_select_option: failed to dispatch Enter for {selector!r}",
            )

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
        that only one window handle exists and it is in a clean state.
        """
        close_extra_tabs(self._driver)
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
        self._run_tab_janitor()
        max_attempts = 3  # 1 initial + 2 retries
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            # Always ensure focus on the main window on each attempt so
            # that a stray popup or closed tab does not starve the check.
            # If we cannot focus the main window, the attempt itself is
            # considered failed: running geo-check on the wrong context
            # would defeat the safeguard this method provides.
            try:
                handles = self._driver.window_handles
                if not handles:
                    raise RuntimeError(
                        "preflight_geo_check: no window handles available"
                    )
                self._driver.switch_to.window(handles[0])
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
            SEL_GREETING_MSG, _random_greeting(self._rnd), field_kind="text",
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
        self._realistic_type_field(SEL_GUEST_EMAIL, guest_email, field_kind="text")
        self.bounding_box_click(SEL_GUEST_CONTINUE)
        self._wait_for_url(URL_PAYMENT, timeout=15)

    # ── Payment & Billing (Step 4 — same page) ──────────────────────────────

    def fill_payment_and_billing(self, card_info, billing_profile) -> None:
        """Fill card (and, if given, billing) fields on the shared payment page."""
        if self._sm is not None:
            self._sm.transition("PAYMENT")
        self._realistic_type_field(SEL_CARD_NAME, card_info.card_name, field_kind="name")
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
                self._sm.set_critical_section(True)
            click_succeeded = False
            try:
                self.bounding_box_click(SEL_COMPLETE_PURCHASE)
                click_succeeded = True
            finally:
                if self._sm is not None:
                    self._sm.set_critical_section(False)
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
        current_url = self._driver.current_url

        # 1 — success
        if any(frag in current_url for frag in URL_CONFIRM_FRAGMENTS):
            return "success"
        # Gate DOM/text confirmation signals on the Givex host to avoid
        # false-positives from generic CSS classes or marketing copy on
        # unrelated pages.
        on_givex_host = URL_CONFIRM_HOST in current_url.lower()
        if on_givex_host and self.find_elements(SEL_CONFIRMATION_EL):
            return "success"
        # SPA fallback: DOM renders confirmation text without URL change
        page_text = self._driver.find_element("tag name", "body").text.lower()
        if on_givex_host and "thank you for your order" in page_text:
            return "success"

        # 2 — vbv_3ds
        if self.find_elements(SEL_VBV_IFRAME):
            return "vbv_3ds"

        # 3 — declined
        # Givex: error=vv là tín hiệu VBV/3DS thất bại (Verified by Visa / 3D Secure failed)
        if "error=vv" in current_url.lower():
            return "declined"
        if self.find_elements(SEL_DECLINED_MSG):
            return "declined"
        if "declined" in page_text or "transaction failed" in page_text:
            return "declined"

        # 4 — ui_busy (spinner visible means active loading, not a stuck UI)
        if self.find_elements(SEL_UI_LOCK_SPINNER):
            return "ui_busy"

        # 5 — 3s timeout fallback: sustained spinner-absent stall → ui_lock
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.3)
            current_url = self._driver.current_url
            if any(frag in current_url for frag in URL_CONFIRM_FRAGMENTS):
                return "success"
            if self.find_elements(SEL_VBV_IFRAME):
                return "vbv_3ds"
            if "error=vv" in current_url:
                return "declined"
            if self.find_elements(SEL_UI_LOCK_SPINNER):
                return "ui_busy"
        # After 3s with no spinner/state change → treat as stuck ui_lock
        return "ui_lock"

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
