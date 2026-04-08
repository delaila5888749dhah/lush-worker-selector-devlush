import uuid
from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class State:
    name: str


@dataclass(frozen=True)
class CardInfo:
    card_number: str
    exp_month: str
    exp_year: str
    cvv: str


@dataclass(frozen=True)
class BillingProfile:
    first_name: str
    last_name: str
    address: str
    city: str
    state: str
    zip_code: str
    phone: Optional[str]
    email: Optional[str]


@dataclass(frozen=True)
class WorkerTask:
    recipient_email: str
    amount: int
    primary_card: CardInfo
    order_queue: Tuple[CardInfo, ...]
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        if self.task_id is None:
            raise ValueError("task_id must not be None")
