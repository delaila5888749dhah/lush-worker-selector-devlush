# AUDIT LOCK — Core Engine v1.0

**Audit Date:** 2026-04-07
**Status:** PASSED — All 3 bugs fixed. Core Engine formally verified.
**Scope:** Core Engine (static framework). Excludes `modules/cdp` (Business Logic stub).

---

## PROVEN INVARIANTS

### INV-FSM-01 — ALLOWED_STATES Synchronization
```
ALLOWED_STATES (modules/fsm/main.py:6) == set(_FSM_STATES) (integration/orchestrator.py:17)
Value: {"ui_lock", "success", "vbv_3ds", "declined"}
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
**Rule:** Temporal modifier output must be non-negative and bounded by the relevant MAX constant for the action type. `apply_temporal_modifier` guards against non-positive inputs and clamps all outputs to [0.0, MAX]. `apply_micro_variation` clamps its result to 0.0 minimum to prevent any negative value propagating to the accumulator.

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

### INV-SCALE-01 — Progressive Scaling Steps
```
SCALE_STEPS = (1, 3, 5, 10)   (modules/rollout/main.py:11)
```
**Rule:** Adding a new scaling step requires a load test demonstrating the system remains stable at the new worker count before merging.

---

### INV-ORCHESTRATOR-01 — handle_outcome() CDP Error Isolation (Fixed: BUG-003)
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

### INV-CDP-01 — _sanitize_error() PII Redaction
```
modules/cdp/main.py — _sanitize_error(msg) redacts card numbers, CVV patterns,
and email addresses before any exception message is logged or re-raised.
Compiled regex patterns: _CARD_PATTERN (16-digit \b\d{16}\b), _CVV_PATTERN
(cvv\s*=\s*\d{3,4}, case-insensitive), _EMAIL_PATTERN (RFC-5321 subset).
```
**Rule:** Any log output or re-raised exception from the CDP layer MUST have PII stripped by `_sanitize_error()` first. Card numbers (16-digit), CVV patterns, and email addresses MUST NOT appear in log output.

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
| GAP-CDP-01 | `modules/cdp/main.py` — PID tracking, `_sanitize_error()`, driver delegation | ✅ Resolved — PR #238 |
| GAP-FSM-02 | FSM singleton shares state across all workers | Acceptable: `orchestrator._lock` serializes `initialize_cycle()` calls; each cycle resets the FSM before use |
| GAP-BILLING-01 | `_find_matching_index()` cursor snap race (theoretical) | Acceptable: entire `select_profile()` holds `_lock` during actual selection |

---

## BUGS FIXED IN THIS PR

| ID | File | Description |
|---|---|---|
| BUG-001 | `modules/delay/wrapper.py` | Missing `try/finally` — state stuck on exception |
| BUG-002 | `modules/watchdog/main.py` | Singleton race + cross-thread blindspot → replaced with `worker_id` registry |
| BUG-003 | `integration/orchestrator.py` | `cdp.clear_card_fields()` unguarded in `handle_outcome()` |

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
