# SPEC-6-FINAL-EXECUTION-WORKFLOW

**Phiên bản:** 2.0 — Native AI Workflow (GitHub Copilot Business)
**Cập nhật:** 2026-04-01
**Trạng thái:** Sẵn sàng triển khai

---

## 1. Nguyên tắc nền tảng

1. **Spec = Luật** — Không viết code khi chưa có đặc tả được Architect phê duyệt.
2. **CI = Cưỡng chế** — Không dựa vào ý thức của người hoặc AI; máy tự kiểm tra.
3. **Runtime Checkpoint = Xác thực cuối cùng** — Mọi logic đều phải được chạy thực tế trước khi phê duyệt.
4. **Cách ly tuyệt đối** — Mỗi module độc lập, không phụ thuộc chéo.
5. **Mỗi tác vụ = Một phạm vi nhỏ** — Không suy diễn, không thêm chức năng ngoài yêu cầu.
6. **Zero-Unapproved External AI** — Tuyệt đối không sử dụng AI bên ngoài hệ sinh thái GitHub Copilot, ngoại trừ các công cụ đã được Architect phê duyệt rõ ràng trong spec (ví dụ: Gemini 3.1 Pro cho bước Cross-Inspector). Không được dùng DeepSeek, ChatGPT Web hoặc copy-paste thủ công. Mọi thao tác AI chính (soạn code, review, autofix) bắt buộc đi qua GitHub native pipeline.
7. **Single Source of Truth** — Issue/PR là trung tâm điều phối duy nhất. Không dùng kênh ngoài (Slack, email, file local) để truyền tải Spec hoặc review.

---

## 2. Cấu trúc Phase (Sơ đồ phân cấp)

