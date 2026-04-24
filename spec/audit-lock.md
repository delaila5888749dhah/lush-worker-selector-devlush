# AUDIT LOCK — Core Engine v1.0

**Audit Date:** 2026-04-07
**Status:** PASSED — All 3 bugs fixed. Core Engine formally verified.
**Scope:** Core Engine (static framework). Excludes `modules/cdp` (Business Logic stub).

---

## PROVEN INVARIANTS

### INV-FSM-01 — ALLOWED_STATES Synchronization
```
ALLOWED_STATES (modules/fsm/main.py:18) == set(_FSM_STATES) (integration/orchestrator.py:38)
Value: {"ui_lock", "success", "vbv_3ds", "declined", "vbv_cancelled"}
```
**Rule:** Adding or removing an FSM state requires updating BOTH files simultaneously AND incrementing the spec version. A regression test (`test_fsm_allowed_states_sync`) enforces this at CI level.

---

### INV-DELAY-01 — Hard Timing Constraints (Blueprint §8.6)
```
MAX_TYPING_DELAY     = 1.8s   (modules/delay/persona.py:8)
MAX_HESITATION_DELAY = 5.0s   (modules/delay/engine.py:18)
MAX_STEP_DELAY       = 7.0s   (modules/delay/engine.py:19)
WATCHDOG_HEADROOM    = ≥3.0s  (7.0 - max behavioral delay = 3.0s minimum)
```
**Rule:** These values MUST NOT be increased without re-evaluating the watchdog timeout (currently 30s in orchestrator). The `_accumulate()` method in DelayEngine enforces the step ceiling at runtime.

---

### INV-DELAY-02 — CRITICAL_SECTION Zero Delay
```
BehaviorStateMachine states {VBV, POST_ACTION} → is_safe_for_delay() = False → delay = 0.0
_in_critical_section = True                    → is_safe_for_delay() = False → delay = 0.0
BehaviorStateMachine states {VBV, POST_ACTION} → is_critical_context() = True
_in_critical_section = True                    → is_critical_context() = True
```
**Rule:** No behavioral delay may ever be injected when the worker is in VBV, POST_ACTION, or flagged as CRITICAL_SECTION. This is enforced by `DelayEngine.is_delay_permitted()`. `is_critical_context()` now reflects both FSM critical states and the `_in_critical_section` flag. Callers that need the authoritative delay-permission decision should use `is_safe_for_delay()` (via the engine) instead.

---

### INV-DELAY-03 — Wrapper try/finally Cleanup (Fixed: BUG-001)
```
modules/delay/wrapper.py — _wrapped() always calls engine.reset_step_accumulator()
and sm.reset() in a finally block, even when task_fn() raises an exception.

Both injection points are protected:
  - Injection point 1 (typing): inject_step_delay() is inside the try block so
    cleanup runs on exception or interruption from the delay call itself.
  - Injection point 2 (thinking): wrapped in its own try/finally so accumulator
    and SM are reset even if the post-success thinking delay raises.
```
**Rule:** The `BehaviorStateMachine` for a worker must always return to `IDLE` after each cycle invocation. The `try/finally` pattern in `wrap()` is the enforcing mechanism for both injection points.

---

### INV-DELAY-04 — Temporal Modifier Bounded Output
```
modules/delay/temporal.py — apply_temporal_modifier():
  base_delay <= 0          → returns 0.0 immediately (no-op guard)
  any action_type, NIGHT   → return value ∈ [0.0, MAX_*_DELAY]
  any action_type, DAY     → return value ∈ [0.0, MAX_*_DELAY]

modules/delay/temporal.py — apply_micro_variation():
  result = max(0.0, base_delay * uniform(0.90, 1.10))
  → result is always non-negative
```
**Rule:** Temporal modifier output must be non-negative and bounded by the relevant MAX constant for the action type. `apply_temporal_modifier` guards against non-positive inputs and clamps all outputs to `[0.0, MAX]`. `apply_micro_variation` clamps its result to 0.0 minimum to prevent any negative value propagating to the accumulator.

