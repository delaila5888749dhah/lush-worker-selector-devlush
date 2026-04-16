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
  - If a session already exists for worker_id, the old session is marked _closed=True
    before being replaced, so any concurrent notify_total() callback that resolves
    the old session from the registry will see _closed and become a no-op.
  - Phải được gọi trước wait_for_total
Forbidden:
  - Không import từ module khác (cdp, billing, fsm)
```

### wait_for_total

```
Function: wait_for_total
Input:
  - worker_id (str)
  - timeout (None | float > 0, đơn vị giây)
      * None  → block indefinitely until notify_total fires
      * float → must be strictly positive; 0 or negative raises ValueError
Output: total value (giá trị tổng tiền từ network response)
Constraints:
  - Thread-safe qua threading.Lock
  - ValueError nếu timeout <= 0 (bao gồm 0, số âm)
  - Block cho đến khi nhận được total hoặc hết timeout
  - Nếu timeout hết mà chưa nhận được total → ném SessionFlaggedError
  - enable_network_monitor(worker_id) phải được gọi trước; nếu chưa → ném RuntimeError
  - Sau khi trả kết quả hoặc ném lỗi, tự động xóa session cho worker_id
  - Session được đánh dấu _closed=True trong finally (dưới lock) ngay cả khi bị thay thế
    bởi enable_network_monitor() trong lúc đang wait; điều này ngăn late notify_total()
    ghi đè total_value sau khi session đã hoàn thành.
  - Event.wait(timeout) PHẢI được gọi ngoài _registry_lock để tránh deadlock với
    notify_total() (cũng cần acquire lock để ghi total_value trước khi set event).
Lock discipline (invariant):
  - Acquire lock → read session reference → release lock → call event.wait()
  - Acquire lock → identity-check + pop + mark _closed → release lock
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
  - No-op if the session is already closed (_closed=True): this prevents stale
    CDP callbacks — fired after a session was removed or replaced — from
    contaminating a newer session for the same worker after crash/reset/re-enable.
  - Thread-safe qua threading.Lock
  - total_value được ghi trước khi set event (dưới lock), đảm bảo wait_for_total
    thấy giá trị đúng ngay khi event.wait() trả về.
Forbidden:
  - Không import từ module khác
```

## Internal Helpers (không thuộc public API)

- `_reset_session(worker_id)`: Remove session entry và đánh dấu `_closed=True`.
- `reset_session(worker_id)`: Public wrapper cho `_reset_session`. Dùng bởi orchestrator.
- `reset()`: Reset toàn bộ registry (close all sessions). Dùng cho test isolation.

## Session Lifecycle and _closed Flag

Each `_WatchdogSession` carries a `_closed` boolean (default `False`). The flag is set
to `True` in the following situations (always under `_registry_lock`):

| Trigger | Where |
|---|---|
| `wait_for_total` completes (success or timeout) | `finally` block |
| `enable_network_monitor` replaces an existing session | before creating new session |
| `_reset_session` / `reset_session` removes the session | inside pop block |
| `reset()` clears the entire registry | before `clear()` |

`notify_total` checks `not session._closed` before writing `total_value` or setting
the event. A session is non-closeable from outside the module: callers interact only
through the public API.

## Threading Model

- Per-worker state (`_watchdog_registry[worker_id]`) keyed by worker_id string
- Toàn bộ registry được bảo vệ bởi một `threading.Lock` (`_registry_lock`)
- `wait_for_total` sử dụng per-session `threading.Event` để chờ signal từ `notify_total`
- **`Event.wait(timeout)` PHẢI thực hiện ngoài lock** để tránh deadlock với `notify_total`
  (invariant được enforce bởi lock-release-then-wait sequence trong implementation)
- `notify_total` ghi `total_value` và set event **dưới lock** — ensures visibility from
  any thread and prevents partial writes
- Session replacement is safe: identity check (`is`) in `wait_for_total` finally ensures
  only the exact session being waited on is removed; a newer replacement is never deleted
- Cross-worker isolation: sessions are fully keyed by `worker_id`; workers never share
  session state regardless of concurrent operations (#5 guarantee)

## Timeout Contract (Invariants)

- `timeout=None` → block indefinitely; valid for callers that guarantee an external
  signal (e.g. test harness).
- `timeout > 0` → block at most `timeout` seconds; raises `SessionFlaggedError` on expiry.
- `timeout <= 0` → **invalid**; raises `ValueError` immediately before any lock is
  acquired. This prevents `Event.wait(0)` or `Event.wait(-n)` from silently returning
  `False` without actually waiting, which would misclassify the session as timed-out.

## Luồng sử dụng (Blueprint §5)

```
1. Gọi enable_network_monitor(worker_id)   → Create session (close old if any), bật monitoring
2. Integration layer lắng nghe CDP         → Network.responseReceived
3. Khi response trả về total amount        → Gọi notify_total(worker_id, value)
4. Gọi wait_for_total(worker_id, timeout)  → Block chờ signal (Event.wait outside lock)
   ├── Nhận được signal → return total value; session removed + closed in finally
   └── Hết timeout     → raise SessionFlaggedError; session removed + closed in finally
```