```
SPEC-6 EXECUTION WORKFLOW (Native AI)
│
├── Phase 1 — Spec Lock & Infrastructure (2–3 ngày)
│   ├── Đóng băng đặc tả: FSM, interface, schema
│   ├── Tạo repo, branch protection, CI skeleton
│   ├── Lưu trữ trong /spec/ (fsm.md, interface.md, schema.py)
│   ├── Cấu hình Copilot Business: Memory Index, CodeQL, Push Protection
│   └── 🏁 Milestone: Spec hoàn chỉnh, CI + Security Gates chạy được
│
├── Phase 2 — Module Isolation & CI Enforcement (2–3 ngày)
│   ├── Tạo 4 module: fsm, cdp, billing, watchdog (thư mục /modules/)
│   ├── CI rules (GitHub Actions):
│   │   ├── check_import_scope — cấm import chéo module
│   │   ├── check_signature — function phải match spec
│   │   ├── check_pr_scope — 1 PR ≤ 200 dòng, chỉ 1 module
│   │   └── check_spec_lock — cấm sửa /spec/*
│   ├── Security Gates (Guard 4.9):
│   │   ├── CodeQL Code Scanning (auto-enabled)
│   │   ├── Dependabot (Security + Version + Grouped)
│   │   ├── Secret Scanning + Push Protection + Validity Checks
│   │   └── Copilot Autofix (auto-suggestion on alerts)
│   ├── PR Rulesets: Require PR, Require Copilot Review, Admin Always Bypass
│   └── 🏁 Milestone: CI bắt được lỗi import/signature/scope, Security Gates chặn được vulnerability
│
├── Phase 3 — Implementation (5–7 ngày)
│   ├── Branch strategy: main (protected) ← develop ← feature/<module>/<function>
│   ├── Native AI Workflow:
│   │   ├── Architect (Opus 4.6) phân tích Issue → viết Spec
│   │   ├── Human Assign Issue → Copilot Coding Agent (Codex 5.2)
│   │   ├── Agent tự đọc repo + Spec → sinh code + unit test → push PR
│   │   ├── Reviewer (GPT-5.4) auto-review PR → APPROVED / REQUEST_CHANGES
│   │   ├── Nếu reject: Agent auto-fix → push lại (Auto-Fix Loop)
│   │   ├── Nếu reject ≥3 lần: Circuit Breaker → Gemini 3.1 Pro phân xử
│   │   └── Merge vào develop sau khi CI pass + Review approve + Security Gates clear
│   ├── Integration sớm:
│   │   ├── Không dùng mock phức tạp — chỉ stub đơn giản (trả đúng format)
│   │   ├── Sau khi có đủ module tối thiểu (fsm + cdp + billing), chạy smoke test
│   │   │   kiểm tra interface compatibility (không test business logic)
│   └── 🏁 Milestone: 4 module hoàn chỉnh, unit test pass, smoke test pass
│
├── Phase 4 — Integration & Staging Validation (3–4 ngày)
│   ├── Tích hợp toàn bộ module (branch integration ← develop)
│   ├── Staging environment:
│   │   ├── Site thật, proxy thật
│   │   ├── Dataset riêng biệt (không ảnh hưởng production)
│   │   ├── Có kill-switch toàn cục để dừng khẩn cấp
│   ├── Rollout: 1 worker → 3 worker
│   ├── Kiểm tra bắt buộc:
│   │   ├── Không double-consume (billing atomic)
│   │   ├── FSM không kẹt, không lỗi state
│   │   ├── Watchdog kill/restart đúng
│   │   ├── CDP network listener hoạt động (chờ total amount)
│   │   └── Log trace đầy đủ
│   ├── Định lượng "ổn định":
│   │   ├── success rate ≥ 70%
│   │   ├── worker restart count < 2 / 24h
│   │   ├── memory usage < 1.5G
│   │   └── không double-consume
│   └── 🏁 Milestone: 3 workers chạy 24h đạt các chỉ số trên
│
├── Phase 5 — Production Rollout (3–5 ngày)
│   ├── Rollout theo nấc: 1 → 3 → 5 → 10 workers
│   ├── Mỗi nấc chạy 12–24h trước khi tăng
│   ├── Giám sát liên tục:
│   │   ├── success rate
│   │   ├── error rate
│   │   ├── memory usage
│   │   ├── số lần worker die
│   ├── Rollback trigger (tự động hoặc thủ công) nếu:
│   │   ├── success rate giảm mạnh (>10% so với nấc trước)
│   │   ├── error rate tăng đột biến (>5%)
│   │   ├── memory > 2G
│   │   └── worker die > 3 lần trong 1h
│   └── 🏁 Milestone: 10 workers chạy 24h ổn định với tất cả chỉ số trong ngưỡng
│
└── Phase 6 — Handover & Operations (2 ngày)
    ├── Viết runbook (hướng dẫn start/stop, đọc log, fallback thủ công)
    ├── Cấu hình cron dọn cache browser profile (1 lần/ngày)
    ├── Backup billing pool (SQLite) định kỳ
    └── 🏁 Milestone: Tài liệu đầy đủ, sẵn sàng bàn giao cho vận hành

├── Phase 7 — Post-Finalization Audit Validation (2–3 ngày)
│   ├── Audit scope (từ Phase 6 audit tasks):
│   │   ├── Task 2 — Lifecycle state machine: ALLOWED_STATES enforcement
│   │   │   ├── Replace _running boolean với explicit state machine
│   │   │   ├── ALLOWED_STATES = {"INIT", "RUNNING", "STOPPING", "STOPPED"}
│   │   │   ├── start() chỉ chấp nhận từ INIT/STOPPED
│   │   │   └── stop() transitions RUNNING → STOPPING → STOPPED
│   │   ├── Task 3 — Start/stop race condition
│   │   │   ├── STOPPING state chặn concurrent start() trong teardown
│   │   │   ├── Deterministic barriers cho concurrent testing
│   │   │   └── Thread completion validation sau join()
│   │   ├── Task 4 — Zombie worker cleanup
│   │   │   ├── _log_event di chuyển vào try block (finally luôn chạy)
│   │   │   ├── Guard t.start() với try/except RuntimeError, OSError
│   │   │   └── Timed-out workers removed từ _workers dict
│   │   ├── Task 5 — Worker registry consistency
│   │   │   ├── Guard thread.join() cho not-yet-started thread (ident is None)
│   │   │   ├── Guard against thread is current_thread()
│   │   │   └── Concurrent spawn yields unique IDs
│   │   ├── Task 6 — Failure mode handling
│   │   │   ├── Guard monitor.record_success() — worker survives monitor failure
│   │   │   ├── Guard monitor.record_error() — task error luôn được logged
│   │   │   ├── Catch-all except Exception logged via _logger.error()
│   │   │   └── Guard thread.join() trong stop_worker với RuntimeError
│   │   ├── Task 7 — Determinism enforcement
│   │   │   ├── No randomness in rollout/monitor/runtime (AST-verified)
│   │   │   ├── Rollout decisions = pure functions of state + check_fn()
│   │   │   ├── Monitor decisions = pure functions of counter state + thresholds
│   │   │   └── reset() fully restores initial state with no leakage
│   │   └── Task 8 — Observability (Guard 3.5 compliance)
│   │       ├── 6-field structured log: timestamp | worker_id | trace_id | state | action | status
│   │       ├── trace_id = uuid4().hex[:12], generated per lifecycle in start()
│   │       ├── get_trace_id() public API, included in get_status()
│   │       └── _trace_lock riêng biệt (tránh deadlock với _lock)
│   ├── Validation process:
│   │   ├── 2 vòng audit (Round 1: phát hiện + fix, Round 2: xác nhận)
│   │   ├── Multi-run test execution (3 consecutive runs)
│   │   └── 266 tests pass at 100% across all runs
│   ├── Measured outcomes:
│   │   ├── 100% test pass rate (266 tests tại thời điểm audit closure)
│   │   ├── 80 audit-specific tests validate all hardening requirements
│   │   ├── No flaky tests detected across consecutive runs
│   │   ├── No race conditions — _lock, _trace_lock, _stop_requests verified
│   │   ├── No hidden failures — all exception paths logged explicitly
│   │   └── No incomplete fixes — all audit items from both rounds addressed
│   ├── Final system state:
│   │   ├── Audit-consistent: code matches audit intent with zero gaps
│   │   ├── Production-hardened: all concurrency paths verified under stress
│   │   ├── Zero cross-module imports, all globals lock-protected
│   │   └── CI fully green including meta_audit, CodeQL 0 alerts
│   └── 🏁 Milestone: System audit-consistent, production-hardened, zero confirmed remaining issues

├── Phase 8 — Production Deployment & Monitoring (2–3 ngày)
    ├── Step 1 — Deploy hệ thống ra production environment
    │   ├── get_deployment_status() — comprehensive health snapshot:
    │   │   ├── Combines runtime state + monitor metrics
    │   │   ├── Returns: running, state, worker_count, active_workers,
    │   │   │   consecutive_rollbacks, trace_id, metrics
    │   │   ├── Resilient: returns metrics=None if monitor unavailable
    │   │   └── Thread-safe via existing Lock guards
    │   └── CI fixes cho production readiness:
    │       ├── meta_audit.py: graceful empty changeset handling
    │       ├── check_spec_lock.py: honor ALLOW_SPEC_MODIFICATION env var
    │       └── meta_audit.py RULE 4: consistent spec lock enforcement
    ├── Step 2 — Verify production deployment status
    │   ├── verify_deployment() — programmatic health check:
    │   │   ├── Kiểm tra 3 acceptance criteria:
    │   │   │   ├── service_running: state == "RUNNING"
    │   │   │   ├── workers_active: worker_count > 0
    │   │   │   └── no_startup_errors: rollbacks == 0, error_rate ≤ 5%, restarts ≤ 3/hr
    │   │   └── Returns structured pass/fail result với checks và errors
    │   └── 14 tests covering healthy path và từng failure mode
    ├── Step 3 — Setup production monitoring
    │   ├── Logging observation: runtime logger emits structured info-level events
    │   ├── Trace ID observation: assigned on start, 12 lowercase hex chars, unique across restarts
    │   ├── Metrics observation: get_metrics() returns dict với all documented keys
    │   └── Monitor failure resilience: metrics=None on error, system continues
    ├── Step 4 — Runtime observation & behavior tracking
    │   ├── Crash resilience: worker crash không bring down runtime
    │   ├── Restart thresholds: >3 restarts/hr triggers rollback flag
    │   ├── Error rate stability: consistent across multiple reads
    │   └── Combined validation: verify_deployment() confirms all criteria
    ├── Step 5 — Baseline recording & metrics snapshot
    │   ├── Capture worker_count, error_rate, restart_count as baseline
    │   ├── Verify no None values at stable state after save_baseline()
    │   └── Stable state: healthy thresholds met, verify_deployment() passes
    ├── Extension spec for future upgrades (spec/deployment.md v1.0):
    │   ├── Extension 1 — Metrics Export (Prometheus/CloudWatch)
    │   ├── Extension 2 — Alerting Rules
    │   ├── Extension 3 — Health Check Endpoint
    │   ├── Extension 4 — Structured Log Aggregation
    │   └── Extension 5 — Deployment Automation (CI/CD)
    ├── Validation results:
    │   ├── 340 tests pass tại Phase 8 completion (271 baseline + 69 Phase 8)
    │   ├── All observation tests exercise existing code paths only
    │   ├── Zero production code modifications in observation steps
    │   └── Validation/observation rule enforced: "observe system behavior, do not modify the system during validation steps"
    └── 🏁 Milestone: Production monitoring active, deployment verified, baseline recorded, extension spec defined

└── Phase 9 — Behavior & Scaling Intelligence (2–3 ngày)
    ├── Purpose:
    │   ├── Chuyển system từ passive scaling → adaptive auto-scaling
    │   └── Runtime tự động điều chỉnh worker count dựa trên metrics thực tế
    ├── Task 1 — Behavior Decision Engine (modules/behavior/main.py):
    │   ├── Pure rule-based logic, thread-safe via threading.Lock
    │   ├── evaluate(metrics, current_step_index, max_step_index) → (action, reasons)
    │   ├── Input metrics:
    │   │   ├── error_rate (float)
    │   │   ├── success_rate (float)
    │   │   ├── restarts_last_hour (int)
    │   │   └── baseline_success_rate (float, optional)
    │   ├── Output actions: SCALE_UP, SCALE_DOWN, HOLD
    │   ├── Decision rules:
    │   │   ├── Rule 0 — Cooldown guard: 30s minimum between decisions → HOLD
    │   │   ├── Rule 1 — error_rate > 5% → SCALE_DOWN
    │   │   ├── Rule 2 — restarts > 3/hr → SCALE_DOWN
    │   │   ├── Rule 3 — success_rate drop > 10% from baseline → SCALE_DOWN
    │   │   ├── Rule 4 — all metrics healthy + success_rate ≥ 70% + not at max → SCALE_UP
    │   │   └── Rule 5 — already at min scale (step 0) → HOLD (prevent under-scale)
    │   ├── Supporting APIs:
    │   │   ├── get_decision_history() — bounded to 100 entries
    │   │   ├── get_last_decision_time() — epoch timestamp
    │   │   ├── get_status() — thresholds + decision count snapshot
    │   │   └── reset() — clear state for testing
    │   └── Safety constraints:
    │       ├── No cross-module imports (zero external dependencies)
    │       ├── All state guarded by _lock (thread-safe)
    │       └── Decision history bounded (max 100 entries)
    ├── Task 2 — Scaling Execution Layer (integration/runtime.py):
    │   ├── behavior.evaluate() called inside _runtime_loop each tick
    │   ├── Decision routing:
    │   │   ├── SCALE_UP → rollout.try_scale_up()
    │   │   ├── SCALE_DOWN → rollout.force_rollback(reason=decision_reasons)
    │   │   └── HOLD → no change (keep current workers)
    │   ├── Consecutive rollback tracking:
    │   │   ├── Incremented on each rollback action
    │   │   ├── Only cleared on actual "scaled_up" action (not on hold)
    │   │   └── Warning logged when count reaches threshold
    │   ├── Integration behavior:
    │   │   ├── Uses existing rollout interfaces (no new module APIs)
    │   │   ├── Preserves lifecycle states (INIT/RUNNING/STOPPING/STOPPED)
    │   │   └── behavior.reset() added to runtime.reset()
    │   └── No module isolation violation:
    │       └── integration/ imports from modules/ (allowed by architecture)
    ├── Validation (from CI & tests):
    │   ├── 33 behavior decision engine tests (test_behavior.py):
    │   │   ├── All decision rules covered individually
    │   │   ├── Cooldown enforcement verified
    │   │   ├── Decision history recording and bounding
    │   │   ├── Thread-safety under concurrent evaluation
    │   │   └── Reset and status API contracts
    │   ├── 13 scaling execution tests (test_scaling_execution.py):
    │   │   ├── Decision routing (SCALE_UP/DOWN/HOLD → correct rollout call)
    │   │   ├── Consecutive rollback tracking and clearing
    │   │   ├── Lifecycle integrity during scaling decisions
    │   │   └── Concurrent thread-safe operation
    │   ├── 386 total tests pass (340 baseline + 46 Phase 9)
    │   ├── No regressions to existing tests
    │   └── CI fully green
    └── 🏁 Milestone: System auto-scales based on runtime metrics, behavior engine operational, all decision paths tested

└── Phase 10 — Behavior Layer (Blueprint-safe) (Status: Designed)
    ├── Purpose:
    │   ├── Thêm behavioral delay vào worker execution layer
    │   ├── Mô phỏng hành vi người dùng thực (typing, click, hesitation)
    │   └── KHÔNG thay đổi control logic, scaling, hoặc orchestration
    ├── 10.1 — Architecture:
    │   ├── Behavior wrapper ONLY tại worker execution layer:
    │   │   └── worker_fn → wrap(task_fn)
    │   └── KHÔNG:
    │       ├── Inject vào runtime loop
    │       ├── Inject vào scaling logic
    │       └── Modify orchestration flow
    ├── 10.2 — FSM Context (MANDATORY):
    │   ├── BehaviorState MUST include context:
    │   │   ├── IDLE — between actions, awaiting next step
    │   │   ├── FILLING_FORM — form field interaction (recipient, billing)
    │   │   ├── PAYMENT — payment data entry (card number, CVV)
    │   │   ├── VBV — 3DS iframe handling
    │   │   └── POST_ACTION — after submit, waiting for result
    │   └── Rule: Delay decision MUST depend on current context
    ├── 10.3 — CRITICAL_SECTION Awareness (Phase 9 alignment):
    │   ├── CRITICAL_SECTION (defined in Phase 9):
    │   │   ├── Payment submit (Complete Purchase execution)
    │   │   ├── VBV/3DS handling (iframe interaction + wait)
    │   │   └── API wait (CDP Network.responseReceived pending)
    │   └── Rule: If in CRITICAL_SECTION → NO delay injected
    ├── 10.4 — SAFE POINT / SAFE ZONE Rule:
    │   ├── Delay ONLY permitted at (SAFE ZONE):
    │   │   ├── UI interaction (typing, click, hover)
    │   │   └── Non-critical steps (form navigation, field focus)
    │   └── Delay NOT permitted at:
    │       ├── Execution control (scaling, lifecycle transitions)
    │       └── System coordination (runtime loop, watchdog checks)
    ├── 10.5 — NO-DELAY Zone (STRICT):
    │   └── Behavior layer MUST NOT inject delay vào:
    │       ├── Payment submit (Complete Purchase click event)
    │       ├── Watchdog timeout checks
    │       ├── Network wait (CDP Network.responseReceived)
    │       ├── VBV iframe load/interaction
    │       └── Page reload operations
    ├── 10.6 — Action-Aware Delay (Bounded):
    │   ├── Delay MUST depend on action type:
    │   │   ├── typing — key-by-key with hesitation (Blueprint §4: CDP gõ phím, quy tắc 4x4)
    │   │   ├── click — bounding box calculation + hover + offset (Blueprint §4: Bounding Box Click)
    │   │   └── thinking — review/hesitation before action (Blueprint §5: Hesitation)
    │   ├── Delay bound:
    │   │   ├── Bounded per UI interaction type (appropriate for human-like behavior)
    │   │   └── MUST NOT affect watchdog timeout or system-level deadlines
    │   └── Deterministic: Seed-based random (Blueprint §2: Gắn Seed Hành Vi)
    │       └── Reproducible execution: same seed → same behavior pattern
    ├── 10.7 — Non-Interference Rule (MANDATORY):
    │   └── Behavior layer MUST NOT:
    │       ├── Break critical execution timing (no delay in CRITICAL_SECTION)
    │       ├── Disrupt FSM flow (state transitions unchanged by behavior)
    │       ├── Cause runtime side-effects (no state mutation outside behavior)
    │       ├── Alter execution order (step sequence unchanged)
    │       └── Change success/failure outcome (logic path unchanged)
    ├── 10.8 — Phase 9 Alignment (STRICT):
    │   ├── Phase 10 MUST respect:
    │   │   ├── SAFE_POINT — behavior operates within safe boundaries only
    │   │   └── CRITICAL_SECTION — zero interference during critical operations
    │   └── Phase 10 MUST NOT operate outside permitted scope
    ├── Constraints:
    │   ├── KHÔNG thay đổi kiến trúc wrapper (wrapper pattern only)
    │   ├── KHÔNG thêm module mới (uses existing modules/delay/)
    │   └── KHÔNG mở rộng scope (behavior delay only, no new features)
    └── 🏁 Milestone: Behavior layer designed, Blueprint-safe, Phase 9 aligned, ready for implementation
```

