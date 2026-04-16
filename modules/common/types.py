from __future__ import annotations

import uuid
from dataclasses import dataclass, field


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
                f"order_queue must be a tuple to preserve immutability, got {type(self.order_queue)!r}"
            )
        for i, card in enumerate(self.order_queue):
            if not isinstance(card, CardInfo):
                raise TypeError(
                    f"order_queue[{i}] must be a CardInfo instance, got {type(card)!r}"
                )
