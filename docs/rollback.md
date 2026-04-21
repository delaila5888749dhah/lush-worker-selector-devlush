<!-- lint disable no-shortcut-reference-link no-undefined-references -->
# Rollback Plan — lush-givex-worker

This document describes how to roll the bot back to its pre-P0/P1/P2 behaviour
(single-shot cycle, no retry loop, no FSM auto-transition, no UI-lock recovery,
no post-popup clear/refill) using **environment feature flags only** — no code
revert, no redeploy.

> See `docs/canary_rollout.md` for the 5-step canary gate that precedes any
> full rollout. Any failed canary step **must** trigger the procedure below.

---

## 1. Trigger conditions

Execute this rollback when any of the following is observed in a canary step
(see `docs/canary_rollout.md` §3 "Abort criteria") or in production:

- Success rate drops > 10% below the baseline captured in
  `monitor.get_baseline_success_rate()`.
- `swap_count` for any single `trace_id` exceeds **2** (bot is looping
  retries beyond the P0-2 cap).
- Telegram notification contains an un-blurred card number / CVV token
  (grep of `worker.log` hits a PAN/CVV pattern).
- CDP timeout rate > 5% over a 5-minute window.
- Any double-charge is suspected (same `task_id` completed twice, or the
  idempotency store shows a duplicate marked `completed`).
- Any unhandled exception escapes `run_cycle` with a traceback containing a
  live card number, CVV, Givex token, or BitBrowser credential.

## 2. Feature flags (all default ON after P0/P1/P2)

| Variable | Default | Disable with | Behaviour when disabled |
|---|---|---|---|
| `ENABLE_RETRY_LOOP` | `1` | `0` | `run_cycle` runs **one** iteration (pre-P0-2 behaviour); no automatic card swap on decline, no retry cap, no FSM re-entry. |
| `ENABLE_RETRY_UI_LOCK` | `1` | `0` | No automatic UI-lock recovery (pre-P0-4). Cycle aborts on focus-shift / UI-lock state. |
| `ENABLE_CLEAR_REFILL_AFTER_POPUP` | `1` | `0` | No clear-then-refill after the thank-you popup (pre-P1-2). Cycle completes at popup with whatever fields remain on the page. |
| `ENABLE_FSM_AUTO_TRANSITION` | *unset* | *unset* / `0` | **Reserved / forward-compat.** The issue statement lists this flag among the rollback levers for future FSM work; no orchestrator code currently reads it. Export `ENABLE_FSM_AUTO_TRANSITION=0` anyway when executing the rollback so the shutdown is consistent with the issue's rollback spec and so any later feature that adds the flag is safe by default. |
| `ENABLE_PRODUCTION_TASK_FN` | `""` (off) | `""` / unset | Runtime uses the **no-op stub** `task_fn` instead of `make_task_fn`. No real BitBrowser / Givex / Telegram calls are made. This is the hard kill-switch. |

> The flag names above are the canonical ones read by the runtime and the
> orchestrator. They are enumerated in `integration/orchestrator.py`
> (`_ENABLE_RETRY_LOOP`, `_ENABLE_RETRY_UI_LOCK`,
> `_ENABLE_CLEAR_REFILL_AFTER_POPUP`) and `integration/runtime.py`
> (`ENABLE_PRODUCTION_TASK_FN`).

## 3. Rollback steps

### 3.1 Soft rollback — disable new behaviour, keep production task_fn

Use when an individual feature (retry, UI-lock, clear-refill) is the suspected
root cause but the real task_fn is still safe to run.

```bash
# On every worker host (systemd / docker / k8s — adapt to your deployment):
export ENABLE_RETRY_LOOP=0
export ENABLE_FSM_AUTO_TRANSITION=0   # reserved / forward-compat — see §2
export ENABLE_RETRY_UI_LOCK=0
export ENABLE_CLEAR_REFILL_AFTER_POPUP=0

# Restart the worker process so the new env is picked up:
systemctl restart lush-givex-worker       # or equivalent
```

The bot now behaves as pre-P0-2: **one attempt per task**, no retry, no
automatic card swap, no post-popup recovery.

### 3.2 Hard rollback — disable production task_fn entirely

Use when any suspected real-API misbehaviour is observed (double-charge,
token leak to Telegram, BitBrowser profile corruption).

```bash
# Clear the production flag on every host:
export ENABLE_PRODUCTION_TASK_FN=""
unset  ENABLE_PRODUCTION_TASK_FN

systemctl restart lush-givex-worker
```

`app/__main__.py` will now pick the no-op stub `task_fn`, and no real
BitBrowser / Givex / Telegram calls are made. Workers stay alive and the
orchestrator remains reachable for diagnostics (`runtime.get_deployment_status()`).

### 3.3 Emergency stop — runtime halt

Use only if the flag-based rollback above does not take effect within one
cycle (≈60 s after restart).

```python
from integration import runtime
runtime.stop(timeout=30)   # all workers check _stop_event at safe points
```

Followed by:

```python
from modules.rollout import main as rollout
rollout.force_rollback(reason="canary failure — see docs/rollback.md §1")
```

See `docs/operations/RUNBOOK.md` §6 and §10 for the full manual procedure.

## 4. Verification after rollback

1. `runtime.get_deployment_status()` reports `current_step` back at the
   rolled-back rung (typically `baseline` or `step_1`).
2. `modules/monitor/main.py::get_metrics()` shows `success_rate` recovering
   within the next 3 cycles.
3. Logs no longer contain retry traces:
   ```bash
   grep -c "retry" worker.log            # should trend to 0
   grep -c "swap_count=" worker.log      # should trend to 0
   ```
4. No new entries in `.idempotency_store.json` marked `completed` with a
   non-success outcome (P0-6 invariant).
5. For hard rollback only: confirm the task_fn is the stub by checking the
   startup banner emitted by `app/__main__.py` — it logs
   `ENABLE_PRODUCTION_TASK_FN=<off>` when the flag is not set.

## 5. Re-enabling after the incident

Do **not** simply re-export the flags. Re-run the canary procedure from
step 1 (`docs/canary_rollout.md`), with the fix for the root cause merged
and a fresh baseline captured via `monitor.save_baseline()`.

## 6. On-call ownership

| Signal | Primary | Escalation |
|---|---|---|
| success_rate drop | on-call engineer | eng-lead |
| token leak in logs / Telegram | security on-call | security-lead (immediate) |
| double-charge suspected | billing on-call | finance-lead + security-lead |
| CDP / BitBrowser timeouts | on-call engineer | platform-lead |

Fill the on-call rotation names into your team's runbook; this file
intentionally names roles rather than people so it stays accurate across
handovers.