---

## 3. Các điểm kiểm soát bắt buộc (Guards)

### Guard 3.1 — Blueprint → Test Binding
- Mỗi yêu cầu kỹ thuật trong blueprint phải có ít nhất một test case tương ứng.
- CI kiểm tra sự tồn tại của blueprint và sự tương ứng (qua mapping hoặc quy ước đặt tên test).

### Guard 3.2 — Billing Atomic (không double-consume)
- SQLite transaction:
  ```sql
  UPDATE cards SET status='used' WHERE id=? AND status='available'
  ```
- Kiểm tra `affected_rows == 1`. Nếu không, từ chối thao tác và ghi log lỗi.

### Guard 3.3 — Watchdog Lifecycle
- Khi kill worker: đóng trình duyệt (kill browser process), xóa profile tạm, giải phóng tài nguyên.
- Ngăn rò rỉ bộ nhớ và zombie process.

### Guard 3.4 — PR Scope Limiter
- Mỗi PR: Tối đa 200 dòng thay đổi (không tính file test). Chỉ ảnh hưởng một module (kiểm tra qua file path).
- CI từ chối PR vượt giới hạn.

### Guard 3.5 — Traceability Logging
- Log định dạng bắt buộc:
  ```
  timestamp | worker_id | trace_id | state | action | status
  ```
