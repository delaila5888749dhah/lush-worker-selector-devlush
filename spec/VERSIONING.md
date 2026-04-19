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
| `spec/core/interface.md` | 5.0 | 2026-04-08 |
| `spec/integration/interface.md` | 5.3 | 2026-04-19 |
| `spec/interface.md` (aggregated) | 5.0 | 2026-04-08 |
| `spec/fsm.md` | 1.0 | 2026-04-01 |
| `spec/watchdog.md` | 1.0 | 2026-04-01 |
| `spec/VERSIONING.md` | 1.0 | 2026-04-01 |
| `spec/deployment.md` | 1.0 | 2026-04-04 |
| `spec/cdp-timeout-contract.md` | 1.1 | 2026-04-16 |

## Changelog

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
