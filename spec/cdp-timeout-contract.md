# CDP Timeout Contract

spec-version: 1.1

## Overview

Defines the timeout and error-handling rules for CDP (Chrome DevTools Protocol)
interactions. These contracts apply to the production orchestrator implementation
in `integration/orchestrator.py`.

## Driver Registry

Each worker MUST register its browser driver before using CDP functions:

```
cdp.register_driver(worker_id, driver)   # Before any CDP operation
cdp.unregister_driver(worker_id)         # After cycle completes / on cleanup
```

The driver registry is thread-safe (`threading.Lock`). Unregistering a
non-existent worker_id is a no-op.

## Timeout Rules

### Network Response Timeout (Blueprint §5)

- After filling card data, the orchestrator calls `watchdog.wait_for_total(worker_id, timeout=30)`.
- If no `Network.responseReceived` event arrives within 30 seconds,
  `SessionFlaggedError` is raised.
- The CDP layer signals the watchdog via `watchdog.notify_total(worker_id, value)`
  from the `Network.responseReceived` callback (see INV-WATCHDOG-02).

### Page Load Timeout

- CDP page navigation MUST complete within a configurable timeout.
- On timeout, raise `SelectorTimeoutError`.
- The orchestrator catches this and decides whether to retry or abort.

### Element Interaction Timeout

- CDP element interaction (fill, click, wait) has a per-operation timeout.
- On timeout, raise `SelectorTimeoutError`.

## CDP Call Executor Timeout (INV-CDP-EXEC-01)

```
_cdp_call_with_timeout(fn, *args, timeout=_CDP_CALL_TIMEOUT)
```

- CDP calls are submitted to a shared `ThreadPoolExecutor` (`cdp-timeout` threads).
- `future.result(timeout=timeout)` provides the caller-side timeout.
- On timeout: `future.cancel()` is attempted (best-effort; no-op if the task is
  already running). The caller is unblocked immediately via `SessionFlaggedError`.
- **Orphaned threads**: after a caller-side timeout, the underlying thread continues
  running until the CDP call completes or the browser process exits. This is an
  inherent CPython limitation. Monitor `get_cdp_metrics()['orphaned_cdp_threads']`
  to detect executor saturation risk.
- **Saturation**: when `orphaned_cdp_threads` approaches `CDP_EXECUTOR_MAX_WORKERS`,
  new submissions queue instead of starting immediately.
- **Executor unavailable**: if `shutdown()` has been called, `submit()` raises
  `RuntimeError` → wrapped as `SessionFlaggedError`.

### Executor Health Metrics

`get_cdp_metrics()` returns:

| Key | Description |
|-----|-------------|
| `total_timeouts` | Cumulative caller-side timeout count |
| `active_cdp_requests` | Orchestration-level in-flight count (NOT executor thread count) |
| `orphaned_cdp_threads` | Best-estimate timed-out threads still occupying executor slots |

`active_cdp_requests == 0` does NOT mean all executor threads are idle. Use
`orphaned_cdp_threads` to assess saturation risk.

## Error Types

| Exception | When Raised |
|-----------|-------------|
| `SelectorTimeoutError` | CDP selector does not appear within timeout |
| `PageStateError` | `detect_page_state()` cannot determine a known FSM state |
| `SessionFlaggedError` | Watchdog timeout — no network response within deadline |
| `SessionFlaggedError` | CDP executor timeout or executor unavailable |
| `NotImplementedError` | CDP function is still a stub (GAP-CDP-01) |

## Integration with Watchdog (INV-WATCHDOG-02)

```
1. orchestrator calls cdp.register_driver(worker_id, driver)
2. orchestrator calls watchdog.enable_network_monitor(worker_id)
3. CDP fills card/billing data → triggers network request
4. CDP Network.responseReceived callback → watchdog.notify_total(worker_id, value)
5. orchestrator calls watchdog.wait_for_total(worker_id, timeout=30)
6. orchestrator calls cdp.unregister_driver(worker_id) on cleanup
```

CDP MUST NOT call any internal watchdog methods directly.
The ONLY allowed watchdog call from CDP is `notify_total(worker_id, value)`.

## Dual-Notify Race Safety (INV-CDP-NOTIFY-01)

Both the `Network.responseReceived` callback path and the pre-wait DOM fallback
(`_notify_total_from_dom`) may attempt to call `watchdog.notify_total()` for the
same worker in the same cycle. Orchestrator enforces **first-notify-wins** via the
`_notified_workers_this_cycle` set (protected by `_network_listener_lock`):

- The first caller that acquires `_network_listener_lock` and parses a valid total
  calls `watchdog.notify_total()` and records the worker_id in the set.
- Subsequent calls for the same worker_id in the same cycle are silently skipped.
- The guard is cleared at the start of each new cycle (`run_payment_step` discards
  the worker_id before `watchdog.enable_network_monitor()`).

This prevents value-overwrite races where a late/duplicate notification would
overwrite the first correct total before `wait_for_total()` reads it.

## Shutdown Safety (INV-CDP-SHUTDOWN-01)

`_shutdown_cdp_executor()` uses `wait=False` to avoid blocking indefinitely on
hung CDP calls. It logs the active/orphaned thread counts before issuing shutdown
so that operational state is visible at process exit. Registered via `atexit`.