- Đủ để debug và tái hiện luồng.

### Guard 3.6 — CDP Network Listener
- Module CDP phải sử dụng `Network.responseReceived` để chờ API tính tiền trước khi điền thông tin thanh toán.
- Staging validation (Phase 4) bắt buộc kiểm tra — nếu không có, coi như không pass.

### Guard 3.7 — Staging Safety Guard
- Dữ liệu staging: riêng biệt, không liên quan đến production.
- Có kill-switch toàn cục (một nút hoặc lệnh) để dừng ngay lập tức toàn bộ worker.

### Guard 3.8 — Rollback Trigger
- Tự động rollback về mức worker trước nếu bất kỳ trigger nào kích hoạt (cài đặt qua PM2 hoặc script giám sát).

### Guard 3.9 — Security Pipeline (4 cổng bắt buộc)
Mọi PR phải vượt qua **tất cả 4 cổng** trước khi được phép Merge:

| # | Cổng | Tiêu chí Pass | Hành động khi Fail |
|---|------|---------------|---------------------|
| 1 | **CodeQL Analysis** | Không có alert High/Critical | Copilot Autofix đề xuất sửa → Agent fix → push lại |
| 2 | **Dependency Review** | Dependabot không phát hiện vulnerability High+ chưa xử lý | Tạo Dependabot PR riêng, merge trước khi tiếp tục |
| 3 | **Secret Scanning** | Không có secret bị rò rỉ. Push Protection chặn trước khi push | Revoke secret → rotate → commit lại không chứa secret |
| 4 | **Copilot Autofix** | Mọi suggestion đã được review (accept/dismiss có lý do) | Human hoặc Agent review từng suggestion trước khi merge |

