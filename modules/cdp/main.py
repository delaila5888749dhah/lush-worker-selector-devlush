"""CDP — Chrome DevTools Protocol interaction stubs.

Provides a per-worker driver registry so that the orchestrator can
associate a browser driver with each worker_id. Business logic
implementation delegates to the registered driver for page interaction.
"""

import logging
import os
import re
import signal
import threading
from typing import Dict, Optional

_log = logging.getLogger(__name__)

_registry_lock = threading.Lock()
_driver_registry: dict[str, object] = {}
_pid_registry: dict[str, int] = {}
_bitbrowser_registry: Dict[str, str] = {}

_CARD_PATTERN = re.compile(r"\b\d{16}\b")
_CVV_PATTERN = re.compile(r"\bcvv\b[\s:=_-]*\d{3,4}\b", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _sanitize_error(msg: str) -> str:
    """Redact sensitive PII from an error message string.

    Replaces 16-digit card numbers, CVV patterns, and email addresses
    with placeholder tokens so that sensitive data is never exposed in
    logs or re-raised exception messages.

    Args:
        msg: The raw error message that may contain PII.

    Returns:
        The message with all recognised PII replaced.
    """
    msg = _CARD_PATTERN.sub("[REDACTED-CARD]", msg)
    msg = _CVV_PATTERN.sub("[REDACTED-CVV]", msg)
    msg = _EMAIL_PATTERN.sub("[REDACTED-EMAIL]", msg)
    return msg


def register_driver(worker_id: str, driver: object) -> None:
    """Register a browser driver instance for the given worker."""
    with _registry_lock:
        _driver_registry[worker_id] = driver


def unregister_driver(worker_id: str) -> None:
    """Remove the driver entry for the given worker."""
    with _registry_lock:
        _driver_registry.pop(worker_id, None)


def _get_driver(worker_id: str) -> object:
    """Retrieve the driver registered for worker_id.

    Args:
        worker_id: Unique identifier for the worker whose driver to retrieve.

    Returns:
        The registered driver object.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    with _registry_lock:
        driver = _driver_registry.get(worker_id)
    if driver is None:
        raise RuntimeError(
            f"No driver registered for worker '{worker_id}'; "
            "call register_driver() first."
        )
    return driver


def _register_pid(worker_id: str, pid: int) -> None:
    """Store the browser process PID for the given worker.

    Args:
        worker_id: Unique identifier for the worker.
        pid: OS process ID of the browser process.
    """
    with _registry_lock:
        _pid_registry[worker_id] = pid


def force_kill(worker_id: str) -> None:
    """Forcibly terminate the browser process registered for worker_id.

    Sends SIGKILL to the registered PID. Falls back to SIGTERM if SIGKILL
    is not available on the platform. Removes the PID from the registry
    after sending the signal. No-op if no PID is registered.

    Args:
        worker_id: Unique identifier for the worker whose browser to kill.
    """
    with _registry_lock:
        pid = _pid_registry.pop(worker_id, None)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        _log.debug("force_kill: SIGKILL failed for worker %r pid %d: %s", worker_id, pid, exc)
    except AttributeError:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            _log.debug("force_kill: SIGTERM fallback failed for worker %r pid %d: %s", worker_id, pid, exc)


def detect_page_state(worker_id: str) -> str:
    """Detect the current page state via the registered driver.

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Returns:
        The detected page state as a string.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
        SelectorTimeoutError: if the driver cannot locate a required element
            within the allowed timeout (propagated from the driver).
        PageStateError: if the driver detects a page state that cannot be
            mapped to a known FSM state (propagated from the driver).
    """
    return _get_driver(worker_id).detect_page_state()


def fill_card(card_info, worker_id: str) -> None:
    """Fill card form fields via the registered driver.

    Args:
        card_info: CardInfo instance with card number, expiry, and CVV.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).fill_card(card_info)


def fill_billing(billing_profile, worker_id: str) -> None:
    """Fill billing form fields via the registered driver.

    Args:
        billing_profile: BillingProfile instance with address and contact info.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).fill_billing(billing_profile)


def fill_payment_and_billing(card_info, billing_profile, worker_id: str) -> None:
    """Fill both card payment and billing fields in a single call.

    Delegates to ``GivexDriver.fill_payment_and_billing(card_info,
    billing_profile)``.  This is the preferred API; the separate
    ``fill_card`` / ``fill_billing`` helpers are kept for backward
    compatibility only.

    Args:
        card_info: CardInfo instance with card number, expiry, and CVV.
        billing_profile: BillingProfile instance with address and contact info.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).fill_payment_and_billing(card_info, billing_profile)


