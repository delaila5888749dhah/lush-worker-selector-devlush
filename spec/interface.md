# Interface Contract (Aggregated)

spec-version: 5.0

> **Contract Segmentation (v2.0):** Interface contracts have been split into
> two separate groups. This file aggregates both groups to maintain backward
> compatibility with the CI pipeline.
>
> - **Core (FSM):** [spec/core/interface.md](core/interface.md)
> - **Integration (Watchdog, Billing, CDP):** [spec/integration/interface.md](integration/interface.md)
>
> **v5.0 Breaking Changes:**
> - CDP functions (detect_page_state, fill_card, fill_billing, clear_card_fields) now require worker_id parameter
> - Added reset_session(worker_id) public API to watchdog module
>
> **v4.0 Breaking Changes:**
> - Added SelectorTimeoutError and PageStateError exception types to modules.common.exceptions
> - WorkerTask is now frozen (immutable)
> - Added register_driver and unregister_driver to CDP module
>
> **v2.0 Breaking Changes:**
> - Exception types moved from spec.schema to modules.common.exceptions
> - Data types moved from spec.schema to modules.common.types
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
Output: State
Error:
  - Raise InvalidStateError if target_state is not in ALLOWED_STATES
  - Raise InvalidTransitionError if target_state is not registered for worker_id
  - Raise ValueError if the transition is not permitted by the payment transition graph

Function: cleanup_worker
Input:
  - worker_id: str
Output: None
Notes:
  - Removes all FSM state for the given worker_id from the per-worker registry

## Module: watchdog

Function: enable_network_monitor
Input:
  - worker_id
Output: None

Function: wait_for_total
Input:
  - worker_id
  - timeout
Output: total value
Error:
  - Raise RuntimeError if enable_network_monitor() was not called for worker_id
  - Raise SessionFlaggedError if timeout expires

Function: notify_total
Input:
  - worker_id
  - value
Output: None
Notes:
  - Safe to call from any thread (browser CDP event thread, worker thread, etc.)
  - No-op if no session exists for worker_id

Function: reset_session
Input:
  - worker_id
Output: None
Notes:
  - Public API for orchestrator to clean up watchdog sessions

## Module: billing

Function: select_profile
Input:
  - zip_code
  - worker_id
Output: BillingProfile
Notes:
  - worker_id is optional (default None) — when provided, uses per-worker shuffled list with index pointer (P4 per-worker isolation)
  - worker_id=None preserves legacy global-deque behaviour for backward compatibility
  - Zip match: searches from state.index forward, returns match WITHOUT advancing pointer
  - No zip match: returns state.profiles[state.index], then index = (index + 1) % n

## Module: cdp

Function: register_driver
Input:
  - worker_id
  - driver
Output: None

Function: unregister_driver
Input:
  - worker_id
Output: None

Function: detect_page_state
Input:
  - worker_id
Output: str

Function: fill_card
Input:
  - card_info
  - worker_id
Output: None

Function: fill_billing
Input:
  - billing_profile
  - worker_id
Output: None

Function: clear_card_fields
Input:
  - worker_id
Output: None