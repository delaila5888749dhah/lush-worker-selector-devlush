# Phase 4 — Integration & Staging Validation Checklist

**Version:** 1.0
**SPEC-6 Reference:** Phase 4 (§64–§82)
**Status:** IN PROGRESS
**Target Milestone:** 3 workers staging 24h — all metrics within threshold

---

## Pre-Staging Readiness Gates

Các điều kiện bắt buộc phải PASS trước khi bắt đầu staging:

- [ ] Phase 10 LOCKED (PR #241, #242 merged to main)
- [ ] CI xanh trên main (all checks pass)
- [ ] Tất cả unit tests pass: `python -m unittest discover tests`
- [ ] behavior layer: inject_step_delay() hoạt động đúng (verified PR #241)
- [ ] temporal model: deterministic, no flaky tests (verified PR #242)

---

## Staging Checklist (SPEC-6 §Phase 4)

### CP-1 — Module Integration
- [ ] Tất cả modules load không error: fsm, billing, cdp, watchdog, behavior, delay
- [ ] Import isolation: không có cross-module import vi phạm
- [ ] interface.md: tất cả public functions đều có implementation

### CP-2 — Worker Initialization
- [ ] 1 worker khởi động thành công
- [ ] PersonaProfile seed assigned đúng (unique per worker)
- [ ] BehaviorWrapper active: inject_step_delay() gọi đúng thời điểm
- [ ] Stagger start: workers khởi động so le random.uniform(12, 25)s

### CP-3 — Form Fill Behavioral Simulation
- [ ] Typing delay: 0.6–1.8s per 4-digit group (Blueprint §4)
- [ ] Thinking hesitation: 3–5s sau khi điền xong trước COMPLETE PURCHASE (Blueprint §5)
- [ ] CRITICAL_SECTION: zero delay injected trong VBV/payment submit
- [ ] Accumulator: tổng delay ≤ 7.0s per step, watchdog headroom ≥ 3s

### CP-4 — FSM State Integrity
- [ ] FSM không bị kẹt state
- [ ] Tất cả transition hợp lệ
- [ ] IDLE → FILLING_FORM → PAYMENT → VBV → POST_ACTION flow đúng
- [ ] BehaviorStateMachine reset đúng sau mỗi cycle

### CP-5 — Billing Atomic (Guard 3.2)
- [ ] Không double-consume: SQLite UPDATE với affected_rows == 1 check
- [ ] Concurrent workers không consume cùng 1 billing record
- [ ] Idempotency key hoạt động đúng

### CP-6 — Watchdog
- [ ] Watchdog kill/restart worker đúng khi timeout
- [ ] Browser process bị kill khi worker die
- [ ] Không zombie process sau kill
- [ ] Worker restart count < 2 / 24h

### CP-7 — CDP Network Listener (Guard 3.6)
- [ ] Network.responseReceived listener active trước khi điền payment
- [ ] Đợi total amount API trả về trước khi proceed
- [ ] Timeout handling đúng (không hang)

### CP-8 — Scaling Behavior (Phase 9)
- [ ] BehaviorDecisionEngine evaluate() gọi đúng trong runtime loop
- [ ] SCALE_UP trigger khi success_rate ≥ 70%, error_rate ≤ 5%
- [ ] SCALE_DOWN trigger khi error_rate > 5% hoặc restarts > 3/hr
- [ ] Cooldown 30s giữa các quyết định
- [ ] Khi metrics unavailable → log "metrics_unavailable_scaling_deferred" và defer scaling (không silent)

### CP-11 — Runtime Lifecycle Safety (PR 13)
- [ ] stop_worker() CRITICAL_SECTION safe: không bị force-stop, chờ CS hoàn thành
- [ ] stop() bounded shutdown: 30% budget cho loop, 70% cho workers; stragglers được log
- [ ] reset() production guard: raise RuntimeError nếu RUNNING + behavior delay enabled
- [ ] _pending_restarts capped tại max(1, len(_workers)) khi worker fail
- [ ] proxy released khi Thread.start() fail trong start_worker()
- [ ] register_signal_handlers() không crash từ non-main thread; log debug khi skip

### CP-9 — Logging & Traceability (Guard 3.5)
- [ ] Log format đúng: timestamp | worker_id | trace_id | state | action | status
- [ ] trace_id unique per lifecycle
- [ ] Tất cả error paths được log
- [ ] log_sink.emit() failure được đếm (runtime._log_sink_error_count) và log WARNING

### CP-10 — Stability Metrics (24h run)
- [ ] success_rate ≥ 70%
- [ ] worker_restart_count < 2 trong 24h
- [ ] memory_usage < 1.5GB
- [ ] zero double-consume incidents
- [ ] error_rate ≤ 30%

---

## Kill-Switch Verification (Guard 3.7)
- [ ] Global kill-switch tested: tất cả workers dừng trong < 5s
- [ ] Staging data isolated: không ảnh hưởng production

---

## Sign-off
- [ ] Architect review: checklist đầy đủ
- [ ] All CP-1 → CP-10 PASS
- [ ] **P4 MILESTONE ACHIEVED**