### Guard 3.10 — Exception Framework & Change Classification (Chống System Freeze)
Khi CI quá cứng nhắc gây nghẽn các thay đổi hợp lệ, sử dụng `CHANGE_CLASS` env var:

| Change Class | Bypass Line Limit | Bypass Module Limit | Use Case |
|-------------|-------------------|--------------------|----|
| `emergency_override` | ✅ | ✅ | Hotfix production, security patch khẩn cấp |
| `spec_sync` | ❌ | ✅ | Đồng bộ code với spec mới sau khi Architect thay đổi interface |
| `infra_change` | ✅ | ❌ | Thay đổi CI scripts, cấu hình infrastructure |

**Governance Enforcement (CI `check_pr_scope`):**
- `CHANGE_CLASS` là **REQUIRED** cho mọi PR. Nếu thiếu → CI **FAIL**.
- CI workflow auto-detect từ PR title: `[emergency]` → `emergency_override`, `[spec-sync]` → `spec_sync`, `[infra]` → `infra_change`, mặc định → `normal`.
- Non-normal CHANGE_CLASS yêu cầu: PR label `approved-override` HOẶC `CHANGE_CLASS_APPROVED=true`.
- `emergency_override` bổ sung yêu cầu: ít nhất 1 APPROVED review + PR title chứa `[emergency]`.
- `spec_sync` yêu cầu: changed files phải bao gồm `spec/`.
- `infra_change` yêu cầu: changed files phải bao gồm `ci/` hoặc `.github/`.
- `ALLOW_MULTI_MODULE` đã bị **LOẠI BỎ HOÀN TOÀN** — không còn được nhận diện.

