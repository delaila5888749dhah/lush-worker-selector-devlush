<!-- lint disable no-shortcut-reference-link no-undefined-references -->
# Changelog

All notable changes to `lush-givex-worker` are recorded here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Unreleased]
### Fixed (BitBrowser v144+/v146 compatibility — `launch_profile` response format)
- **Bug:** BitBrowser v144 and v146 changed the `/api/v1/browser/open`
  (`launch_profile`) response format: the `webdriver` field is no longer
  present.  The bot raised `RuntimeError: BitBrowser launch_profile response
  missing webdriver` on every cycle, making the worker completely unusable on
  any machine running current upstream BitBrowser releases.
- **Fix:** `BitBrowserSession.__enter__` now falls back to deriving the
  Selenium Remote WebDriver URL from the `http` field (e.g.
  `"127.0.0.1:64663"` → `"http://127.0.0.1:64663"`) when `webdriver` is
  absent.  The legacy `webdriver` field is still preferred when both are
  present — fully backward compatible.
- **No operator action required:** no env-var changes or migration steps are
  needed.  Both old (`webdriver`) and new (`http`) response formats are
  supported automatically.

### Documented (Blueprint §14.5 — Autoscaler error-rate path)
- `autoscaler._evaluate_scale_down(error_rate)` clarified as a two-mode API:
  the per-worker failure sub-path (default `error_rate=0.0`) is production-
  wired via `get_recommended_scale_down_target()`, while the global
  `error_rate > ERROR_RATE_THRESHOLD` sub-path is **external-only / opt-in**
  and intentionally not invoked by any automatic production loop. Production
  global-error-rate response remains owned by the behavior path under the
  `_is_safe_locked` safety gate (Blueprint §14.1).
- Blueprint §14.5 added; `_evaluate_scale_down` docstring tightened to match.
- `tests/test_autoscaler.py` adds explicit coverage for the manual external-
  trigger contract and asserts the runtime loop never feeds a non-zero
  `error_rate` into this API.

### Added (Blueprint §2.1 — BitBrowser Profile Pool)
- `BITBROWSER_POOL_MODE` env var — bật chế độ pool profile có sẵn,
  thay create/delete flow (tránh Operation Password prompt của BitBrowser).
- `BITBROWSER_PROFILE_IDS` env var — CSV profile IDs (VD:
  `abc123,def456,ghi789`). Bắt buộc khi `BITBROWSER_POOL_MODE=1`.
- `BitBrowserPoolClient` trong `modules/cdp/fingerprint.py` — round-robin
  cursor thread-safe (threading.Lock) + BUSY set, eviction khi API trả 404.
- Blueprint §2.1 mô tả đầy đủ cơ chế + Sync Matrix §11.
- `tests/test_bitbrowser_pool.py` — 10 unit tests cho round-robin,
  wrap-around, BUSY skip, timeout, thread-safety, 404 eviction, payload
  chính xác, và backward compatibility.

### Changed (Blueprint §2.1)
- None (backward-compatible: `BITBROWSER_POOL_MODE=0` mặc định giữ nguyên
  hành vi create/delete cũ).

### Added (P2-5, canary / final gate)
- `docs/rollback.md` — rollback plan documenting every feature-flag
  kill-switch (`ENABLE_RETRY_LOOP=0`, `ENABLE_RETRY_UI_LOCK=0`,
  `ENABLE_CLEAR_REFILL_AFTER_POPUP=0`, `ENABLE_PRODUCTION_TASK_FN`),
  verification steps, and re-enablement criteria.
- `docs/canary_rollout.md` — 5-step canary runbook (smoke → mini →
  soak → multi → full) with per-step PASS criteria, 24 h observation
  window, abort criteria, and monitoring-dashboard specification
  (`success_rate` / `swap_rate` / `cdp_timeout_rate`).
- `docs/operations/RUNBOOK.md` §11 cross-links the two docs above.

### Fixed (P0-5, #113)
- `orchestrator.refill_after_vbv_reload` now executes the complete purchase
  sequence — preflight → navigate → eGift → cart → guest → payment — when
  `ctx.task` is set (i.e. the VBV cancel caused a full page reload).
  Previously only `fill_billing` + `fill_card_fields` were called, leaving
  the preflight, navigation, eGift-form, cart, and guest-checkout steps
  unexecuted, causing the flow to fail at payment submission.
  The legacy partial-refill path (`ctx.task is None`) is unchanged.
  Each step is logged at INFO level for journey tracing.

### Fixed (P0-6, #114)
- `orchestrator.run_cycle` no longer calls `mark_completed` for non-success outcomes.
  Previously, declined/retry/abort_cycle tasks were falsely recorded as completed,
  permanently blocking retry on subsequent cycles due to idempotency duplicate check.
  **Migration:** If `.idempotency_store.json` exists from a previous dev run,
  delete it to clear bogus completed entries. No production impact (production has
  not started yet).

## [Test Hardening + Rollout Scheduler Deprecation] — 2026-04-18
### Added
- 4 error-branch tests for `scripts/download_maxmind.py` (checksum mismatch, archive without `.mmdb`, urlopen `OSError`, empty checksum parse).
- 5 edge-case tests for `scripts/seed_billing_pool.py` (missing input, UTF-8 BOM, all rows skipped, quoted comma field, safe overwrite).
- 4 edge-case tests for `ProxyPool.load_from_file` + `PROXY_LIST_FILE` env-var init.
- 2 CLI subprocess smoke tests for `backup_billing_pool.py` / `cleanup_browser_profiles.py`.
- `tests/smoke/test_real_bitbrowser_smoke.py` — real BitBrowser smoke harness (gated behind `BITBROWSER_API_KEY`).
- `.github/workflows/smoke-real.yml` — manual-dispatch workflow for the real BitBrowser smoke harness.
- `pytest.ini` registering the `real_browser` marker and filtering it out of the default run.
### Changed
- `integration/rollout_scheduler.py` — legacy scheduler internals retained in-place (dormant via `ROLLOUT_MANAGED_BY_RUNTIME=true`); every public call now emits `DeprecationWarning` via a new `_warn_deprecated` helper. Full removal of the legacy loop/internals is deferred to a follow-up `[infra]` PR.
- `tests/test_rollout_scheduler.py` — legacy loop/stability/lifecycle tests restored alongside new `TestDeprecationSignalling` class (5 tests); `reset()` anti-pattern (`assertIsNone`) fixed.
- RUNBOOK / HANDOVER / AUDIT_SCORECARD updated to reflect `rollout_scheduler` deprecation.
### Fixed
- `tests/test_e2e_integration.py` no longer raises a collection error under `python -m unittest discover` — a skip-guard detects the unittest runner and raises `unittest.SkipTest` cleanly.


## [Phase 11] — 2026-04-12

### Added
- `inject_card_entry_delays(bio, stop_event=None)` in `modules/delay/wrapper.py` — exposes a helper for applying `BiometricProfile` Layer 2 per-keystroke timing during card entry simulation (16 delays per card entry, with inter-group pauses at indices 3/7/11).
- `inject_card_entry_delays` exported from `modules/delay/main.py` for integration by callers.
### Changed
- `modules/delay/biometrics.py` docstring updated to describe Phase 11 helper/export availability rather than completed production-path wiring.
- NOTE: spec/audit-lock.md invariants (INV-BIO-01, INV-BIO-02, INV-BIO-03) will be
  added in a follow-up spec-sync PR after this PR is merged.
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