---
```
modules/watchdog/main.py — _watchdog_registry: dict[worker_id → _WatchdogSession]
```
**Rule:** Watchdog state is keyed by `worker_id` (plain string). This ensures:
1. No cross-worker contamination when 10+ workers run concurrently.
2. `notify_total(worker_id, value)` is safe to call from ANY thread, including the browser's CDP event thread — no `threading.local()` blindspot.
3. `enable_network_monitor(worker_id)` creates a fresh session, completely isolated from other workers.

---

### INV-RUNTIME-01 — Worker State Transition Table
```
IDLE → IN_CYCLE → {CRITICAL_SECTION | SAFE_POINT} → IN_CYCLE → IDLE
```
**Rule:** All transitions go through `_transition_worker_state_locked()`. Direct assignment to `_worker_states[wid]` is forbidden. The `finally` block in `_worker_fn()` only removes the worker entry if `_workers.get(worker_id) is threading.current_thread()` — preventing stale threads from corrupting the registry.

---

### INV-RUNTIME-02 — reset() is Test-Only
```
integration/runtime.py — reset() sets _behavior_delay_enabled = False
```
**Rule:** `reset()` MUST NOT be called from any production code path. It is exclusively for test teardown. Calling it in production would silently disable all behavioral delay injection.

---

### INV-SCALE-01 — Progressive Scaling Steps (dynamic cap)
`SCALE_STEPS` is derived at runtime from `MAX_WORKER_COUNT` (default 10, range 1–50)
in `modules/rollout/main.py`. Contract:
- strictly ascending
- `SCALE_STEPS[0] == 1`
- `SCALE_STEPS[-1] == MAX_WORKER_COUNT`
- `all(s <= MAX_WORKER_COUNT for s in SCALE_STEPS)`

**Rule:** Changing the derivation rule or cap range requires a load test
demonstrating stability at the new cap before merging.

---

### INV-ORCHESTRATOR-02 — handle_outcome() state=None Observability
```
integration/orchestrator.py — handle_outcome(state=None, ...) logs a WARNING
before returning "retry". Silent return was replaced with explicit logging
to make upstream FSM anomalies (page-state detection failure, or
run_payment_step returning without a state transition) observable.
```
**Rule:** `handle_outcome()` MUST log a warning when called with `state=None`.
The return value "retry" is unchanged. No exception is raised.

---

### INV-ORCHESTRATOR-03 — Dual Notify Race Safety (first-notify-wins)
```
integration/orchestrator.py — _notified_workers_this_cycle: set[str]
Protected by _network_listener_lock.
Cleared per cycle in run_payment_step before watchdog.enable_network_monitor().
```
**Rule:** Both the `Network.responseReceived` callback path and the DOM fallback
path call `_notify_total_from_dom()`. Only the first caller to acquire
`_network_listener_lock` for a given worker_id in a given cycle may call
`watchdog.notify_total()`. Subsequent calls are silently skipped. This prevents
value-overwrite races on `session.total_value` in the watchdog.

---

### INV-ORCHESTRATOR-04 — Submitted-State Crash Safety
```
integration/orchestrator.py — _submitted_task_ids persisted before wait_for_total.
On reload, a WARNING is emitted if submitted tasks are found (crash-recovery path).
```
**Rule:** Once a task_id is recorded as "submitted" (payment sent, result
unconfirmed), it MUST block re-execution on next load to prevent double-charge.
On `_load_idempotency_store()`, if `_submitted_task_ids` is non-empty after
loading, a WARNING log MUST be emitted. The timeout path that fires after
`mark_submitted()` MUST log a distinct "AFTER payment submission" message to
distinguish it from pre-submission failures.

---

