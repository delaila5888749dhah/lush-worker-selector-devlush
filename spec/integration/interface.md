# Interface Contract — Integration (Watchdog, Billing, CDP, Observability)

spec-version: 5.1

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

---

## Module: observability.metrics_exporter

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

## Module: observability.healthcheck

- **Entry points:**
  - `get_health(status_fn=None) -> dict`
  - `start_server(host=DEFAULT_HOST, port=DEFAULT_PORT, status_fn=None) -> bool`
  - `stop_server(timeout=5.0) -> bool`
  - `is_running() -> bool`
  - `reset() -> None`
- **Dependency injection:** `status_fn` is a `Callable() -> dict` injected by the caller (typically `integration.runtime.get_deployment_status`) to avoid `modules → integration` import direction inversion
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

## Module: observability.log_sink

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

## Changelog

### v5.1 (2026-04-12)
- Added Ext-1 Metrics Export contract (`modules.observability.metrics_exporter`).
- Added Ext-3 Health Check Endpoint contract (`modules.observability.healthcheck`).
- Added Ext-4 Structured Log Aggregation contract (`modules.observability.log_sink`).
