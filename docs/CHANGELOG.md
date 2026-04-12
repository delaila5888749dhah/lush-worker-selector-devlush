# Changelog

All notable changes to `lush-givex-worker` are recorded here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Phase 7 — Observability Extensions] — 2026-04-12

### Added
- **Ext-1** Metrics Export — PR #246: `export_metrics(metrics)`, custom backends, thread-safe
- **Ext-3** Health Check Endpoint — PR #247: `GET /health`, degraded detection
- **Ext-2** Alerting Rules — PR #248: threshold evaluation + alert dispatch (22 tests)
- **Ext-4** Log Aggregation — PR #249: structured JSON `emit(event)` (13 tests)
- **spec-sync v5.1** — PR #250: Ext-1 and Ext-3 interface contracts
- **spec-sync v5.2** — PR #251: Ext-2/Ext-4 contracts, `check_signature.py` fix

## [Phase 5–6 — Production Rollout & Operations] — 2026-04-12

### Added
- **Phase 6** — PR #245: `RUNBOOK.md`, cron scripts, 15 operational tests
- **Phase 5** — PR #244: `rollout_scheduler.py` (5→10 workers), rollback trigger

## [Phase 4 — Staging Validation] — 2026-04-12

### Added
- **Phase 4 Staging** — PR #243: checklist, report template, runbook, smoke tests

## [Phase 10 — Timing Hardening] — 2026-04-12

### Fixed
- **wrapper.py** — PR #241: `inject_step_delay()` dual typing+thinking delay
- **Temporal tests** — PR #242: replaced 25 flaky clock tests with deterministic seeds

## [CDP Audit — GAP Closures] — 2026-04-10 / 2026-04-12

### Fixed
- **GAP-CDP-01** — PR #238: PID tracking, `_sanitize_error()`, `force_kill()`
- **CDP audit** — PR #239: drop spec file, unused imports, empty excepts
- **spec-sync** — PR #240: close GAP-CDP-01, add INV-CDP-01/02
- **MED-01** — PR #224: shared `_cdp_executor`
- **LOW** — PRs #221–#223: type syntax, PEP-8 formatting, memory guard

## [Chaos Engineering & Thread Safety] — 2026-04-10

### Added
- Chaos stress tests — PR #231 | `LateCallbackInjector` — PR #233 | FakeAsyncDriver — PR #236

### Fixed
- Docstring fixes — PR #234 | Node.js 24 — PR #232 | FSM TOCTOU race — PR #226 | Event polling — PR #228

### Changed
- Type hints — PR #230 | Pin deps — PR #227 | CI env fix — PR #229

## [Core Infrastructure — SPEC-6] — 2026-03-26 to 2026-04-09

### Added
- FSM `add_new_state` — PR #5 | CI rules — PRs #6, #10, #25, #27 | Docs — PRs #22–#24
