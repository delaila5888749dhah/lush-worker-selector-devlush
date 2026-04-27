# Addendum — Selenium Flavor Pin (U-06 / F2 audit)

**Pinned:** `selenium-wire==5.1.0` in `requirements.txt`.

Rationale: the orchestrator's Total Watchdog uses `driver.add_cdp_listener` to
intercept `Network.responseReceived` events — a method provided by
`selenium-wire` (not by stock `selenium`). Neither flavor was pinned before.
`selenium-wire==5.1.0` is the latest stable at audit time; no known advisories.

## Startup probe

`probe_cdp_listener_support(driver_obj)` in `integration/runtime.py`:
- Verifies `hasattr(driver_obj, "add_cdp_listener")` and callable.
- Returns `True` when the hook is callable (strict-mode pass).
- Returns `False` and logs a `WARNING` when the hook is missing **but**
  `ALLOW_DOM_ONLY_WATCHDOG=1` is set (documented degraded mode — see below).
- Raises `RuntimeError` with a clear operator message otherwise, naming both
  the pinned Selenium flavor and the fallback opt-in env var so misconfigured
  deployments are caught at driver bring-up time rather than silently as a
  10-second Phase A cycle timeout.

The probe is invoked from `integration/worker_task.py` immediately after the
seleniumwire driver is constructed and registered with the CDP registry, so
the assertion fires once per cycle before any network watchdog attach.

## DOM-only fallback contract — `ALLOW_DOM_ONLY_WATCHDOG`

| `ALLOW_DOM_ONLY_WATCHDOG` | Driver lacks `add_cdp_listener` |
| ------------------------- | ------------------------------- |
| unset / `0` / `false` / `no` (default) | **RuntimeError** at bootstrap (strict mode). |
| `1` / `true` / `yes` (case-insensitive) | **WARNING** logged; orchestrator falls back to DOM polling. |

When the fallback is enabled, the orchestrator's
`_setup_network_total_listener` starts a daemon DOM-polling thread for
**Phase A** (pre-fill pricing total) so the pricing watchdog still receives a
notification before any card field is typed. Phase C (post-submit
confirmation) already issues a synchronous `_notify_total_from_dom` call, so
no additional polling is required there. Both phases therefore use DOM
polling as the signal source under the fallback contract.

The polling thread stops as soon as:

- `_notify_total_from_dom` succeeds (worker enters `_notified_workers_this_cycle`),
- the per-worker stop event is set (`_stop_phase_a_dom_polling`, called from
  the Phase A wait `finally` block), or
- the watchdog timeout budget (`PAYMENT_WATCHDOG_TIMEOUT_S`, default 10s) +1s
  hard-stop deadline elapses.

Operators should treat the fallback as a **degraded** mode and re-install the
pinned Selenium flavor as soon as practical.

Unit tests:
- `tests/verification/test_selenium_flavor_probe.py` (probe behaviour, env contract)
- `tests/test_phase_a_dom_polling_fallback.py` (orchestrator polling thread)

**Verdict: CLEARED.**