### INV-CDP-EXEC-01 — Executor Saturation Observability
```
integration/orchestrator.py — _cdp_orphaned_threads: int
Incremented on each caller-side timeout (thread may still occupy executor slot).
Reported via get_cdp_metrics()['orphaned_cdp_threads'].
```
**Rule:** Every caller-side CDP timeout MUST increment `_cdp_orphaned_threads`.
`get_cdp_metrics()` MUST include `orphaned_cdp_threads` in its return dict.
When `orphaned_cdp_threads` approaches `CDP_EXECUTOR_MAX_WORKERS`, executor
saturation risk is high — new submissions will queue rather than start immediately.

---

### INV-CDP-SHUTDOWN-01 — Executor Shutdown Bounded and Observable
```
integration/orchestrator.py — _shutdown_cdp_executor() uses wait=False.
Logs active_cdp_requests and _cdp_orphaned_threads before shutdown.
```
**Rule:** Shutdown MUST NOT block indefinitely on hung CDP calls (`wait=False`).
Active/orphaned thread counts MUST be logged before `shutdown()` is called so
operational state is visible at process exit.

---

### INV-REDIS-01 — Redis Idempotency Store Failure Semantics
```
integration/orchestrator.py — _RedisIdempotencyStore
is_duplicate() failure → fail-safe: return True (treat as duplicate)
mark_submitted() failure → re-raise (critical payment checkpoint)
mark_completed() failure → log WARNING, do not re-raise
```
**Rule:** Redis failures in `is_duplicate()` MUST be treated as duplicates to
prevent double-charge (fail-safe). Failures in `mark_submitted()` MUST propagate
(the submitted checkpoint must be reliable). Failures in `mark_completed()` MUST
be logged as WARNING but NOT re-raised (task is already submitted; completion
recording failure is non-critical).

---
```
integration/orchestrator.py — cdp.clear_card_fields() in vbv_3ds branch is wrapped
in try/except. A CDP failure during VBV handling does NOT prevent "await_3ds" from
being returned to the caller.
```
**Rule:** `handle_outcome()` must remain a pure decision function. Side-effect failures (CDP calls) must be logged and swallowed, not propagated as exceptions from this function.

---

### INV-WATCHDOG-02 — notify_total() is the CDP Integration Point
```
modules/watchdog/main.py — notify_total(worker_id, value) is the ONLY public entry
point for CDP to signal that a checkout total has been received.
```
**Rule:** When implementing `modules/cdp/main.py` Business Logic, the CDP Network.responseReceived callback MUST call `watchdog.notify_total(worker_id, value)`. The `worker_id` must be passed into the CDP layer from the orchestrator. CDP must NOT call any internal watchdog methods directly.

---

### INV-CDP-01 — _sanitize_error() PII Redaction (delegated)
```
modules/cdp/main.py — _sanitize_error is re-exported from
  modules.common.sanitize.sanitize_error (INV-PII-UNIFIED-01).
  No local regex patterns remain in this module.
modules/cdp/driver.py — _sanitize_error is re-exported from
  modules.common.sanitize.sanitize_error (INV-PII-UNIFIED-01).
```
**Rule:** Any log output or re-raised exception from the CDP layer MUST have PII
stripped by `modules.common.sanitize.sanitize_error()` first. The canonical
sanitiser redacts 13/15/16/19-digit PANs (bare / spaced / dashed), CVV keyword
patterns, bare CVVs adjacent to a redacted PAN, email addresses, and Redis URL
credentials. CDP modules MUST NOT define their own regex patterns or local
`_sanitize_error` functions.

---

### INV-PII-UNIFIED-01 — Single PII Sanitiser Source of Truth
```
modules/common/sanitize.py — sanitize_error() is the canonical PII redaction
  function. Redacts in order: Redis URL credentials, PANs (13/15/16/19 digits
  in bare/spaced/dashed forms), bare CVV adjacent to redacted PAN, keyword
  CVV (cvv=123), email addresses.
Delegating modules:
  - modules/cdp/main.py          → from modules.common.sanitize import sanitize_error as _sanitize_error
  - modules/cdp/driver.py        → from modules.common.sanitize import sanitize_error as _sanitize_error
  - integration/orchestrator.py  → _canonical_sanitize_error wrapper accepting Exception
  - integration/runtime.py       → _sanitize_error adapter accepting Exception
```
**Rule:** No module in `modules/*` or `integration/*` may define a local
function named `_sanitize_error` or `sanitize_error` that contains regex
patterns. All PII redaction MUST go through `modules.common.sanitize`. Thin
adapters that accept `Exception` and delegate to `sanitize_error(str(exc))`
are permitted for backward-compatible call sites.

