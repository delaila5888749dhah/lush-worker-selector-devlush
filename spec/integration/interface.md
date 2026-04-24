# Interface Contract — Integration (Watchdog, Billing, CDP, Observability)

spec-version: 8.0

> **v8.0 Breaking Changes:**
> - Added ClickDispatchError exception type to modules.common.exceptions (raised by GivexDriver.bounding_box_click in strict mode — P3-D3)
> - Added cdp.run_preflight_up_to_guest_checkout (splits out the card/billing fill so wait_for_total can gate on payment-page network signal — P3-F4-ORDER)
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
> **v5.3 Breaking Changes:**
> - billing.select_profile now accepts optional worker_id parameter for per-worker state isolation (Blueprint §5 billing_pool rule)
>
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

---

## Module: modules.observability.metrics_exporter

- **Entry point:** `export_metrics(metrics: dict) -> None`
- **Called from:** `integration.runtime._runtime_loop` after `monitor.get_metrics()`
- **Default backend:** Structured JSON log at DEBUG level via Python logging
- **Envelope:** `{**metrics, "event": "metrics_export", "ts": float}` — envelope fields override metrics keys on collision
- **Custom backends:** Register via `register_exporter(fn)` / `unregister_exporter(fn)`
- **Fail-safe:** Exceptions from individual exporters are caught and logged as WARNING
- **Thread-safe:** All shared state guarded by `threading.Lock`
- **Reset:** `reset()` clears all exporter state — called from `integration.runtime.reset()`
- **Backward compatibility:** Additive only — no existing code changed

---

## Module: modules.observability.healthcheck

- **Entry points:**
  - `get_health(status_fn=None) -> dict`
  - `start_server(host="127.0.0.1", port=8080, status_fn=None) -> bool`
  - `stop_server(timeout=5.0) -> bool`
  - `is_running() -> bool`
  - `reset() -> None`
- **Dependency injection:** `status_fn` is a `Callable() -> dict` injected by the caller (typically `integration.runtime.get_deployment_status`) to avoid `modules → integration` import direction inversion. When `status_fn` is omitted (`None`), `get_health()` returns `{"status": "unknown", "errors": ["status_fn not configured"]}`
- **Health response schema:**
  ```json
  {
    "status": "healthy | degraded | unknown",
    "running": true,
    "state": "RUNNING",
    "worker_count": 2,
    "consecutive_rollbacks": 0,
    "errors": []
  }
  ```
- **Degraded conditions:** `running == False`, `consecutive_rollbacks > 0`, `error_rate > 5%`
- **HTTP endpoint:** `GET /health` → 200 JSON; any other path → 404
- **Server:** `ThreadingHTTPServer` in a daemon thread; `_stopping` flag prevents start/stop races
- **Thread-safe:** All server state guarded by `threading.Lock`
- **Backward compatibility:** Additive only — no existing interface changes

---

## Module: modules.observability.log_sink

- **Entry point:** `emit(event: dict) -> None`
- **Called from:** `integration.runtime._log_event` alongside existing pipe-delimited format
- **Log schema:** `{"ts": float, "source": str, "level": str, "event": str, "data": dict}`
- **Default backend:** Structured JSON log at DEBUG level via Python logging
- **Custom sinks:** Register via `register_sink(fn)` / `unregister_sink(fn)`
- **Fail-safe:** `emit()` wraps all logic in try/except — never propagates into `_log_event`
- **Thread-safe:** All shared state guarded by `threading.Lock`
- **Reset:** `reset()` clears all sink state — called from `integration.runtime.reset()`
- **Backward compatibility:** Additive — pipe-delimited format unchanged, JSON is additional

---

## Module: modules.observability.alerting

- **Entry points:**
  - `evaluate_alerts(metrics: dict) -> list[str]`
  - `send_alert(message: str) -> None`
  - `register_alert_handler(fn) -> None`
  - `unregister_alert_handler(fn) -> bool`
  - `set_log_alert_enabled(enabled: bool) -> None`
  - `get_status() -> dict`
  - `reset() -> None`
- **Called from:** `integration.runtime._runtime_loop` after `metrics_exporter.export_metrics(metrics)`, before `behavior.evaluate()`
- **Thresholds:**
  - `error_rate > 0.05` (5%) → alert
  - `restarts_last_hour > 3` → alert
  - `success_rate < baseline_success_rate - 0.10` (only when `baseline_success_rate` is not `None`) → alert
- **Default backend:** `_logger.warning("ALERT: %s", message)` via Python logging
- **Custom handlers:** Register via `register_alert_handler(fn)` / `unregister_alert_handler(fn) -> bool`
- **Fail-safe:** `evaluate_alerts()` and `send_alert()` both wrap all logic in `try/except` — never propagate exceptions into `_runtime_loop`
- **Thread-safe:** All shared state (`_alert_handlers`, `_alert_count`, `_log_alert_enabled`) guarded by `threading.Lock`; handler list is snapshot-copied before iteration
- **Reset:** `reset()` clears all handler and counter state — called from `integration.runtime.reset()`
- **Backward compatibility:** Additive only — existing rollback logic in `_runtime_loop` unchanged

---

## Changelog

### v7.1 (2026-04-23)
- Added `monitor.record_ui_lock_retry()`, `monitor.record_ui_lock_recovered()`, and `monitor.record_ui_lock_exhausted()` for UI-lock recovery observability.

### v5.3 (2026-04-19)
- billing.select_profile(zip_code, worker_id=None) — added optional worker_id for per-worker shuffled state (P4 Blueprint compliance).

### v5.2 (2026-04-12)
- Added Ext-2 Alerting Rules contract (`modules.observability.alerting`).
- Added Ext-4 Structured Log Aggregation contract (`modules.observability.log_sink`).

### v5.1 (2026-04-12)
- Added Ext-1 Metrics Export contract (`modules.observability.metrics_exporter`).
- Added Ext-3 Health Check Endpoint contract (`modules.observability.healthcheck`).
