# FSM Specification

spec-version: 1.0

## ALLOWED_STATES (Tập đóng)
- ui_lock
- success
- vbv_3ds
- declined

## State Semantics
| State     | Mô tả                                           | Terminal? |
|-----------|--------------------------------------------------|-----------|
| ui_lock   | Form đơ, cần focus-shift retry                   | No        |
| success   | Đơn hàng thành công, URL → /confirmation         | Yes       |
| vbv_3ds   | Iframe 3D-Secure xuất hiện                       | No        |
| declined  | Giao dịch bị từ chối, cần swap thẻ              | No        |

## Transitions (Runtime — Phase 3+)
- ui_lock  → success | vbv_3ds | declined
- vbv_3ds  → declined | success
- declined → declined (swap thẻ) | [end cycle]

## Registry Rules
- Mỗi state chỉ đăng ký 1 lần (singleton per name)
- Thread-safe qua Lock
- ALLOWED_STATES là tập đóng — không mở rộng runtime
spec
fsm.md