---

### INV-CDP-02 — PID Registry Thread Safety
```
modules/cdp/main.py — _pid_registry: dict[worker_id → int]
Protected by _registry_lock (shared lock with _driver_registry).
force_kill() pops PID under lock BEFORE calling os.kill().
```
**Rule:** `_register_pid()` and `force_kill()` MUST hold `_registry_lock` during all registry reads/writes. `force_kill()` pops the PID entry before sending the signal — ensuring the registry is always cleaned up even if `os.kill()` raises `ProcessLookupError` or `PermissionError`.

---

## KNOWN GAPS (deferred to Business Logic phase)

| ID | Description | Resolution |
|---|---|---|
| GAP-CDP-01 | `modules/cdp/main.py` — PID tracking, `_sanitize_error()`, driver delegation | ✅ Resolved — PR #238; INV-CDP-01 rewritten and INV-PII-UNIFIED-01 added in PR #232 |
| GAP-FSM-02 | FSM singleton shares state across all workers | Acceptable: `orchestrator._lock` serializes `initialize_cycle()` calls; each cycle resets the FSM before use |
| GAP-BILLING-01 | `_find_matching_index()` cursor snap race (theoretical) | Acceptable: entire `select_profile()` holds `_lock` during actual selection |

---

## BUGS FIXED IN THIS PR

| ID | File | Description |
|---|---|---|
| BUG-001 | `modules/delay/wrapper.py` | Missing `try/finally` — state stuck on exception |
| BUG-002 | `modules/watchdog/main.py` | Singleton race + cross-thread blindspot → replaced with `worker_id` registry |
| BUG-003 | `integration/orchestrator.py` | `cdp.clear_card_fields()` unguarded in `handle_outcome()` |
| PH3A-01 | `integration/orchestrator.py` | Phase 3A Task 2 — `run_payment_step` now blocks on `watchdog.wait_for_total(timeout=10)` in **Phase A** (pre-fill), raising `SessionFlaggedError` before any card field is typed.  A second best-effort wait in **Phase C** (post-submit) no longer raises — it only refines the total. Matches INV-PAYMENT-01 and `spec/contracts/section5_payment.yaml`. |
| PH3A-02 | `modules/cdp/driver.py` | Phase 3A Task 1 — `select_guest_checkout`, `fill_payment_and_billing`, and legacy `fill_billing` route all §5 text fields through `_realistic_type_field` (CDP `Input.dispatchKeyEvent`).  `_cdp_type_field` retained as deprecation shim; raises `RuntimeError` when `ENFORCE_CDP_TYPING_STRICT=1`, emits `DeprecationWarning` otherwise. |
| PH3A-03 | `modules/cdp/driver.py` | Phase 3A Task 3 — `bounding_box_click` now raises `CDPClickError` (new, in `modules/common/exceptions.py`) on **all four** failure branches (rect fetch, zero-size rect, missing persona RNG, CDP dispatch failure) when `self._strict=True` (default).  Non-strict mode retains the `.click()` fallback. |

---

## SECURITY AUDIT FIXES (2026-04-08)

