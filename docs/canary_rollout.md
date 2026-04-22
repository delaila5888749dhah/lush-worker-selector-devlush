<!-- lint disable no-shortcut-reference-link no-undefined-references -->
# Canary Rollout Runbook — lush-givex-worker (P2-5)

This is the **final gate** before enabling the bot against the real
`orderQueue` with live Telegram / BitBrowser / Givex APIs. Every canary
step below must PASS — and be observed for **24 h** — before the next
step is attempted. A FAIL at any step triggers the procedure in
`docs/rollback.md`.

> Prerequisites: P0-1..P0-6, P1-1..P1-5, P2-1..P2-4 merged; the 14 E2E
> tests added in P2-4 green in CI against mocks.

---

## 0. Prerequisite issue traceability

P2-5 is the **final gate** — it depends on every preceding issue being
closed. The table below maps each prerequisite to its delivery status so
an operator can confirm the chain is complete before starting Step 1.

| Issue | Title (short) | Category | Status |
|---|---|---|---|
| #108 | Master tracking issue | meta | Tracking — closed when all children close |
| #109 | P0-1 — FSM state machine + preflight | code | Implemented earlier (merged) |
| #110 | P0-2 — Payment retry loop | code | Implemented earlier (merged) |
| #111 | P0-3 — Card-swap on decline | code | Implemented earlier (merged) |
| #112 | P0-4 — UI-lock recovery | code | Implemented earlier (merged) |
| #113 | P0-5 — VBV/3DS refill after reload | code | Implemented earlier (merged) |
| #114 | P0-6 — Idempotency store fix | code | Implemented earlier (merged) |
| #115 | P1-1 — Popup text-match selector | code | Implemented earlier (merged) |
| #116 | P1-2 — Clear/refill after popup | code | Implemented earlier (merged) |
| #117 | P1-3 — Thank-you popup shadow DOM | code | Implemented earlier (merged) |
| #118 | P1-4 — Structured logging + trace | code | Implemented earlier (merged) |
| #119 | P1-5 — Worker task abort | code | Implemented earlier (merged) |
| #120 | P2-1 — Production logging / Telegram | code | Implemented earlier (merged) |
| #121 | P2-2 — Givex URL env override | code | Implemented earlier (merged) |
| #122 | P2-3 — Preflight geo-check | code | Implemented earlier (merged) |
| #123 | P2-4 — 14 E2E integration tests | code | Implemented earlier (merged) |
| #124 | **P2-5 — Canary deploy (this doc)** | docs + ops | **Documented here**; operator execution pending |

> **Scope of this PR:** documentation deliverables only (`docs/rollback.md`,
> `docs/canary_rollout.md`, RUNBOOK cross-links, CHANGELOG). This PR does
> **not** claim that the operator-executed acceptance criteria (mini-canary
> $5 transaction, soak 5 cards, 3-worker parallel, full rollout) have been
> completed — those are tracked in §5 "Operator checklist" below.

## 1. The five canary steps

Each step reuses the same production binary; only the scope of traffic
(`WORKER_COUNT`, billing-pool size, Givex task feed) changes between
steps.

### Step 1 — Smoke test (1 worker, 1 TEST Visa)

- Setup
  - `WORKER_COUNT=1`
  - Single TEST card `4111 1111 1111 1111` in `billing_pool/`
  - `GIVEX_EGIFT_URL` / `GIVEX_PAYMENT_URL` → **staging** sandbox URLs
  - `ENABLE_PRODUCTION_TASK_FN=1`
- Run: a single purchase cycle via `python -m app`.
- PASS criteria
  - No uncaught exception in `worker.log`.
  - Full journey trace present:
    `preflight → navigate → egift → cart → guest → payment → popup`.
  - Trace line format matches `timestamp | worker_id | trace_id | state | action | status`.
  - `modules/monitor/main.py::get_metrics()` reports the cycle exactly
    once (no duplicate idempotency writes).

### Step 2 — Mini-canary (1 worker, 1 real card, $5 order)

- Setup: same as Step 1, but
  - 1 **real** card in `billing_pool/`.
  - 1 tiny real order ($5) fed into `orderQueue`.
  - `GIVEX_EGIFT_URL` / `GIVEX_PAYMENT_URL` → **production** URLs.
- Run: one cycle end-to-end.
- PASS criteria
  - Givex records the transaction with the expected amount.
  - Telegram receives a **blurred** PNG screenshot (the blur filter in
    `modules/notification/screenshot_blur.py` is applied — no raw PAN,
    CVV, or expiry pixels visible).
  - `grep -E '(4[0-9]{3}[- ]?[0-9]{4}[- ]?[0-9]{4}[- ]?[0-9]{4}|cvv=)' worker.log`
    returns **zero** matches.
  - `.idempotency_store.json` contains exactly one `completed` entry
    for the task_id.

### Step 3 — Single-worker soak (1 worker, 5 cards)

- Setup: same as Step 2 but 5 cards chained in the billing pool; some
  of them expected to decline (e.g. low-balance gift cards).
- Run: 5 consecutive cycles.
- PASS criteria
  - ≥1 decline → automatic swap to next card via the P0-2 retry loop.
  - ≥1 success.
  - Per-`trace_id` `swap_count` ≤ 2 everywhere in the log
    (`grep 'swap_count=' worker.log | sort -t= -k2 -n | tail`).
  - **No double-charge**: for every `task_id` there is at most one
    `completed` record and at most one Givex transaction of the
    expected amount. Reconcile `.idempotency_store.json` against
    Givex admin export before proceeding.

### Step 4 — Multi-worker (3 workers parallel)