### Guard 3.11 — Spec Versioning (Kiểm soát phiên bản đặc tả)
- Mỗi file spec chứa header `spec-version: MAJOR.MINOR`
- MAJOR bump = breaking change → CI fail → cần `CHANGE_CLASS=spec_sync`
- MINOR bump = additive → CI phát hiện stub thiếu → Agent tự implement
- CI `check_version_consistency` kiểm tra tính nhất quán giữa file headers và VERSIONING.md
- Chi tiết: [spec/VERSIONING.md](../../spec/VERSIONING.md)

### Guard 3.12 — Contract Segmentation (Tách biệt hợp đồng)
- `spec/core/interface.md` — FSM (core state machine)
- `spec/integration/interface.md` — Watchdog, Billing, CDP (integration)
- `spec/interface.md` — Bản tổng hợp tương thích ngược
- CI `check_signature` đọc cả segmented và fallback files
- **CI `check_spec_consistency` đảm bảo aggregated file KHÔNG lệch khỏi segmented files**
- **Divergence Guard:** CI tự động so sánh function list giữa segmented và aggregated files. WARNING nếu phát hiện lệch.

---

## 4. AI Workforce Control (Native Pipeline)

### 4.1 — Tổ hợp Model

| Vai trò | Model | Kích hoạt | Đầu ra |
|---------|-------|-----------|--------|
| **Architect** | Claude Opus 4.6 | Comment `@github-copilot` trên Issue/PR (Web) | Spec, Interface Contract, Quyết định kỹ thuật |
| **Developer** | GPT-5.2-Codex | Assign Issue cho Copilot (Coding Agent) | Code, Unit test, PR |
| **Reviewer** | GPT-5.4 | Tự động qua PR Ruleset | APPROVED hoặc REQUEST_CHANGES |
| **Cross-Inspector** | Gemini 3.1 Pro | Kích hoạt thủ công (Circuit Breaker / khó độ cao) | Phân xử độc lập, mã code chốt hạ |

### 4.2 — Sơ đồ luồng Native AI

