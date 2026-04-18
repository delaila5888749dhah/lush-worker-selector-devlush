# Addendum — Runtime Wiring Posture (U-02, U-08)

**Source read:** `integration/runtime.py` (full file, all functions in scope).

## U-02 — CDP / BitBrowser registration calls

Searched the entire module for each symbol:

| Symbol | Present in runtime.py? |
|---|---|
| `cdp.register_driver` | **NO** |
| `cdp._register_pid` | **NO** |
| `cdp.register_browser_profile` | **NO** |
| `BitBrowserSession(...)` | **NO** |
| `get_bitbrowser_client()` | **NO** |

The only `cdp` calls in `runtime.py` are:
- `cdp.get_browser_profile(worker_id)` — read-only, in `get_worker_browser_profile()`.
- `from modules.cdp import main as cdp` — module import, line 25.
- `from modules.cdp.proxy import get_default_pool` — proxy pool import, line 26.

No driver is constructed, no PID registered, no browser profile registered, no
BitBrowser session opened anywhere in `integration/runtime.py`.  F-01/F-03 wiring
is **not yet present** and remains the responsibility of those fix PRs.

**U-02 verdict: CLEARED** — runtime.py does not register CDP drivers, PIDs,
browser profiles, or open BitBrowser sessions. Lock-in test in
`tests/verification/test_runtime_wiring_posture.py` will break if silent wiring
is added before F-01/F-03 PRs are reviewed.

## U-08 — Stagger-start delay between worker launches

Searched `integration/runtime.py` for `random.uniform`, `random.uniform(12, 25)`,
`stagger`, and all `time.sleep` / `_safe_sleep` calls in `start_worker` and
`_apply_scale`.

Findings:
- `start_worker`: applies an **exponential backoff** `_restart_delay` only when
  `_pending_restarts > 0`; this is a restart-backoff, not a stagger delay.
- `_apply_scale`: calls `start_worker(task_fn)` in a tight loop with **no sleep**
  between iterations.
- No `random.uniform(12, 25)` anywhere in the module.

Blueprint §2 specifies: *"random.uniform(12, 25) seconds between worker launches
to avoid network-cycle detection by Givex."*  This is **absent** from the current
code.

**U-08 verdict: REMAINS_OPEN** — the blueprint stagger-start delay is not
implemented.  A follow-up issue should be filed: **"[follow-up] Implement
stagger-start delay (random.uniform 12–25 s) between worker launches per
Blueprint §2"**.  Do not fix here.
