# Interface Contract — Integration (Watchdog, Billing, CDP)

spec-version: 5.0

> **v5.0 Breaking Changes:**
> - CDP functions (detect_page_state, fill_card, fill_billing, clear_card_fields) now require worker_id parameter
> - Added reset_session(worker_id) public API to watchdog module
>
> **v4.0 Breaking Changes:**
> - Added SelectorTimeoutError and PageStateError exception types to modules.common.exceptions
> - WorkerTask is now frozen (immutable)
> - Added register_driver(worker_id, driver) and unregister_driver(worker_id) to CDP module
>
> **v3.0 Breaking Changes:**
> - enable_network_monitor and wait_for_total now require worker_id parameter
> - Added notify_total function
>
> **v2.0 Breaking Changes:**
> - Exception types (SessionFlaggedError) moved to modules.common.exceptions
> - Data types moved to modules.common.types
> - spec/ is no longer a runtime dependency

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
Output: BillingProfile

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

## Ext-1: Metrics Export

- **Module:** `modules.observability.metrics_exporter`
- **Entry point:** `export_metrics(metrics: dict) -> None`
- **Called from:** `integration.runtime._runtime_loop` after `monitor.get_metrics()`
- **Default backend:** Structured JSON log at DEBUG level
- **Custom backends:** Register via `register_exporter(fn)` / `unregister_exporter(fn)`
- **Fail-safe:** Exceptions in exporters are caught; loop is never disrupted
- **Thread-safe:** All shared state guarded by `threading.Lock()`
- **Backward compatibility:** Additive only — no existing interface changes
