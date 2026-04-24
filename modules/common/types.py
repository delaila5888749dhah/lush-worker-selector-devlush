from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class State:
    name: str


@dataclass(frozen=True)
class CardInfo:
    card_number: str
    exp_month: str
    exp_year: str
    cvv: str
    card_name: str = ""


@dataclass(frozen=True)
class BillingProfile:
    first_name: str
    last_name: str
    address: str
    city: str
    state: str
    zip_code: str
    phone: str | None
    email: str | None
    country: str = "US"


@dataclass(frozen=True)
class WorkerTask:
    recipient_email: str
    amount: int
    primary_card: CardInfo
    order_queue: tuple[CardInfo, ...]
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError(
                f"task_id must be a non-empty string, got {self.task_id!r}"
            )
        if len(self.task_id.strip()) == 0:
            raise ValueError("task_id must not be blank or whitespace-only")
        if not isinstance(self.recipient_email, str) or not self.recipient_email.strip():
            raise ValueError(
                f"recipient_email must be a non-empty string, got {self.recipient_email!r}"
            )
        if not isinstance(self.amount, int) or isinstance(self.amount, bool) or self.amount <= 0:
            raise ValueError(
                f"amount must be a positive integer, got {self.amount!r}"
            )
        if not isinstance(self.primary_card, CardInfo):
            raise ValueError(
                f"primary_card must be a CardInfo instance, got {type(self.primary_card)!r}"
            )
        if not isinstance(self.order_queue, tuple):
            raise TypeError(
                "order_queue must be a tuple to preserve immutability, "
                f"got {type(self.order_queue)!r}"
            )
        for i, card in enumerate(self.order_queue):
            if not isinstance(card, CardInfo):
                raise TypeError(
                    f"order_queue[{i}] must be a CardInfo instance, got {type(card)!r}"
                )


@dataclass
class CycleContext:
    """Mutable context object scoped to one payment cycle (across card-swap retries).

    ``billing_profile`` is populated once by :func:`integration.orchestrator.run_cycle`
    on the first attempt and **reused on all subsequent card-swap retries** within
    the same cycle.  This ensures billing name, address, phone, and email remain
    constant while only the card fields change.

    ``swap_count`` tracks how many card swaps have occurred in the cycle and is
    incremented by the orchestrator on each card swap.

    A new :class:`CycleContext` must be created for every fully new cycle (new
    order / new worker run).
    """

    cycle_id: str
    worker_id: str
    billing_profile: Optional[BillingProfile] = None
    zip_code: Optional[str] = None
    card_attempts: int = 0
    task: Optional[WorkerTask] = None
    swap_count: int = 0
    utc_offset_hours: float = 0.0
