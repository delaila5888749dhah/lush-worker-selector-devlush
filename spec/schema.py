"""Spec schema — contract-only definitions.

DEPRECATED: Runtime code should import from modules.common instead.
  - Types:      modules.common.types (State, CardInfo, BillingProfile, WorkerTask)
  - Exceptions: modules.common.exceptions (InvalidStateError, etc.)

This file is retained as a contract reference only.  It MUST NOT be
imported by modules/ at runtime (violates spec ↔ implementation boundary).
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class State:
    name: str


@dataclass
class CardInfo:
    card_number: str
    exp_month: str
    exp_year: str
    cvv: str


@dataclass
class BillingProfile:
    first_name: str
    last_name: str
    address: str
    city: str
    state: str
    zip_code: str
    phone: Optional[str]
    email: Optional[str]


@dataclass
class WorkerTask:
    recipient_email: str
    amount: int
    primary_card: CardInfo
    order_queue: List[CardInfo]


class SessionFlaggedError(Exception):
    pass


class CycleExhaustedError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass