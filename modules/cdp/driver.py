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
import re as _re
import secrets
import socket
import threading
import time
import datetime
import importlib
import ipaddress
import urllib.parse
import urllib.request
import urllib.error

try:
    from selenium.webdriver.support.ui import Select  # type: ignore[import]
except ImportError:  # pragma: no cover - tests mock _cdp_select_option
    Select = None  # type: ignore[assignment,misc]

try:
    from selenium.webdriver.common.action_chains import ActionChains as _ActionChains  # type: ignore[import]
    from selenium.webdriver.common.by import By  # type: ignore[import]
    from selenium.webdriver.support.ui import WebDriverWait  # type: ignore[import]
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore[import]
    from selenium.common.exceptions import TimeoutException  # type: ignore[import]
except ImportError:  # pragma: no cover - selenium always present in prod
    _ActionChains = By = WebDriverWait = EC = None  # type: ignore[assignment,misc]
    TimeoutException = Exception  # type: ignore[assignment,misc]

try:
    from modules.cdp.mouse import GhostCursor as _GhostCursor
    from modules.cdp.keyboard import type_value as _type_value
except ImportError:  # pragma: no cover - defensive; mouse.py/keyboard.py always present
    _GhostCursor = None  # type: ignore[assignment,misc]
    _type_value = None  # type: ignore[assignment,misc]

from modules.common.exceptions import (
    CDPCommandError,
    PageStateError,
    SelectorTimeoutError,
    SessionFlaggedError,
)


class GeoCheckFailedError(RuntimeError):
    """Raised by :func:`preflight_geo_check` when the detected country does
    not match the expected country (default ``"US"``).

    Blueprint §2: pre-flight geo assertion — caller may choose to abort the
    cycle and rotate proxy/session on this error.
    """

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

# ── PII redaction patterns ────────────────────────────────────────────────
# Matches 16-digit PAN-like sequences in plain (4111111111111111),
# space-separated (4111 1111 1111 1111), and dash-separated
# (4111-1111-1111-1111) formats.
_CARD_PAN_RE = _re.compile(r"(?<!\d)\d{4}(?:[ -]?\d{4}){3}(?!\d)")
_CVV_RE = _re.compile(r"\bcvv\s*=\s*\d{3,4}\b", _re.IGNORECASE)
_EMAIL_RE = _re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _sanitize_error(msg: str) -> str:
    """Redact PAN-like card numbers, CVV patterns, and emails from *msg*.

    Handles card numbers in plain (``4111111111111111``), space-separated
    (``4111 1111 1111 1111``), and dash-separated (``4111-1111-1111-1111``)
    formats so that sensitive data is never exposed in logs or re-raised
    exception messages from the driver layer.

    Args:
        msg: The raw message that may contain PII.

    Returns:
        The message with all recognised PII replaced by placeholder tokens.
    """
    msg = _CARD_PAN_RE.sub("[REDACTED-CARD]", msg)
    msg = _CVV_RE.sub("[REDACTED-CVV]", msg)
    msg = _EMAIL_RE.sub("[REDACTED-EMAIL]", msg)
    return msg


