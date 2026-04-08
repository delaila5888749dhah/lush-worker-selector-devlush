"""CDP — Chrome DevTools Protocol interaction stubs.

Provides a per-worker driver registry so that the orchestrator can
associate a browser driver with each worker_id. Business logic
implementation delegates to the registered driver for page interaction.
"""

import threading

_registry_lock = threading.Lock()
_driver_registry: dict[str, object] = {}


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


def detect_page_state(worker_id: str = "default") -> str:
    """Detect the current page state via the registered driver.

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Returns:
        The detected page state as a string.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    return _get_driver(worker_id).detect_page_state()


def fill_card(card_info, worker_id: str = "default") -> None:
    """Fill card form fields via the registered driver.

    Args:
        card_info: CardInfo instance with card number, expiry, and CVV.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).fill_card(card_info)


def fill_billing(billing_profile, worker_id: str = "default") -> None:
    """Fill billing form fields via the registered driver.

    Args:
        billing_profile: BillingProfile instance with address and contact info.
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).fill_billing(billing_profile)


def clear_card_fields(worker_id: str = "default") -> None:
    """Clear card form fields via the registered driver.

    Args:
        worker_id: Unique identifier for the worker whose driver to use.

    Raises:
        RuntimeError: if no driver has been registered for the given worker_id.
    """
    _get_driver(worker_id).clear_card_fields()