```
[Human] ─── Tạo Issue, mô tả Task ───────────────────────────────┐
   │                                                             │
   ▼                                                             │
[Architect — Opus 4.6] ◄── đọc repo từ Copilot Memory Index      │
   │  Phân tích yêu cầu, viết Spec vào Issue comment             │
   │                                                             │
   ▼                                                             │
[Human] ─── Assign Issue cho Copilot ────────────────────────────┐│
   │                                                             ││
   ▼                                                             ││
[Developer — Codex 5.2] (Copilot Coding Agent)                   ││
   │  Tự đọc repo + Issue Spec qua GitHub API                    ││
   │  Tạo branch → Sinh code + test → Push PR                    ││
   │                                                             ││
   ▼                                                             ││
[CI Pipeline] ◄── GitHub Actions tự chạy                         ││
   │  ├── check_import_scope                                     ││
   │  ├── check_signature                                        ││
   │  ├── check_pr_scope                                         ││
   │  ├── check_spec_lock                                        ││
   │  └── Unit tests                                             ││
   │                                                             ││
   ▼                                                             ││
[Security Gates — Guard 3.9]                                     ││
   │  ├── CodeQL (no high/critical)                              ││
   │  ├── Dependabot (no high+ unaddressed)                      ││
   │  ├── Secret Scanning + Push Protection                      ││
   │  └── Copilot Autofix (suggestions reviewed)                 ││
   │                                                             ││
   ▼                                                             ││
[Reviewer — GPT-5.4] ◄── Tự động qua PR Ruleset                  ││
   │                                                             ││
   ├── APPROVED ──► [Human] ──► Merge ──► ✅ Done               ││
   │                                                             ││
   └── REQUEST_CHANGES ──► Auto-Fix Loop                         ││
          │                                                      ││
          ├── Lần 1-2: Agent tự đọc review → fix → push lại      ││
          │                                                      ││
          └── Lần ≥3: ⚡ Circuit Breaker                         ││
                 │                                               ││
                 └── [Cross-Inspector — Gemini 3.1 Pro]          ││
                        Phân xử độc lập → mã code chốt hạ        ││
```

### 4.3 — Nguyên tắc làm việc

1. **Không AI nào tự ý thay đổi spec** — Spec do Architect (Opus) định nghĩa, được lock bởi `check_spec_lock`.
2. **Task đơn nhất** — Mỗi Issue mô tả đúng 1 function/feature. Format chuẩn:
   ```
   Function: <tên>
   Input: <format>
   Output: <format>
   Constraints: <điều kiện>
   Forbidden: <không được làm>
   ```
3. **Code chỉ viết đúng task** — Không thêm logic ngoài phạm vi Issue.
4. **Review bắt buộc trước merge** — GPT-5.4 kiểm tra PR, nếu không pass thì REQUEST_CHANGES.
5. **Zero Human Copy-Paste** — Human không copy output AI giữa các công cụ. Mọi thông tin di chuyển qua Issue/PR native.

---

## 5. GitHub Enforcement

### 5.1 — Branch Protection
- `main`: Chỉ nhận PR từ `develop`, phải có CI pass + ít nhất 1 approve từ Reviewer.
- `develop`: Cấm push trực tiếp, chỉ nhận PR từ feature branch.

### 5.2 — PR Rulesets (Copilot Business)
- **Require PR:** Mọi thay đổi phải qua PR, không push trực tiếp.
- **Require Copilot Review:** GPT-5.4 tự động review mọi PR.
- **Admin Always Bypass:** Admin có quyền bypass khi cần xử lý khẩn cấp.

### 5.3 — CI bắt buộc (GitHub Actions)
| Check | Mô tả |
|-------|-------|
| `check_import_scope` | Đảm bảo không module nào import từ module khác |
| `check_signature` | So sánh function signature trong code với spec (multi-file aware, cross-file duplicate detection) |
| `check_pr_scope` | Kiểm tra scope PR: ≤200 dòng, ≤1 module, governance enforcement cho CHANGE_CLASS |
| `check_spec_lock` | Đảm bảo không PR nào sửa file trong `/spec/` (trừ Architect) |
| `check_spec_consistency` | Đảm bảo `spec/interface.md` (aggregated) không lệch khỏi segmented files |
| `check_version_consistency` | Validate `spec-version` headers nhất quán với VERSIONING.md |
| Unit tests | `python -m unittest discover tests` |

### 5.4 — Security Automation (Copilot Business)
| Tính năng | Trạng thái | Mô tả |
|-----------|-----------|-------|
| CodeQL Code Scanning | ✅ Enabled | Phát hiện vulnerability trong code |
| Code Quality Scanning | ✅ Enabled | Phát hiện code smell và anti-pattern |
| Copilot Autofix | ✅ Enabled | Tự đề xuất fix cho CodeQL alerts |
| Dependabot Security | ✅ Enabled | Alert khi dependency có CVE |
| Dependabot Version | ✅ Enabled | Tự tạo PR cập nhật dependency |
| Dependabot Grouped | ✅ Enabled | Gom các update cùng ecosystem |
| Secret Scanning | ✅ Enabled | Phát hiện secret bị commit |
| Push Protection | ✅ Enabled | Chặn push chứa secret |
| Validity Checks | ✅ Enabled | Kiểm tra secret còn active không |
| Non-provider Patterns | ✅ Enabled | Phát hiện pattern secret không chuẩn |

---

## 6. Xử lý Ngoại lệ (Exception Handling)

