# Spec Versioning System

spec-version: 1.0

## Version Format

Mỗi file Spec sử dụng phiên bản theo định dạng `MAJOR.MINOR`:

| Thành phần | Ý nghĩa | Ví dụ |
|------------|---------|-------|
| **MAJOR** | Thay đổi phá vỡ (breaking change): xóa function, đổi tên param, thay đổi output type | `1.0` → `2.0` |
| **MINOR** | Thay đổi tương thích (additive): thêm function mới, thêm optional param | `1.0` → `1.1` |

## Version Header

Mỗi file Spec bắt buộc chứa dòng version ở đầu file:

```
spec-version: MAJOR.MINOR
```

## Migration Rules

### Khi bump MINOR (1.0 → 1.1)
1. Code hiện tại tiếp tục hoạt động bình thường
2. CI tự động phát hiện function mới chưa có implementation → Agent tạo stub
3. Không cần migration script

### Khi bump MAJOR (1.x → 2.0)
1. Architect tạo Issue mô tả breaking changes
2. Tạo migration checklist trong Issue body:
   - [ ] Liệt kê các function bị xóa/đổi tên
   - [ ] Liệt kê các module bị ảnh hưởng
   - [ ] Cập nhật tests tương ứng
3. CI `check_signature` sẽ fail cho đến khi code đồng bộ với spec mới
4. Sử dụng `CHANGE_CLASS=spec_sync` để bypass module limit khi sync

## Changelog

Mỗi thay đổi version phải được ghi nhận trong phần `## Changelog` ở cuối file Spec
hoặc trong commit message với prefix `[spec-vX.Y]`.

## Current Versions

| File | Version | Cập nhật |
|------|---------|----------|
| `spec/core/interface.md` | 8.0 | 2026-04-28 |
| `spec/integration/interface.md` | 8.2 | 2026-04-30 |
| `spec/interface.md` (aggregated) | 8.3 | 2026-04-30 |
| `spec/fsm.md` | 1.1 | 2026-04-23 |
| `spec/watchdog.md` | 1.0 | 2026-04-01 |
| `spec/VERSIONING.md` | 1.0 | 2026-04-01 |
| `spec/deployment.md` | 1.0 | 2026-04-04 |
| `spec/cdp-timeout-contract.md` | 1.1 | 2026-04-16 |

## Changelog

### v8.3 / v8.2 (2026-04-30) — ADDITIVE (Phase A reorder / INV-PAYMENT-01 fix)
- `spec/interface.md` (→ 8.3), `spec/integration/interface.md` (→ 8.2): Added two new
  public functions to `Module: cdp`:
  - `run_pre_card_checkout_prepare(task, billing_profile, worker_id)` — performs geo
    check (idempotent), navigation, eGift form fill, add-to-cart, and guest-checkout
    selection. Does NOT type card/billing fields. Safe before Phase A pricing wait.
  - `run_payment_card_fill(card_info, billing_profile, worker_id)` — types card and
    billing payment fields. MUST only be called after Phase A total is confirmed
    (INV-PAYMENT-01 / Blueprint §5).
  - `run_preflight_and_fill` retained as backward-compatible alias calling both above
    in sequence; existing callers continue to work without modification.
- `integration/orchestrator.py::run_payment_step` reordered so navigation precedes the
  Phase A watchdog wait. Fixes `ALLOW_DOM_ONLY_WATCHDOG=1` deadlock where DOM polling
  was querying `about:blank` before the browser navigated to the Givex checkout page.
- MINOR bump per VERSIONING_ENFORCEMENT Rule 3 (additive, backward-compatible).

### v8.0 / v8.1 / v8.2 (2026-04-28) — BREAKING (FSM transition signature)
- `spec/core/interface.md` (→ 8.0), `spec/integration/interface.md` (→ 8.1),
  `spec/interface.md` (→ 8.2): `transition_for_worker` gained an optional
  `trace_id: str | None = None` parameter to support the canonical 6-field
  structured FSM transition log (`timestamp | worker_id | trace_id | state |
  action | status`). Major bump per Rule 6 VERSIONING_ENFORCEMENT because the
  parameter list of a public spec function changed.

### v7.2 (2026-04-24) — ADDITIVE (Blueprint §2.1)
- `spec/interface.md`: Declared `BitBrowserPoolClient` (pool-mode BitBrowser
  profile manager — round-robin sequential, thread-safe) under `Module: cdp`.
  Activated via `BITBROWSER_POOL_MODE=1` + `BITBROWSER_PROFILE_IDS` CSV.
  Legacy `BitBrowserClient` behaviour unchanged (backward-compatible).

### v7.1 (2026-04-23) — ADDITIVE
- `spec/integration/interface.md`: Added `monitor.record_ui_lock_retry()`, `monitor.record_ui_lock_recovered()`, and `monitor.record_ui_lock_exhausted()` for UI-lock recovery observability.
- `spec/interface.md`: Aggregated the same monitor UI-lock metric APIs to keep segmented and aggregated interface specs aligned.

### v7.0 (2026-04-21) — BREAKING
- Added `CDPError` exception type to `modules.common.exceptions` (plain `Exception` subclass)
- Raised by `GivexDriver.clear_card_fields_cdp` when the underlying CDP command fails (P1-4)
- Orchestrator `retry_new_card` block catches `CDPError` and aborts the cycle instead of resubmitting — prevents double-charge when card fields still contain stale data

### v6.0 (2026-04-20) — BREAKING
- Added `CDPCommandError` exception type to `modules.common.exceptions` (inherits `SessionFlaggedError`)
- Attributes: `command` (CDP method name), `detail` (PII-sanitized error string)
- Used by `_safe_cdp_cmd()` helper in `modules/cdp/driver.py` for typed CDP error handling

### v1.1 (2026-04-16) — ADDITIVE
- `spec/cdp-timeout-contract.md`: Added INV-CDP-EXEC-01 (executor saturation, orphaned threads), INV-CDP-NOTIFY-01 (first-notify-wins dual-notify race safety), INV-CDP-SHUTDOWN-01 (bounded shutdown observability), and executor health metrics table

### v5.0 (2026-04-08) — BREAKING
- CDP functions (`detect_page_state`, `fill_card`, `fill_billing`, `clear_card_fields`) now require `worker_id` parameter for multi-worker deployment safety
- Added `reset_session(worker_id)` public API to watchdog module

### v4.0 (2026-04-07) — BREAKING
- Added `SelectorTimeoutError` and `PageStateError` exception types to `modules.common.exceptions`
- `WorkerTask` is now `frozen=True` (immutable dataclass)
- Added `register_driver(worker_id, driver)` and `unregister_driver(worker_id)` to CDP module (driver registry)
- Created `spec/cdp-timeout-contract.md` — CDP timeout and error-handling rules

### v2.0 (2026-04-02) — BREAKING
- **Exception types** (`InvalidStateError`, `InvalidTransitionError`, `SessionFlaggedError`, `CycleExhaustedError`) moved from `spec.schema` to `modules.common.exceptions`
- **Data types** (`State`, `CardInfo`, `BillingProfile`, `WorkerTask`) moved from `spec.schema` to `modules.common.types`
- `spec/` is no longer a runtime dependency — enforces architecture boundary between contract and implementation
- All `modules/` imports rewired to `modules.common`
