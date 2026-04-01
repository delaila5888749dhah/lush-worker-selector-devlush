# Watchdog Specification

spec-version: 1.0

## Guard 3.3 — Watchdog Lifecycle (Architectural Context)

Guard 3.3 yêu cầu: khi kill worker phải đóng trình duyệt, xóa profile tạm, giải phóng tài nguyên,
ngăn rò rỉ bộ nhớ và zombie process. Phạm vi hiện tại của module này bao gồm **Total Watchdog**
(giám sát tổng tiền qua CDP Network). Phần worker cleanup sẽ được triển khai ở Phase 3+.

## Functions

### enable_network_monitor

```
Function: enable_network_monitor
Input: None
Output: None
Constraints:
  - Thread-safe qua threading.Lock
  - Reset trạng thái monitoring trước đó (clear event, clear total value)
  - Phải được gọi trước wait_for_total
Forbidden:
  - Không import từ module khác (cdp, billing, fsm)
```

### wait_for_total

```
Function: wait_for_total
Input: timeout (int | float, đơn vị giây)
Output: total value (giá trị tổng tiền từ network response)
Constraints:
  - Thread-safe qua threading.Lock
  - Block cho đến khi nhận được total hoặc hết timeout
  - Nếu timeout hết mà chưa nhận được total → ném SessionFlaggedError
  - enable_network_monitor() phải được gọi trước; nếu chưa → ném RuntimeError
  - Sau khi trả kết quả hoặc ném lỗi, tự động disable monitor
Forbidden:
  - Không tự gọi CDP/Network API (module isolation)
  - Không reload trang
  - Không import từ module khác
```

## Internal Helpers (không thuộc public API)

- `_notify_total(value)`: Signal rằng total amount đã nhận được. Dùng cho integration layer bên ngoài module.
- `_reset_monitor()`: Reset toàn bộ state (monitor_enabled, total_value, event). Dùng cho test isolation.

## Threading Model

- Module-level state (`_monitor_enabled`, `_total_value`) được bảo vệ bởi `threading.Lock`
- `wait_for_total` sử dụng `threading.Event` để chờ signal từ `_notify_total`
- `Event.wait(timeout)` thực hiện **ngoài lock** để tránh deadlock
- `_notify_total` set event **sau khi** cập nhật `_total_value` trong lock

## Luồng sử dụng (Blueprint §5)

```
1. Gọi enable_network_monitor()         → Reset state, bật monitoring
2. Integration layer lắng nghe CDP       → Network.responseReceived
3. Khi response trả về total amount      → Gọi _notify_total(value)
4. Gọi wait_for_total(timeout=10)        → Block chờ signal
   ├── Nhận được signal → return total value
   └── Hết timeout     → raise SessionFlaggedError
```
