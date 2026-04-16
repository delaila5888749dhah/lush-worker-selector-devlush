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
  - Always creates a brand-new, isolated session for worker_id (even if a
    stale session already exists due to crash/restart-like flows).
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
  - Event.wait(timeout) được gọi NGOÀI lock để tránh deadlock
  - Cleanup trong finally dùng kiểm tra identity (is) để bảo vệ session mới
    nếu enable_network_monitor() được gọi lại trong khi wait đang chờ
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
  - total_value được set dưới lock; event.set() được gọi NGOÀI lock để
    đảm bảo notifier thread không bao giờ bị block bởi waiter trong finally
  - Nếu session bị remove/reset trước khi notify đến → no-op an toàn,
    không mutate session mới hoặc bất kỳ session tương lai nào
Forbidden:
  - Không import từ module khác
```

### reset_session

```
Function: reset_session
Input: worker_id (str)
Output: None
Constraints:
  - Thread-safe qua threading.Lock
  - Xóa session entry cho worker_id khỏi registry (public API cho orchestrator)
  - Sau khi gọi, mọi wait_for_total() đang block sẽ không bị unblock
    bởi reset này — chúng sẽ tự timeout hoặc nhận signal từ notify_total()
    đã được đặt trong session cũ trước khi reset
Notes:
  - Late notify_total() sau khi reset_session() là no-op an toàn
```

## Internal Helpers (không thuộc public API)

- `_reset_session(worker_id)`: Remove session entry cho worker_id. Dùng nội bộ.
- `reset()`: Reset toàn bộ registry. Dùng cho test isolation.

## Timeout Contract

Caller convention (xem `integration/orchestrator.py`):

```
_WATCHDOG_TIMEOUT = 30  # giây
watchdog.wait_for_total(worker_id, timeout=_WATCHDOG_TIMEOUT)
```

- Timeout mặc định được dùng trong orchestrator là **30 giây**.
- Delay layer đảm bảo tổng behavioral delay ≤ 7.0s/bước, để lại ≥ 23s headroom.
- `spec/blueprint.md` (§5 và §Timing Invariants) đề cập "watchdog timeout = 10s" là
  narrative cũ và không phản ánh giá trị triển khai thực tế; contract triển khai là 30s
  (xem `integration/orchestrator.py: _WATCHDOG_TIMEOUT = 30` và `spec/cdp-timeout-contract.md`).
- Module watchdog không enforce giá trị timeout cụ thể — timeout được truyền từ caller.
  Caller có trách nhiệm dùng giá trị phù hợp với SLA của hệ thống.

## Threading Model

- Per-worker state (`_watchdog_registry[worker_id]`) keyed by worker_id string
- Toàn bộ registry được bảo vệ bởi `threading.Lock` (`_registry_lock`)
- `wait_for_total` sử dụng per-session `threading.Event` để chờ signal từ `notify_total`
- `Event.wait(timeout)` thực hiện **ngoài lock** để tránh deadlock
- `notify_total` cập nhật `total_value` **dưới lock**, sau đó gọi `event.set()` **ngoài lock**
  — safe from any thread và đảm bảo notifier không bị block bởi waiter's finally
- `wait_for_total.finally` dùng kiểm tra identity (`is`) thay vì chỉ kiểm tra `worker_id`:
  bảo vệ session mới nếu `enable_network_monitor()` được gọi lại trong khi wait đang chờ

## Session Replacement Race Protection

Nếu `enable_network_monitor(worker_id)` được gọi trong khi một `wait_for_total(worker_id)`
đang block (session A đang chờ), luồng thay thế diễn ra như sau:

```
1. Session A đang trong event.wait()
2. enable_network_monitor() tạo Session B → ghi đè registry[worker_id]
3. notify_total() (nếu có) signal Session B
4. Session A hết timeout → finally kiểm tra: registry[worker_id] is session_a?
   → FALSE (registry bây giờ trỏ đến Session B)
   → KHÔNG xóa Session B khỏi registry (đúng!)
5. Session B vẫn còn trong registry, usable cho caller tiếp theo
```

## Luồng sử dụng (Blueprint §5)

```
1. Gọi enable_network_monitor(worker_id)   → Create session, bật monitoring
2. Integration layer lắng nghe CDP         → Network.responseReceived
3. Khi response trả về total amount        → Gọi notify_total(worker_id, value)
4. Gọi wait_for_total(worker_id, timeout)  → Block chờ signal
   ├── Nhận được signal → return total value
   └── Hết timeout     → raise SessionFlaggedError
```

