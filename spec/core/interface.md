# Interface Contract — Core (FSM)

spec-version: 4.0

> **v4.0 Breaking Changes:**
> - Added CDPTimeoutError and CDPNavigationError exception types to modules.common.exceptions
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