| ID | Severity | File | Description |
|---|---|---|---|
| BL-001 | BLOCKER | `integration/orchestrator.py` | In-memory idempotency store → replaced with file-based persistent JSON store (`IDEMPOTENCY_STORE_PATH`). Atomic writes via temp-file + rename. Timestamps stored as wall-clock for cross-restart portability. Payment-submitted checkpoint persisted after `fill_card` and before `wait_for_total`. |
| HI-001 | HIGH | `modules/billing/main.py` | `BILLING_POOL_DIR` path traversal — resolved path validated against allowed prefixes (project root, `/data`, `/tmp`); null bytes rejected; falls back to default on violation. |
| HI-002 | HIGH | `integration/orchestrator.py` | Dead `_lock = threading.Lock()` removed; error context logging added to `run_payment_step` and `run_cycle` exception handlers with `worker_id` and `task_id`. |
| ME-001 | MEDIUM | `integration/orchestrator.py` | Module-level `_logger.warning()` side effect removed; warning moved to `initialize_cycle()` with `_init_warning_emitted` once-flag. |
| ME-002 | MEDIUM | `integration/runtime.py` | Timezone-less log timestamps replaced with `datetime.now(timezone.utc).isoformat(timespec="seconds")`. |
| ME-003 | MEDIUM | `modules/billing/main.py` | `_find_matching_index()` missing lock contract docstring added — documents that caller must hold `_lock`. |
| ME-004 | MEDIUM | `integration/orchestrator.py` | `_FSM_STATES` duplicate eliminated — now imported directly from `modules.fsm.main.ALLOWED_STATES` to prevent drift (see INV-FSM-01). |

---

## CHANGE POLICY (Post-Audit)

Any PR that modifies the following files MUST include an update to this document:

- `modules/fsm/main.py` (ALLOWED_STATES)
- `modules/delay/engine.py` (hard constraints)
- `modules/delay/wrapper.py` (SAFE ZONE logic)
- `modules/delay/temporal.py` (temporal modifier bounds)
- `modules/delay/state.py` (FSM states, critical-context semantics)
- `modules/watchdog/main.py` (registry architecture)
- `integration/orchestrator.py` (wiring + outcome logic)
- `integration/runtime.py` (worker state transitions)
- `modules/rollout/main.py` (SCALE_STEPS)
- `modules/cdp/main.py` (PID registry, _sanitize_error, driver registry)
- `modules/cdp/fingerprint.py` (BitBrowser pool client — FEATURE-POOL-01)

---

## FEATURE-POOL-01 — BitBrowser Profile Pool (Blueprint §2.1)

```
modules/cdp/fingerprint.py — BitBrowserPoolClient
  Round-robin cursor + BUSY set, protected by threading.Lock
  acquire_profile() → str (sequential, thread-safe)
  release_profile(profile_id) → None (best-effort close, always clears BUSY)
  randomize_fingerprint(profile_id) → POST /browser/update/partial
  launch_profile(profile_id) → dict (POST /browser/open)
  _evict_profile(profile_id) → pool removal on 404
  get_bitbrowser_client() factory branches on BITBROWSER_POOL_MODE:
    "1"/"true"/"yes" → BitBrowserPoolClient (requires BITBROWSER_PROFILE_IDS)
    otherwise        → legacy BitBrowserClient (unchanged behaviour)
  get_bitbrowser_client() dedupes BITBROWSER_PROFILE_IDS (warns) and warns
    when len(pool) < 2 × WORKER_COUNT.

modules/cdp/fingerprint.py — BitBrowserSession (INV-POOL-INT, Phase 2)
  __init__: detects pool capability via isinstance(client, BitBrowserPoolClient)
  __enter__ pool-mode flow:
    acquire_profile → randomize_fingerprint → launch_profile(/browser/open)
    HTTPError 404 on /browser/open → _evict_profile + release_profile
    (create_profile / delete_profile are NEVER called)
  __enter__ legacy-mode flow: create_profile → launch_profile (unchanged)
  release_profile pool-mode: delegates to client.release_profile (close only,
    POOL-NO-DELETE — /browser/delete is NEVER called)
  release_profile legacy-mode: close_profile + delete_profile (unchanged)
```

