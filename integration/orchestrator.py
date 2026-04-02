"""Orchestration layer — coordinates FSM, Watchdog, Billing, and CDP modules.

All inter-module communication uses modules.common types only.
No cross-module imports exist within the individual modules themselves;
this file is the single integration point that wires them together.
"""

import threading

from modules.common.exceptions import CycleExhaustedError, SessionFlaggedError
from modules.common.types import WorkerTask
from modules.billing import main as billing
from modules.cdp import main as cdp
from modules.fsm import main as fsm
from modules.watchdog import main as watchdog

_FSM_STATES = ("ui_lock", "success", "vbv_3ds", "declined")
_WATCHDOG_TIMEOUT = 30

_lock = threading.Lock()


def initialize_cycle():
    """Reset FSM registry and register all valid states for a new cycle."""
    fsm.reset_states()
    for state_name in _FSM_STATES:
        fsm.add_new_state(state_name)


def run_payment_step(task, zip_code=None):
    """Execute one payment attempt.

    Steps:
      1. Select a billing profile from the pool.
      2. Enable the network watchdog.
      3. Fill billing and card data via CDP.
      4. Wait for the checkout total to be confirmed by the watchdog.
      5. Return (state, total).

    Args:
        task: WorkerTask containing card and order information.
        zip_code: Optional zip code for billing profile matching.

    Returns:
        A (state, total) tuple where state is a State object or None,
        and total is the confirmed checkout amount.

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out waiting for the total.
        NotImplementedError: if CDP functions are not yet implemented.
    """
    profile = billing.select_profile(zip_code)
    watchdog.enable_network_monitor()
    cdp.fill_billing(profile)
    cdp.fill_card(task.primary_card)
    total = watchdog.wait_for_total(timeout=_WATCHDOG_TIMEOUT)
    state = fsm.get_current_state()
    return state, total


def handle_outcome(state, order_queue):
    """Determine the next action based on the current FSM state.

    Args:
        state: Current State object (or None if FSM was never transitioned).
        order_queue: Remaining cards available for swap.

    Returns:
        One of: "complete", "retry", "retry_new_card", "await_3ds".
    """
    if state is None:
        return "retry"
    if state.name == "success":
        return "complete"
    if state.name == "declined":
        return "retry_new_card" if order_queue else "retry"
    if state.name == "ui_lock":
        return "retry"
    if state.name == "vbv_3ds":
        cdp.clear_card_fields()
        return "await_3ds"
    return "retry"


def run_cycle(task, zip_code=None):
    """Run a full payment cycle for a WorkerTask.

    Initializes the FSM, executes one payment attempt, and returns the
    outcome action together with the final state and confirmed total.

    Args:
        task: WorkerTask containing the recipient, amount, and card data.
        zip_code: Optional zip code for billing profile selection.

    Returns:
        A (action, state, total) tuple where action is one of:
        "complete" | "retry" | "retry_new_card" | "await_3ds".

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out.
        NotImplementedError: if CDP functions are not yet implemented.
    """
    with _lock:
        initialize_cycle()
    state, total = run_payment_step(task, zip_code)
    action = handle_outcome(state, task.order_queue)
    return action, state, total
