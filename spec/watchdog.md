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
Input: worker_id (str)
Output: None
Constraints:
  - Thread-safe qua threading.Lock
  - Create or reset a per-worker watchdog session
  - Phải được gọi trước wait_for_total
Forbidden:
  - Không import từ module khác (cdp, billing, fsm)
```

### wait_for_total

```
Function: wait_for_total
Input:
  - worker_id (str)
  - timeout (int | float, đơn vị giây)
Output: total value (giá trị tổng tiền từ network response)
Constraints:
  - Thread-safe qua threading.Lock
  - Block cho đến khi nhận được total hoặc hết timeout
  - Nếu timeout hết mà chưa nhận được total → ném SessionFlaggedError
  - enable_network_monitor(worker_id) phải được gọi trước; nếu chưa → ném RuntimeError
  - Sau khi trả kết quả hoặc ném lỗi, tự động xóa session cho worker_id
Forbidden:
  - Không tự gọi CDP/Network API (module isolation)
  - Không reload trang
  - Không import từ module khác
```

### notify_total

```
Function: notify_total
Input:
  - worker_id (str)
  - value
Output: None
Constraints:
  - Safe to call from ANY thread (browser CDP event thread, worker thread, etc.)
  - No-op if no session exists for worker_id (idempotent)
  - Thread-safe qua threading.Lock
Forbidden:
  - Không import từ module khác
```

## Internal Helpers (không thuộc public API)

- `_reset_session(worker_id)`: Remove session entry cho worker_id. Dùng nội bộ bởi wait_for_total.
- `reset()`: Reset toàn bộ registry. Dùng cho test isolation.

## Threading Model

- Per-worker state (`_watchdog_registry[worker_id]`) keyed by worker_id string
- Toàn bộ registry được bảo vệ bởi `threading.Lock` (`_registry_lock`)
- `wait_for_total` sử dụng per-session `threading.Event` để chờ signal từ `notify_total`
- `Event.wait(timeout)` thực hiện **ngoài lock** để tránh deadlock
- `notify_total` set event **sau khi** cập nhật `total_value` — safe from any thread

## Luồng sử dụng (Blueprint §5)

```
1. Gọi enable_network_monitor(worker_id)   → Create session, bật monitoring
2. Integration layer lắng nghe CDP         → Network.responseReceived
3. Khi response trả về total amount        → Gọi notify_total(worker_id, value)
4. Gọi wait_for_total(worker_id, timeout)  → Block chờ signal
   ├── Nhận được signal → return total value
   └── Hết timeout     → raise SessionFlaggedError
```