**Rule:** Any modification to `BitBrowserPoolClient` acquire/release/eviction
semantics, to the round-robin cursor invariant, to `BitBrowserSession`
pool/legacy branching, or to the `get_bitbrowser_client()` factory branching
MUST update this entry. Backward compatibility for `BITBROWSER_POOL_MODE=0`
(legacy create/delete flow) is MANDATORY — both
`tests/test_bitbrowser_pool.py#test_backward_compat_legacy_mode_unaffected`
and `tests/test_bitbrowser_pool_session.py#test_legacy_session_still_creates_and_deletes`
enforce this at CI level.

---

### INV-BITBROWSER-RETRY-01 — _post() Retry Semantics
```
modules/cdp/fingerprint.py — BitBrowserClient._post() retries transient failures:
  Retryable:    urllib.error.URLError, OSError, HTTPError with 500 <= code < 600
  Fail-fast:    HTTPError with 400 <= code < 500, RuntimeError, JSONDecodeError
  Config (env): BITBROWSER_RETRY_ATTEMPTS        (default 3, min 1)
                BITBROWSER_RETRY_WAIT_INITIAL_S  (default 0.5)
                BITBROWSER_RETRY_WAIT_MAX_S      (default 8.0)
  Backoff:      doubles each attempt, capped at WAIT_MAX_S
```
**Rule:** 4xx responses MUST fail fast (caller bug — bad payload or auth).
5xx and network errors MUST be retried up to `BITBROWSER_RETRY_ATTEMPTS` total
attempts. The combined worst-case wall-clock (attempts × WAIT_MAX_S) MUST
remain below the watchdog timeout (_WATCHDOG_TIMEOUT = 30s and
_CDP_CALL_TIMEOUT). If defaults are changed, re-verify this budget.

---

### INV-BITBROWSER-ENDPOINT-01 — Endpoint Scheme/Host Safety
```
modules/cdp/fingerprint.py — _validate_endpoint_scheme() runs in
  BitBrowserClient.__init__. HTTP on any non-loopback host (not in
  {"127.0.0.1", "localhost", "::1"}) emits a WARNING because the API key
  would transit clear-text. BITBROWSER_ENDPOINT_STRICT=1 escalates the
  warning to ValueError.
```
**Rule:** Plain HTTP is only safe on loopback. Operators deploying the worker
against a remote BitBrowser instance MUST use HTTPS or explicitly accept
risk by leaving `BITBROWSER_ENDPOINT_STRICT` unset. CI MUST NOT bypass this
warning by setting loopback endpoints in non-loopback test environments.

---

### PHASE-1-BRINGUP — RT-CAP-50-VS-500, RT-STAGGER-FLAG (2026-04-24)
```
modules/rollout/main.py — _MAX_MAX_WORKER_COUNT raised 50 → 500 (option a).
  configure_max_workers() validates count ∈ [1, 500].
  set_scale_steps() cap raised to 500.
integration/runtime.py:
  _validate_startup_config() accepts MAX_WORKER_COUNT / WORKER_COUNT ∈ [1, 500]
    and emits a WARNING when MAX_WORKER_COUNT > 100.
  _stagger_enabled is a new module-level flag (default True) gating the
    12–25s launch gap in _apply_scale() — independent of the behavior-delay
    flag.  set_stagger_enabled(bool) is the public setter.  reset() flips
    both _behavior_delay_enabled and _stagger_enabled to False for test
    hygiene (mirrors pre-existing semantics).
```
**Rule:** Blueprint §1 permits `MAX_WORKER_COUNT` up to 500; the runtime cap
MUST equal `modules.rollout.main._MAX_MAX_WORKER_COUNT` at all three
enforcement points (`configure_max_workers`, `set_scale_steps`,
`integration.runtime._validate_startup_config`). Regression test
`tests/test_phase1_runtime_bringup.py::
TestMaxWorkerCountCapConsistent::
test_max_worker_count_cap_consistent_between_configure_and_startup` enforces
this at CI level. Stagger MUST NOT be re-coupled to `_behavior_delay_enabled`
in `_apply_scale()`; regression test
`test_stagger_enabled_independent_of_behavior_delay` guards the decoupling.
