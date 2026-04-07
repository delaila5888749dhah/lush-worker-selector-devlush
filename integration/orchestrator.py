"""Orchestration layer — coordinates FSM, Watchdog, Billing, and CDP modules.

All inter-module communication uses modules.common types only.
No cross-module imports exist within the individual modules themselves;
this file is the single integration point that wires them together.
"""

import logging
import threading

from modules.billing import main as billing
from modules.cdp import main as cdp
from modules.fsm import main as fsm
from modules.monitor import main as monitor
from modules.rollout import main as rollout
from modules.watchdog import main as watchdog

_FSM_STATES = ("ui_lock", "success", "vbv_3ds", "declined")
_WATCHDOG_TIMEOUT = 30

_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def initialize_cycle(worker_id: str = "default"):
    """Reset FSM registry and register all valid states for a new cycle."""
    rollout.configure(monitor.check_rollback_needed, monitor.save_baseline)
    fsm.initialize_for_worker(worker_id)


def run_payment_step(task, zip_code=None, worker_id: str = "default"):
    """Execute one payment attempt.

    Steps:
      1. Select a billing profile from the pool.
      2. Enable the network watchdog for this worker.
      3. Fill billing and card data via CDP.
      4. Wait for the checkout total to be confirmed by the watchdog.
      5. Return (state, total).

    Args:
        task: WorkerTask containing card and order information.
        zip_code: Optional zip code for billing profile matching.
        worker_id: Unique identifier for this worker (used to key the watchdog session).

    Returns:
        A (state, total) tuple where state is a State object or None,
        and total is the confirmed checkout amount.

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out waiting for the total.
        NotImplementedError: if CDP functions are not yet implemented.
    """
    profile = billing.select_profile(zip_code)
    watchdog.enable_network_monitor(worker_id)
    cdp.fill_billing(profile)
    cdp.fill_card(task.primary_card)
    total = watchdog.wait_for_total(worker_id, timeout=_WATCHDOG_TIMEOUT)
    state = fsm.get_current_state_for_worker(worker_id)
    return state, total


def handle_outcome(state, order_queue, worker_id: str = "default"):
    """Determine the next action based on the current FSM state.

    Args:
        state: Current State object (or None if FSM was never transitioned).
        order_queue: Remaining cards available for swap.
        worker_id: Unique identifier for this worker (used for log context).

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
        try:
            cdp.clear_card_fields()
        except Exception:
            _logger.warning(
                "cdp.clear_card_fields() failed for worker=%s during vbv_3ds "
                "handling; proceeding to await_3ds",
                worker_id,
                exc_info=True,
            )
        return "await_3ds"
    return "retry"


def run_cycle(task, zip_code=None, worker_id: str = "default"):
    """Run a full payment cycle for a WorkerTask.

    Initializes the FSM, executes one payment attempt, and returns the
    outcome action together with the final state and confirmed total.

    Args:
        task: WorkerTask containing the recipient, amount, and card data.
        zip_code: Optional zip code for billing profile selection.
        worker_id: Unique identifier for this worker.

    Returns:
        A (action, state, total) tuple where action is one of:
        "complete" | "retry" | "retry_new_card" | "await_3ds".

    Raises:
        CycleExhaustedError: if the billing pool is empty.
        SessionFlaggedError: if the watchdog times out.
        NotImplementedError: if CDP functions are not yet implemented.
    """
    with _lock:
        initialize_cycle(worker_id)
    state, total = run_payment_step(task, zip_code, worker_id=worker_id)
    action = handle_outcome(state, task.order_queue, worker_id=worker_id)
    return action, state, total