def clear_card_fields(worker_id: str) -> None:
    """Clear card form fields via the registered driver.

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).clear_card_fields()


def preflight_geo_check(worker_id: str) -> str:
    """Run the geo pre-flight check via the registered driver.

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Returns:
        ``"US"`` when the primary API confirms a US IP, or ``"UNKNOWN"``
        when the API is unavailable and the fallback is used.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
        RuntimeError: if the detected country is not ``"US"``.
    """
    return _get_driver(worker_id).preflight_geo_check()


def navigate_to_egift(worker_id: str) -> None:
    """Navigate to the eGift purchase page via the registered driver.

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).navigate_to_egift()


def fill_egift_form(task, billing_profile, worker_id: str) -> None:
    """Fill the eGift order form via the registered driver.

    Args:
        task: WorkerTask with ``recipient_email`` and ``amount``.
        billing_profile: BillingProfile with name details.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).fill_egift_form(task, billing_profile)


def add_to_cart_and_checkout(worker_id: str) -> None:
    """Click Add-to-Cart and Review & Checkout via the registered driver.

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).add_to_cart_and_checkout()


def select_guest_checkout(email: str, worker_id: str) -> None:
    """Complete the guest checkout flow via the registered driver.

    Args:
        email: Guest email address to use for checkout.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).select_guest_checkout(email)


def submit_purchase(worker_id: str) -> None:
    """Submit the purchase via the registered driver.

    This is the irreversible action that charges the card.  It must only
    be called after the idempotency checkpoint has been persisted (U-07).

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).submit_purchase()


def run_preflight_and_fill(task, billing_profile, worker_id: str) -> None:
    """Run all pre-submit purchase steps via the registered driver.

    Executes steps 1–6 of the full purchase sequence (everything up to and
    including form fill), but intentionally omits the submit step so that the
    orchestrator can persist the idempotency checkpoint between fill and
    submit (U-07):

    1. Geo pre-flight check (``preflight_geo_check``).
    2. Navigate to eGift page (``navigate_to_egift``).
    3. Fill the eGift form (``fill_egift_form``).
    4. Add to cart and click Review & Checkout (``add_to_cart_and_checkout``).
    5. Select guest checkout (``select_guest_checkout``).
    6. Fill payment and billing fields (``fill_payment_and_billing``).

    Args:
        task: WorkerTask with purchase details.
        billing_profile: BillingProfile with address and email.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
        ValueError: if ``billing_profile.email`` is ``None``.
    """
    if billing_profile.email is None:
        raise ValueError(
            "billing_profile.email must not be None for guest checkout"
        )
    driver = _get_driver(worker_id)
    driver.preflight_geo_check()
    driver.navigate_to_egift()
    driver.fill_egift_form(task, billing_profile)
    driver.add_to_cart_and_checkout()
    driver.select_guest_checkout(billing_profile.email)
    driver.fill_payment_and_billing(task.primary_card, billing_profile)


def run_full_purchase_flow(task, billing_profile, worker_id: str) -> str:
    """Run the complete purchase sequence via the registered driver.

    Delegates to ``GivexDriver.run_full_cycle``.  Prefer
    ``run_preflight_and_fill`` + ``submit_purchase`` from the orchestrator
    so that the idempotency checkpoint can be persisted between fill and
    submit (U-07).

    Args:
        task: WorkerTask with purchase details.
        billing_profile: BillingProfile with address and email.
        worker_id: Unique identifier for the worker whose driver to use.

    Returns:
        The FSM state string returned by ``detect_page_state()``.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    return _get_driver(worker_id).run_full_cycle(task, billing_profile)


def register_browser_profile(worker_id: str, profile_id: str) -> None:
    """Register BitBrowser profile id for a worker."""
    with _registry_lock:
        _bitbrowser_registry[worker_id] = profile_id


def get_browser_profile(worker_id: str) -> Optional[str]:
    """Get BitBrowser profile id for a worker, if present."""
    with _registry_lock:
        return _bitbrowser_registry.get(worker_id)
