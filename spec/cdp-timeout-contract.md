# CDP Timeout Contract

spec-version: 1.0

## Overview

Defines the timeout and error-handling rules for CDP (Chrome DevTools Protocol)
interactions. These contracts apply once CDP Business Logic is implemented
(resolving GAP-CDP-01 from audit-lock.md).

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

## Error Types

| Exception | When Raised |
|-----------|-------------|
| `SelectorTimeoutError` | CDP selector does not appear within timeout |
| `PageStateError` | `detect_page_state()` cannot determine a known FSM state |
| `SessionFlaggedError` | Watchdog timeout — no network response within deadline |
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
