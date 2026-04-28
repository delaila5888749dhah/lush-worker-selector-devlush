# Interface Contract — Core (FSM)

spec-version: 7.0

> **v7.0 Breaking Changes:**
> - Added CDPError exception type to modules.common.exceptions (raised by GivexDriver.clear_card_fields_cdp on CDP failure — P1-4)
>
> **v6.0 Breaking Changes:**
> - Added CDPCommandError exception type to modules.common.exceptions (inherits SessionFlaggedError)
>
> **v5.0 Breaking Changes:**
> - CDP functions (detect_page_state, fill_card, fill_billing, clear_card_fields) now require worker_id parameter
> - Added reset_session(worker_id) public API to watchdog module
>
> **v4.0 Breaking Changes:**
> - Added SelectorTimeoutError and PageStateError exception types to modules.common.exceptions
> - WorkerTask is now frozen (immutable)
>
> **v2.0 Breaking Changes:**
> - Exception types (InvalidStateError, InvalidTransitionError) moved to modules.common.exceptions
> - State type moved to modules.common.types
> - spec/ is no longer a runtime dependency

## Module: fsm

Function: add_new_state
Input:
  - state_name
Output: State
Error:
  - Raise InvalidStateError if state_name is not in ALLOWED_STATES
  - Raise ValueError if state_name already exists in registry

Function: get_current_state
Input: None
Output: State | None

Function: transition_to
Input:
  - target_state
Output: State
Error:
  - Raise InvalidStateError if target_state is not in ALLOWED_STATES
  - Raise InvalidTransitionError if target_state is not registered

Function: reset_states
Input: None
Output: None
Notes:
  - Clears registry (_states.clear())
  - Resets current_state to None
  - After reset, transition_to will raise InvalidTransitionError

## Per-Worker API

Function: initialize_for_worker
Input:
  - worker_id: str
Output: None
Notes:
  - Resets and re-registers all allowed states for the given worker_id

Function: get_current_state_for_worker
Input:
  - worker_id: str
Output: State | None
Notes:
  - Returns the current state for the given worker_id
  - Returns None if no current state exists for the given worker_id

Function: transition_for_worker
Input:
  - worker_id: str
  - target_state: str
  - trace_id: str | None = None
Output: State
Error:
  - Raise InvalidStateError if target_state is not in ALLOWED_STATES
  - Raise InvalidTransitionError if target_state is not registered for worker_id
  - Raise ValueError if the transition is not permitted by the payment transition graph
Notes:
  - Emits structured INFO log "FSM_TRANSITION worker_id=… from=… to=… trace_id=…" on every successful transition
  - Emits structured WARN log "FSM_TRANSITION_REJECTED … reason=out_of_band|terminal trace_id=…" when a transition is rejected by the payment transition graph or by the terminal-state guard
  - trace_id is an optional correlation identifier included verbatim in the structured log; when omitted it is logged as "-"

Function: cleanup_worker
Input:
  - worker_id: str
Output: None
Notes:
  - Removes all FSM state for the given worker_id from the per-worker registry