### 6.1 — CI Failure Recovery
```
CI Fail
  │
  ├── Lỗi code (test fail, lint fail, signature mismatch)
  │     └── Coding Agent tự đọc log → fix → push lại
  │           └── Nếu fail ≥2 lần cùng lỗi: Human comment hướng dẫn cụ thể
  │
  ├── Lỗi security gate (CodeQL alert, secret leak)
  │     └── Xử lý theo Guard 3.9 — KHÔNG bypass bằng force-merge
  │
  ├── Lỗi infrastructure (flaky test, runner timeout)
  │     └── Human re-run workflow thủ công
  │
  └── Lỗi spec mismatch (check_signature, check_spec_lock fail)
        └── Dừng lại. Architect review Spec → cập nhật nếu cần → Agent retry
```

### 6.2 — Review Rejection Recovery
```
PR bị REQUEST_CHANGES
  │
  ├── Lần 1-2: Auto-Fix Loop
  │     └── Coding Agent đọc review comments → fix → push
  │
  ├── Lần 3: ⚡ Circuit Breaker kích hoạt
  │     └── Human triệu hồi Gemini 3.1 Pro
  │           ├── Gemini đọc toàn bộ PR conversation
  │           ├── Phân tích root cause
  │           └── Đề xuất mã code chốt hạ hoặc thay đổi Spec
  │
  └── Sau Gemini phân xử:
        ├── Nếu lỗi do Spec: Architect cập nhật Spec → Agent retry từ đầu
        └── Nếu lỗi do code: Human commit mã Gemini đề xuất → Reviewer re-review
```

### 6.3 — Deadlock Prevention
- Nếu sau Circuit Breaker mà PR vẫn không pass: Human có quyền **close PR** và tạo Issue mới với scope nhỏ hơn.
- Admin có quyền **Always Bypass** trong PR Ruleset cho trường hợp khẩn cấp (hotfix production).
- Mọi bypass phải được ghi log lý do trong PR comment.

---

## 7. Tổng kết Milestones

| Phase | Milestone |
|-------|-----------|
| P1 | Spec lock, CI skeleton + Security Gates sẵn sàng |
| P2 | CI bắt được lỗi import, signature, PR scope. Security pipeline chặn vulnerability |
| P3 | 4 module hoàn chỉnh, unit test pass, smoke test pass |
| P4 | 3 workers staging 24h đạt chỉ số ổn định |
| P5 | 10 workers production 24h ổn định |
| P6 | Runbook hoàn chỉnh, sẵn sàng bàn giao |
| P7 | System audit-consistent, production-hardened, zero confirmed remaining issues |
| P8 | Production monitoring active, deployment verified, baseline recorded, extension spec defined |
| P9 | System auto-scales based on runtime metrics, behavior engine operational, all decision paths tested |
| P10 | Behavior layer designed, Blueprint-safe, Phase 9 aligned, ready for implementation |

---

## 8. Changelog

| Version | Ngày | Thay đổi |
|---------|------|----------|
| 1.0 | 2025-Q1 | Phiên bản gốc — 6 AI roles, dùng DeepSeek + copy-paste thủ công |
| 2.0 | 2026-04-01 | **Native AI Workflow** — Loại bỏ hoàn toàn DeepSeek, System Coordinator GPT, và mọi quy trình copy-paste. Chuyển sang 3 tầng bản địa (Human → Architect/Reviewer → Coding Agent). Thêm Security Pipeline (Guard 3.9), Circuit Breaker (Rule 3), CI Failure Recovery. Chuẩn hóa tên file bỏ Unicode en-dash. |
| 2.1 | 2026-04-04 | **Phase 8 — Production Deployment & Monitoring.** Thêm Phase 8 vào workflow. Định nghĩa `get_deployment_status()`, extension spec cho future upgrades. |
| 2.2 | 2026-04-04 | **Spec Reconstruction — Phase 7 & Phase 8.** Tái dựng Phase 7 (Post-Finalization Audit Validation) từ lịch sử PR #112–#138. Mở rộng Phase 8 thành full spec từ lịch sử PR #142–#150. Thêm P7 vào milestones table. Đồng bộ spec với system đã triển khai (CHANGE_CLASS=spec_sync). |
| 2.3 | 2026-04-04 | **Phase 9 — Behavior & Scaling Intelligence.** Bổ sung Phase 9 từ lịch sử PR #160 (Issue #155 Task 1: Behavior Decision Engine, Issue #159 Task 2: Scaling Execution Layer). Thêm P9 vào milestones table. Đồng bộ spec với system đã triển khai (CHANGE_CLASS=spec_sync). |
| 2.4 | 2026-04-04 | **Phase 10 — Behavior Layer (Blueprint-safe).** Bổ sung Phase 10 spec: CRITICAL_SECTION awareness, SAFE POINT/SAFE ZONE rule, FSM context (BehaviorState), NO-DELAY zone, bounded UI delay, non-interference rule, Phase 9 alignment. Thêm P10 vào milestones table. Đồng bộ spec với Blueprint (CHANGE_CLASS=spec_sync). |
