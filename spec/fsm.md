# FSM Specification

spec-version: 1.0

## ALLOWED_STATES (Tập đóng)
- ui_lock
- success
- vbv_3ds
- declined

## State Semantics
| State     | Mô tả                                                          | Terminal? |
|-----------|----------------------------------------------------------------|-----------|
| ui_lock   | Form đơ, cần focus-shift retry                                 | No        |
| success   | Đơn hàng thành công, URL → /confirmation                       | Yes       |
| vbv_3ds   | Iframe 3D-Secure xuất hiện                                     | No        |
| declined  | Giao dịch bị từ chối — terminal cho worker này                 | Yes       |

## Transitions (Runtime — Phase 3+)
- ui_lock  → success | vbv_3ds | declined
- vbv_3ds  → declined | success
- success  → [terminal — no outgoing transitions]
- declined → [terminal — no outgoing transitions; card swap handled at orchestration level]

## Worker Initialization Rule
- `transition_for_worker()` requires the worker to be initialized via `initialize_for_worker()` first.
- Calling `transition_for_worker()` for an uninitialized worker raises `ValueError("worker '<id>' not initialized")`.
- This rule applies regardless of the target state.

## Terminal-State Integrity
- Workers in terminal states (`success`, `declined`) cannot be transitioned further.
- Any attempt to transition a worker already in a terminal state raises `ValueError` with message
  `"Invalid transition from <terminal> to <target>: '<terminal>' is a terminal state"`.
- Terminal-state protection is enforced inside the registry lock to prevent race conditions
  where late callbacks attempt to advance an already-settled worker.

## Registry Rules
- Mỗi state chỉ đăng ký 1 lần (singleton per name)
- Thread-safe qua Lock (read → validate → write trong single lock acquisition)
- ALLOWED_STATES là tập đóng — không mở rộng runtime
- Per-worker isolation: mỗi worker_id có registry entry độc lập

## Error Contract
| Scenario                                          | Exception              |
|---------------------------------------------------|------------------------|
| state_name not in ALLOWED_STATES                  | InvalidStateError      |
| state_name already exists in registry             | ValueError             |
| target_state not in ALLOWED_STATES                | InvalidStateError      |
| target_state not registered for worker            | InvalidTransitionError |
| worker not initialized (entry is None)            | InvalidTransitionError |
| transition attempted from a terminal state        | ValueError             |
| transition not in _VALID_PAYMENT_TRANSITIONS      | ValueError             |

## reset_states Behavior (Legacy Global API)
- Clears registry (_states.clear())
- Resets current_state to None
- After reset, transition_to will raise InvalidTransitionError
- Thread-safe via Lock
- Legacy global API: deprecated; use per-worker API for production multi-worker usage

