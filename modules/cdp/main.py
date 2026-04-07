"""CDP — Chrome DevTools Protocol interaction stubs.

Provides a per-worker driver registry so that the orchestrator can
associate a browser driver with each worker_id. Business logic
implementation will use the registered driver for page interaction.
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


def detect_page_state():
    raise NotImplementedError(
        "CDP detect_page_state not implemented. "
        "Deploy blocked until this function is implemented."
    )


def fill_card(card_info):
    raise NotImplementedError(
        "CDP fill_card not implemented. "
        "Deploy blocked until this function is implemented."
    )


def fill_billing(billing_profile):
    raise NotImplementedError(
        "CDP fill_billing not implemented. "
        "Deploy blocked until this function is implemented."
    )


def clear_card_fields():
    raise NotImplementedError(
        "CDP clear_card_fields not implemented. "
        "Deploy blocked until this function is implemented."
    )
