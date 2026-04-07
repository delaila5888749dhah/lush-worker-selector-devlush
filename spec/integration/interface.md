# Interface Contract — Integration (Watchdog, Billing, CDP)

spec-version: 2.0

> **v2.0 Breaking Changes:**
> - Exception types (SessionFlaggedError) moved to modules.common.exceptions
> - Data types moved to modules.common.types
> - spec/ is no longer a runtime dependency

## Module: watchdog

Function: enable_network_monitor
Input:
  - worker_id (str)
Output: None

Function: wait_for_total
Input:
  - worker_id (str)
  - timeout
Output: total value
Error:
  - Raise RuntimeError if enable_network_monitor() was not called for worker_id
  - Raise SessionFlaggedError if timeout expires

Function: notify_total
Input:
  - worker_id (str)
  - value
Output: None
Notes:
  - Safe to call from any thread (browser CDP event thread, worker thread, etc.)
  - No-op if no session exists for worker_id

## Module: billing

Function: select_profile
Input:
  - zip_code
Output: BillingProfile

## Module: cdp

Function: detect_page_state
Input: None
Output: str

Function: fill_card
Input:
  - card_info
Output: None

Function: fill_billing
Input:
  - billing_profile
Output: None

Function: clear_card_fields
Input: None
Output: None