# ── URL constants ─────────────────────────────────────────────────────────
URL_GEO_CHECK = "https://lumtest.com/myip.json"
URL_BASE      = "https://wwws-usa2.givex.com/cws4.0/lushusa/"
URL_EGIFT     = os.getenv(
    "GIVEX_EGIFT_URL",
    "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/",
)
URL_CART      = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/shopping-cart.html"
URL_CHECKOUT  = "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/checkout.html"
URL_PAYMENT   = os.getenv(
    "GIVEX_PAYMENT_URL",
    "https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/guest/payment.html",
)

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
                    return int(offset.total_seconds() // 3600)
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
            return int(offset.total_seconds() // 3600)
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


def preflight_geo_check(driver, expected_country: str = "US") -> dict:
    """Navigate to ``URL_GEO_CHECK`` and assert the public IP is in ``expected_country``.

    Blueprint §2: strict pre-flight geo assertion executed once per cycle.
    Unlike the :meth:`GivexDriver.preflight_geo_check` method (which also
    drives the UTC-offset adjustment for typing cadence), this standalone
    helper focuses on the single concern of asserting the proxy geo and
    raising a typed exception that the caller can route.

    Args:
        driver: A Selenium-compatible driver exposing ``get`` and
            ``find_element``.
        expected_country: Two-letter country code to assert against the
            ``country`` field in the JSON response.  Defaults to ``"US"``.

    Returns:
        The parsed JSON response dict on success.

    Raises:
        GeoCheckFailedError: When the JSON response's ``country`` field does
            not equal ``expected_country`` or the response body is malformed.
        SessionFlaggedError: When the browser window is lost mid-navigation
            (``NoSuchWindowException``) — caller should rotate session.
    """
    # Import selenium exception lazily so the module remains importable in
    # environments where selenium is stubbed/absent.
    try:
        from selenium.common.exceptions import NoSuchWindowException  # noqa: PLC0415
    except ImportError:  # pragma: no cover - selenium always present in prod
        class NoSuchWindowException(Exception):  # type: ignore[no-redef]
            """Fallback stub when selenium is unavailable."""

    try:
        driver.get(URL_GEO_CHECK)
        body = driver.find_element("tag name", "body").text
    except NoSuchWindowException as exc:
        raise SessionFlaggedError("Window lost during geo check") from exc

    try:
        data = _json.loads(body)
    except (ValueError, TypeError) as exc:
        raise GeoCheckFailedError(
            f"Malformed geo-check response: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise GeoCheckFailedError(
            f"Geo-check response was not a JSON object: {type(data).__name__}"
        )
    actual = data.get("country")
    if actual != expected_country:
        raise GeoCheckFailedError(
            f"Expected country {expected_country!r}, got {actual!r}"
        )
    return data


_HARD_RESET_JS = """
try { window.localStorage.clear(); } catch(e) {}
try { window.sessionStorage.clear(); } catch(e) {}
try {
  document.cookie.split(';').forEach(function(c) {
    document.cookie = c.replace(/^ +/, '').replace(/=.*/, '=;expires=' + new Date().toUTCString() + ';path=/');
  });
} catch(e) {}
"""


def hard_reset_browser_state(driver) -> None:
    """Clear cookies, localStorage, and sessionStorage (Blueprint §3 Hard-Reset).

    Must be called AFTER navigating to the target same-origin URL (e.g.
    ``/e-gifts/``) but BEFORE any form interaction.  The JS block wraps each
    storage clear in its own ``try/catch`` so a page with disabled storage
    APIs does not abort the reset.  ``driver.delete_all_cookies`` is invoked
    as a Selenium-level safety net; exceptions from either step are logged
    and swallowed so the caller may continue.
    """
    try:
        driver.execute_script(_HARD_RESET_JS)
    except Exception as exc:  # pylint: disable=broad-except
        _log.debug("hard_reset_browser_state: execute_script skipped: %s", exc)
    try:
        driver.delete_all_cookies()
    except Exception as exc:  # pylint: disable=broad-except
        _log.debug("hard_reset_browser_state: delete_all_cookies skipped: %s", exc)


def handle_ui_lock_focus_shift(driver) -> bool:
    """Focus-Shift Retry per Blueprint §6 Ngã rẽ 1.

    Steps:
      1. Click ``SEL_NEUTRAL_DIV`` (``body``) to shift focus away from the
         locked submit button.
      2. Wait 0.5s to let any animation settle.
      3. Re-locate ``SEL_COMPLETE_PURCHASE`` and click it once via
         ``ActionChains.click``.

    This helper executes **exactly once** per invocation and never retries
    internally — the caller is responsible for enforcing the one-retry-per-
    cycle cap (Blueprint rule).  Returns ``True`` on apparent success and
    ``False`` on any exception (already logged).

    Args:
        driver: Selenium-compatible driver.
    """
    if _ActionChains is None:  # pragma: no cover - selenium always present in prod
        _log.warning("handle_ui_lock_focus_shift: ActionChains unavailable")
        return False
    try:
        neutral = driver.find_element("css selector", SEL_NEUTRAL_DIV)
        _ActionChains(driver).move_to_element(neutral).click().perform()
        time.sleep(0.5)
        btn = driver.find_element("css selector", SEL_COMPLETE_PURCHASE)
        _ActionChains(driver).move_to_element(btn).click().perform()
        return True
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("focus_shift_retry failed: %s", exc)
        return False


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
    rng = rng or _random
    base = getattr(driver, "_driver", driver)
    by = By.CSS_SELECTOR if By is not None else "css selector"
    iframe = base.find_element(by, iframe_selector)
    base.switch_to.frame(iframe)
    elem = base.find_element(by, element_selector)
    er = base.execute_script("const r=arguments[0].getBoundingClientRect();return {left:r.left,top:r.top,width:r.width,height:r.height};", elem)
    base.switch_to.default_content()
    ir = base.execute_script("const r=arguments[0].getBoundingClientRect();return {left:r.left,top:r.top};", iframe)
    abs_x = ir["left"] + er["left"] + er["width"] / 2 + rng.uniform(-15, 15)
    abs_y = ir["top"] + er["top"] + er["height"] / 2 + rng.uniform(-5, 5)
    for evt in ("mousePressed", "mouseReleased"):
        base.execute_cdp_cmd("Input.dispatchMouseEvent",
            {"type": evt, "x": abs_x, "y": abs_y, "button": "left", "clickCount": 1})
    return abs_x, abs_y


def _popup_use_xpath() -> bool:
    """Return True if the XPath text-match locator should be used (P1-1).

    Default: True. Set env ``POPUP_USE_XPATH=0`` (or ``false``/``no``) to
    roll back to the legacy CSS selector (:data:`SEL_POPUP_SOMETHING_WRONG`).
    """
    raw = os.environ.get("POPUP_USE_XPATH", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def handle_something_wrong_popup(driver, timeout: float = 2.0) -> bool:
    """Click Close on the 'Something went wrong' popup if present (Blueprint §6 Fork 3).

    P1-1: By default uses an XPath text-match locator so generic ``.modal`` /
    ``.popup`` elements (cookie banners, newsletter modals, success modals)
    without the target text do NOT trigger a false-positive close. Set env
    ``POPUP_USE_XPATH=0`` to roll back to the legacy CSS selector.
    """
    if WebDriverWait is None or EC is None or By is None:
        return False
    base = getattr(driver, "_driver", driver)
    if _popup_use_xpath():
        locator = (By.XPATH, XPATH_POPUP_SWW)
    else:
        locator = (By.CSS_SELECTOR, SEL_POPUP_SOMETHING_WRONG)
    try:
        WebDriverWait(base, timeout).until(
            EC.presence_of_element_located(locator))
    except TimeoutException:
        return False
    try:
        driver.bounding_box_click(SEL_POPUP_CLOSE)
        return True
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("popup close failed: %s", exc)
        return False


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

    def handle_vbv_challenge(self) -> bool:
        """Cancel VBV/3DS iframe challenge (Blueprint §6 Fork 3); swallow all errors."""
        try:
            vbv_dynamic_wait(rng=self._get_rng())
            cdp_click_iframe_element(self, SEL_VBV_IFRAME, SEL_VBV_CANCEL_BTN, rng=self._get_rng())
            handle_something_wrong_popup(self)
            return True
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning("handle_vbv_challenge failed: %s", _sanitize_error(str(exc)))
            return False

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
        # Billing section
        self._cdp_type_field(SEL_BILLING_ADDRESS, billing_profile.address)
        self._cdp_select_option(SEL_BILLING_COUNTRY, billing_profile.country)
        self._cdp_select_option(SEL_BILLING_STATE, billing_profile.state)
        self._cdp_type_field(SEL_BILLING_CITY, billing_profile.city)
        self._cdp_type_field(SEL_BILLING_ZIP, billing_profile.zip_code)
        if billing_profile.phone:
            self._cdp_type_field(SEL_BILLING_PHONE, billing_profile.phone)

    def fill_card_fields(self, card_info) -> None:
        """Fill card fields only (no billing); used after VBV reload."""
        self.fill_payment_and_billing(card_info, billing_profile=None)

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

    def clear_card_fields_cdp(self) -> None:
        """Clear card number + CVV via CDP Ctrl+A + Backspace (Blueprint §6 Fork 4)."""
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
            except Exception:  # pylint: disable=broad-except
                _log.warning("clear_card_fields_cdp failed; continuing")

    def clear_card_fields(self) -> None:
        """Clear all card form fields (best-effort)."""
        self.clear_card_fields_cdp()

    # ── Post-submit state detection (Step 5) ─────────────────────────────────

    def detect_page_state(self) -> str:
        """Inspect the current page and return the FSM state name.

        Detection order:
        1. ``success``   — URL contains a confirmation fragment, OR
                           ``.order-confirmation`` element is present, OR
                           page text contains "thank you for your order"
                           (SPA fallback when URL does not change).
        2. ``vbv_3ds``   — A 3-D Secure / Adyen iframe is present.
        3. ``declined``  — URL contains ``error=vv`` (Givex VBV/3DS failure
                           signal), OR a payment-error element is present, OR
                           page text contains "declined" / "transaction failed".
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
        # SPA fallback: DOM renders confirmation text without URL change
        page_text = self._driver.find_element("tag name", "body").text.lower()
        if "thank you for your order" in page_text:
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
