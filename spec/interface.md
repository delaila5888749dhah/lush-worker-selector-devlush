# Interface Contract (Aggregated)

spec-version: 7.2

> **Contract Segmentation (v2.0):** Interface contracts have been split into
> two separate groups. This file aggregates both groups to maintain backward
> compatibility with the CI pipeline.
>
> - **Core (FSM):** [spec/core/interface.md](core/interface.md)
> - **Integration (Watchdog, Billing, CDP):** [spec/integration/interface.md](integration/interface.md)
>
> **v7.2 Additive Changes (Blueprint §2.1):**
> - Declared `BitBrowserPoolClient` in modules/cdp/fingerprint.py for pool-mode
>   profile management (round-robin sequential, thread-safe). Activated via
>   `BITBROWSER_POOL_MODE=1`. Legacy `BitBrowserClient` behaviour unchanged.
>
> **v7.1 Additive Changes:**
> - Added monitor UI-lock metric APIs: `record_ui_lock_retry()`, `record_ui_lock_recovered()`, `record_ui_lock_exhausted()`
>
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

### Class: BitBrowserPoolClient (Blueprint §2.1)

Location: `modules/cdp/fingerprint.py`.
Activated when `BITBROWSER_POOL_MODE=1` and `BITBROWSER_PROFILE_IDS` is a
non-empty CSV. Round-robin sequential, thread-safe profile lease manager.

Notes:
  - `acquire_profile()` → `str`: picks the next AVAILABLE profile id from the
    pool starting at the shared cursor; marks it BUSY. Raises `RuntimeError`
    if no profile becomes available within `acquire_timeout_s` (default 60s).
  - `release_profile(profile_id)`: best-effort closes the browser window
    (POST `/browser/close`, no delete) and always clears the BUSY flag.
  - `randomize_fingerprint(profile_id)`: POST `/browser/update/partial` with
    `{"ids": [profile_id], "browserFingerPrint": {"batchRandom": True,
    "batchUpdateFingerPrint": True}}`. On HTTP 404 the profile is evicted
    from the pool and `RuntimeError` is raised.
  - `launch_profile(profile_id)` → `dict`: POST `/browser/open` → response
    dict containing `webdriver` URL for Selenium + CDP attach.
  - Constructor raises `ValueError` if `profile_ids` is empty.
  - Factory `get_bitbrowser_client()` returns `BitBrowserPoolClient` when
    pool mode is on, else the legacy `BitBrowserClient` (behaviour unchanged
    when `BITBROWSER_POOL_MODE=0`).

## Module: monitor

Function: record_ui_lock_retry
Input: None
Output: None
Notes:
  - Increments the UI-lock retry-attempt counter
  - Thread-safe via `threading.Lock`

Function: record_ui_lock_recovered
Input: None
Output: None
Notes:
  - Increments the UI-lock recovered counter after focus-shift recovery clears the lock
  - Thread-safe via `threading.Lock`

Function: record_ui_lock_exhausted
Input: None
Output: None
Notes:
  - Increments the UI-lock exhausted counter when retry budget is consumed and the lock persists
  - Thread-safe via `threading.Lock`
