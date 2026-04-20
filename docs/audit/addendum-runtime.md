# Addendum — Runtime Wiring Posture (U-02, U-08)

**Source:** `integration/runtime.py`, `integration/worker_task.py`.

## U-02 — CDP / BitBrowser registration calls

F-01/F-03 have landed.  The BitBrowser session lifecycle now lives in
`integration/worker_task.py::make_task_fn`: `get_bitbrowser_client()` →
`BitBrowserSession(...)` → `cdp.register_driver()` → `cdp._register_pid()`
→ `cdp.register_browser_profile()` → `cdp.unregister_driver()` in a
`finally` block.  `integration/runtime.py` itself remains free of CDP /
BitBrowser wiring (only `cdp.get_browser_profile()` read-only accessor).

**Verdict: CLEARED.** The former lock-in test
`tests/verification/test_runtime_wiring_posture.py` has been deleted
(posture requirement is obsolete now that F-01/F-03 shipped) and
replaced by `tests/verification/test_runtime_wiring_lifecycle.py`,
which asserts the positive wiring in `worker_task.py`.

## U-08 — Stagger-start delay between worker launches

`_stagger_sleep_before_launch()` (Blueprint §1, §8.4) now enforces a
`random.uniform(12, 25)` s gap between consecutive worker launches.  The
call site is `_apply_scale` in the SCALE_UP loop, immediately before
each `start_worker(task_fn)` invocation.  The sleep is interruptible by
`_stop_event` so graceful shutdown preempts the wait.  Stagger state is
independent from the per-worker `_restart_delay` backoff (which handles
failure restarts, not fresh scale-ups) and from the `modules.delay`
behavior delay (which operates inside a cycle, not between cycles).

**Verdict: CLEARED.** Covered by `tests/test_stagger_start.py`.

