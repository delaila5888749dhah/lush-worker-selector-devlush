# Addendum — Selenium Flavor Pin (U-06 / F2 audit)

**Pinned:** `selenium-wire==5.1.0` in `requirements.txt`.

The orchestrator's Total Watchdog uses `driver.add_cdp_listener` to intercept
`Network.responseReceived` events — provided by `selenium-wire`, not stock
`selenium`.

## Startup probe

`probe_cdp_listener_support(driver_obj)` in `integration/runtime.py`:
- Returns `True` when the hook is callable.
- Returns `False` and logs a `WARNING` when the hook is missing **and**
  `ALLOW_DOM_ONLY_WATCHDOG=1` (documented degraded mode — see below).
- Raises `RuntimeError` otherwise (names the pinned flavor and the fallback
  env), so misconfigured deployments fail at bring-up rather than as a 10s
  Phase A cycle timeout.

Invoked from `integration/worker_task.py` after the seleniumwire driver is
constructed and registered.

## DOM-only fallback contract — `ALLOW_DOM_ONLY_WATCHDOG`

| `ALLOW_DOM_ONLY_WATCHDOG` | Driver lacks `add_cdp_listener` |
| ------------------------- | ------------------------------- |
| unset / `0` / `false` / `no` (default) | **RuntimeError** at bootstrap. |
| `1` / `true` / `yes` (case-insensitive) | **WARNING**; orchestrator falls back to DOM polling. |

When enabled, `_setup_network_total_listener` starts a daemon DOM-polling
thread for **Phase A** so the pricing watchdog still fires before card data
is typed. Phase C already runs `_notify_total_from_dom` synchronously after
submit, so both phases use DOM polling under the fallback.

The polling thread stops on first notify, when the per-worker stop event is
set (Phase A wait `finally` → `_stop_phase_a_dom_polling`), or after
`PAYMENT_WATCHDOG_TIMEOUT_S + 1s`. Treat the fallback as degraded and
re-install the pinned Selenium flavor as soon as practical.

Tests: `tests/verification/test_selenium_flavor_probe.py`,
`tests/test_phase_a_dom_polling_fallback.py`.

**Verdict: CLEARED.**
