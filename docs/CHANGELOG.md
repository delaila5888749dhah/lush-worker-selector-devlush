# Changelog

All notable changes to `lush-givex-worker` are recorded here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

---

## [Unreleased]

---

## [Phase 7 — Observability Extensions] — 2026-04-12

### Added
- **Ext-1** Metrics Export (`modules/observability/metrics_exporter.py`) — PR #246
  - `export_metrics(metrics: dict)` — structured JSON log at DEBUG level
  - Custom backend registration via `register_exporter` / `unregister_exporter`
  - Thread-safe, fail-safe, integrated into `integration.runtime._runtime_loop`
- **Ext-3** Health Check Endpoint (`modules/observability/healthcheck.py`) — PR #247
  - `GET /health` HTTP endpoint via `ThreadingHTTPServer`
  - Returns `{status, running, state, worker_count, consecutive_rollbacks, errors}`
  - Degraded detection: error_rate > 5%, rollbacks > 0, running == False
- **Ext-2** Alerting Rules (`modules/observability/alerting.py`) — PR #248
  - `evaluate_alerts(metrics)` — threshold evaluation: error_rate > 5%, restarts > 3/hr, success_rate drop > 10%
  - `send_alert(message)` — dispatches to registered handlers + default WARNING log
  - 22 unit tests in `tests/test_alerting.py`
- **Ext-4** Structured Log Aggregation (`modules/observability/log_sink.py`) — PR #249
  - `emit(event: dict)` — structured JSON log alongside existing pipe-delimited format
  - Schema: `{ts, source, level, event, data}`
  - 13 unit tests in `tests/test_log_sink.py`
- **spec-sync v5.1** (`spec/integration/interface.md`) — PR #250
  - Added Ext-1 and Ext-3 interface contracts
- **spec-sync v5.2** (`spec/integration/interface.md`) — PR #251
  - Added Ext-2 and Ext-4 interface contracts
  - Fixed `ci/check_signature.py` duplicate detection for bullet-list module sections
  - Stabilized runtime timeout CI test

---

## [Phase 5–6 — Production Rollout & Operations] — 2026-04-12

### Added
- **Phase 6 Handover & Operations** — PR #245
  - `docs/operations/RUNBOOK.md` — production runbook
  - Cron scripts for automated maintenance
  - 15 operational tests
- **Phase 5 Rollout Scheduler** — PR #244
  - `integration/rollout_scheduler.py` — automatic rollout scheduler (5→10 workers)
  - Gradual scale-up with rollback trigger config

---

## [Phase 4 — Staging Validation] — 2026-04-12

### Added
- **Phase 4 Staging** — PR #243
  - `docs/staging/PHASE4_CHECKLIST.md` — validation checklist
  - `docs/staging/PHASE4_REPORT_TEMPLATE.md` — report template
  - `docs/staging/RUNBOOK_STAGING.md` — staging runbook
  - Smoke tests for integration validation

---

## [Phase 10 — Timing Hardening & Test Stabilization] — 2026-04-12

### Fixed
- **wrapper.py multi-step delay** — PR #241
  - `inject_step_delay()` helper with dual typing+thinking delay injection
- **Temporal model test hardening** — PR #242 (`spec_sync`)
  - Eliminated 25 flaky clock-dependent tests in `test_temporal_model.py`
  - Added 25 deterministic seed-based replacements

---

## [CDP Audit — GAP Closures] — 2026-04-10 / 2026-04-12

### Fixed
- **GAP-CDP-01**: PID tracking, `_sanitize_error()`, `force_kill()` — PR #238
- **CDP audit fixes**: drop spec file, unused imports, empty excepts — PR #239
- **spec-sync**: Close GAP-CDP-01, add INV-CDP-01 and INV-CDP-02 to audit-lock — PR #240
- **MED-01**: Replace per-call `ThreadPoolExecutor` with shared `_cdp_executor` — PR #224
- **LOW**: `MAX_BILLING_PROFILES` memory guard in `billing/_read_profiles_from_disk` — PR #223
- **LOW**: Expand single-line function defs to PEP-8 multi-line in `runtime.py` — PR #222
- **LOW**: Native Python 3.10+ type syntax in `common/types.py` — PR #221

---

## [Chaos Engineering & Thread Safety] — 2026-04-10

### Added
- Chaos engineering stress tests for Core Engine thread-safety audit — PR #231
- `LateCallbackInjector` + vbv_3ds FSM path + exception subclass injection — PR #233
- Phase 2 FakeAsyncDriver with per-session Timer A/B async callback coverage — PR #236

### Fixed
- `fix(chaos)`: correct docstring contradictions and vbv_3ds success_count logic — PR #234
- `fix`: opt-in Node.js 24 for all GitHub Actions workflows — PR #232
- `fix`: atomic initialization in FSM `initialize_for_worker` to eliminate TOCTOU race — PR #226
- Replace fixed `time.sleep` synchronization with event-based polling in tests — PR #228

### Changed
- `behavior`: add type hints and finite-float contract note to `evaluate()` — PR #230
- `chore`: pin dependencies to exact versions, add `requirements-lock.txt` — PR #227
- `ci`: remove `environment: production` from CI check job — PR #229

---

## [Core Infrastructure — SPEC-6] — 2026-03-26 to 2026-04-09

### Added
- FSM `add_new_state` implementation with tests — PR #5
- `check_import_scope` CI rule — PR #6
- `check_signature` CI rule — PR #10
- `check_pr_scope` CI rule — PR #25
- `check_spec_lock` CI rule — PR #27
- Initial spec and workflow documentation — PRs #22, #23, #24

---
