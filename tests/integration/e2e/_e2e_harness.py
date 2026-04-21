"""Shared helpers for the 14 P2-4 E2E tests (tests/integration/e2e/).

These helpers build on top of the existing ``_integration_harness`` stub
driver but add cards-with-suffix factories, idempotency reset context, and
patch groups that every E2E test needs.

Do NOT import across tests — each test file is intentionally a single,
self-contained file so that it can be executed in isolation by the CI
``make test-e2e`` target.
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import Optional, Sequence
from unittest.mock import MagicMock

# Make the sibling ``_integration_harness`` module importable regardless of
# how unittest discovers this suite (with or without __init__.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.cdp.main as _cdp_main  # noqa: E402  pylint: disable=wrong-import-position
from integration.orchestrator import (  # noqa: E402  pylint: disable=wrong-import-position
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _submitted_task_ids,
)
from modules.common.types import (  # noqa: E402  pylint: disable=wrong-import-position
    BillingProfile,
    CardInfo,
    WorkerTask,
)
from modules.fsm.main import cleanup_worker, reset_registry  # noqa: E402  pylint: disable=wrong-import-position
from _integration_harness import (  # noqa: E402  pylint: disable=wrong-import-position
    _StubGivexDriver,
    make_mock_billing,
)


_STORE_PATCH = "integration.orchestrator._get_idempotency_store"


def make_card(suffix: str = "1111", number: Optional[str] = None) -> CardInfo:
    """Build a CardInfo with a distinct last-4 digit suffix.

    Args:
        suffix: 4-digit suffix appended to a fixed BIN (used when ``number``
            is None).
        number: Optional full PAN (overrides ``suffix``).
    """
    return CardInfo(
        card_number=number or f"411111111111{suffix}",
        exp_month="12",
        exp_year="2030",
        cvv="123",
    )


def make_task(
    task_id: str = "e2e-task-001",
    order_queue: Sequence[CardInfo] = (),
    primary_card: Optional[CardInfo] = None,
    amount: int = 50,
) -> WorkerTask:
    return WorkerTask(
        task_id=task_id,
        recipient_email="recipient@example.test",
        amount=amount,
        primary_card=primary_card or make_card("0000"),
        order_queue=tuple(order_queue),
    )


def make_billing_profile() -> BillingProfile:
    return BillingProfile(
        first_name="E2E",
        last_name="Tester",
        address="1 Integration Way",
        city="New York",
        state="NY",
        zip_code="10001",
        phone="2125550123",
        email="billing@example.test",
    )


def fresh_store_mock() -> MagicMock:
    """Idempotency store where nothing is a duplicate yet."""
    store = MagicMock()
    store.is_duplicate.return_value = False
    return store


class E2EBase(unittest.TestCase):
    """Common setUp/tearDown for all P2-4 E2E tests."""

    worker_id = "p2-4-e2e-worker"

    def setUp(self):
        reset_registry()
        cleanup_worker(self.worker_id)
        with _idempotency_lock:
            _completed_task_ids.clear()
            _in_flight_task_ids.clear()
            _submitted_task_ids.clear()

    def tearDown(self):
        # cdp.unregister_driver is a no-op when worker_id is not registered
        # (it uses dict.pop with a default), so no guard is needed.
        _cdp_main.unregister_driver(self.worker_id)
        cleanup_worker(self.worker_id)


__all__ = [
    "E2EBase",
    "_STORE_PATCH",
    "_StubGivexDriver",
    "fresh_store_mock",
    "make_billing_profile",
    "make_card",
    "make_mock_billing",
    "make_task",
]