- Setup: `WORKER_COUNT=3`, 3 cards per worker.
- Run: 3 parallel cycles.
- PASS criteria
  - No `task_id` assigned to more than one worker at a time
    (`_in_flight_task_ids` invariant; no log line matching
    `duplicate.*task_id`).
  - No BitBrowser profile corruption (each worker owns a distinct
    profile directory).
  - No race condition in the billing pool: the same card is never
    claimed by two workers in the same minute.
  - Metrics aggregate cleanly: `success_rate`, `swap_rate`, and
    `cdp_timeout_rate` remain within their baselines (see §2).

### Step 5 — Full production

Only after Steps 1–4 have each been observed for 24 h with PASS:

- Setup: `WORKER_COUNT` at its configured production value; full
  `orderQueue` enabled.
- PASS criteria
  - `success_rate` ≥ baseline captured in Step 3.
  - No rollback trigger from `docs/rollback.md` §1 fires for 24 h.

---

## 2. Monitoring dashboard

The monitoring dashboard must surface these three rates per worker and
in aggregate. Each is derived from counters that already exist in the
codebase; wire them into your preferred metrics backend (Prometheus /
Datadog / Grafana — this repo is backend-agnostic).

| Metric | Source | Target (canary PASS) |
|---|---|---|
| `success_rate` | `modules/monitor/main.py::get_metrics()["success_rate"]` (wraps `get_success_rate()` = `success_count / (success_count + error_count)`) | ≥ `baseline_success_rate` − 5 pp |
| `swap_rate` | Count of `swap_count=` log events ÷ total cycles, per 5-min window | ≤ 0.5 (i.e. at most one swap every two cycles on average) |
| `cdp_timeout_rate` | Count of `cdp.*timeout` log events ÷ total cycles, per 5-min window | ≤ 0.05 |

Additional gauges that must be visible on the dashboard:

- `baseline_success_rate` (pin the value captured at start of Step 3).
- `in_flight_task_ids` size (must stay < `WORKER_COUNT`).
- `completed_task_ids` size (monotonically non-decreasing within a
  retention window).
- Restart counter: `modules/monitor/main.py::get_metrics()["restarts_last_hour"]`.

## 3. Abort criteria (shared with `docs/rollback.md` §1)

Trigger rollback **immediately** on any of:

- Success-rate drop > 10 pp below baseline for 2 consecutive windows.
- Any `swap_count` > 2 for a single `trace_id`.
- Any PAN / CVV / Givex token visible in logs or in a Telegram PNG.
- `cdp_timeout_rate` > 5% for a 5-minute window.
- Any suspected double-charge.
- Any unhandled exception that escapes `run_cycle`.

## 4. Observation window

Between every canary step, observe the metrics above for a **24 h**
window with no new deploys. Only advance to the next step if every
PASS criterion in this doc **and** every "must not trigger" row in
`docs/rollback.md` §1 holds for the full window.

## 5. Operator checklist

Tick each item only after the previous step's 24 h observation has
elapsed without triggering any abort criterion:

- [ ] Step 1 — Smoke test PASS, 24 h observation complete.
- [ ] Step 2 — Mini-canary PASS, 24 h observation complete.
- [ ] Step 3 — Single-worker soak PASS, 24 h observation complete.
- [ ] Step 4 — Multi-worker PASS, 24 h observation complete.
- [ ] Step 5 — Full production enabled; `success_rate`, `swap_rate`,
  `cdp_timeout_rate` all within target for the first 24 h.
- [ ] P2-4 E2E suite (14 tests) executed against real staging APIs
  and all tests PASS.

## 6. Links

- Rollback procedure: `docs/rollback.md`
- Operator runbook (day-to-day): `docs/operations/RUNBOOK.md`
- Staging checklist: `docs/staging/PHASE4_CHECKLIST.md`
- Feature-flag defaults: `integration/orchestrator.py` (§"ENABLE_*"),
  `integration/runtime.py`.

---

## 7. `MAX_WORKER_COUNT` — rollout cap

Two environment variables control the worker pool during canary
rollout. They are distinct and must be kept in sync:

| Var                | Role                                 | Default |
|--------------------|--------------------------------------|---------|
| `WORKER_COUNT`     | Initial workers at boot              | 1       |
| `MAX_WORKER_COUNT` | Rollout cap (upper bound for scale)  | 10      |

Invariants enforced by `integration.runtime._validate_startup_config`:

- `WORKER_COUNT ≤ MAX_WORKER_COUNT`
- `1 ≤ MAX_WORKER_COUNT ≤ 50`

`SCALE_STEPS` is derived at import time by
`modules/rollout/main.py::_build_scale_steps(MAX_WORKER_COUNT)` — the
cap is always the last step and the rollout never exceeds it. See the
"Scaling the worker pool" section in `README.md` for the full table of
derived step tuples.

### Small canary (cap=2)

```sh
export WORKER_COUNT=1
export MAX_WORKER_COUNT=2
python -m integration.runtime
# SCALE_STEPS will be derived as (1, 2); rollout never exceeds 2 workers.
```

### Staging canary (cap=4)

```sh
export WORKER_COUNT=1
export MAX_WORKER_COUNT=4
# SCALE_STEPS = (1, 3, 4); rollout walks 1 → 3 → 4 and stops.
```

Pair these settings with the canary steps in §1: Step 1 / Step 2 run
at `WORKER_COUNT=1`, Step 4 bumps to `WORKER_COUNT=3` under
`MAX_WORKER_COUNT=4`, and Step 5 (full production) raises
`MAX_WORKER_COUNT` to its configured production value.
