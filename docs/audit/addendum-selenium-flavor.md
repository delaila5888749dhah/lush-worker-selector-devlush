# Addendum — Selenium Flavor Pin (U-06 / F2 audit)

**Pinned for local-launched Selenium:** `selenium-wire==5.1.0` in
`requirements.txt`.

The orchestrator's Total Watchdog uses `driver.add_cdp_listener` to intercept
`Network.responseReceived` events. In local-launched Selenium this hook is
provided by `selenium-wire`, not stock `selenium`. In BitBrowser attach mode,
the attached driver is stock Selenium (or an equivalent attach mechanism), so
installing `selenium-wire` does **not** add `add_cdp_listener` to that driver.

## Startup probe

`probe_cdp_listener_support(driver_obj, attach_mode_hint=None)` in
`integration/runtime.py`:
- Returns `True` when the hook is callable.
- Returns `False` and logs a `WARNING` when the hook is missing **and**
  `ALLOW_DOM_ONLY_WATCHDOG=1` (documented degraded mode — see below).
- Raises `RuntimeError` otherwise, so missing CDP listener support fails at
  bring-up rather than as a 10s Phase A cycle timeout.

The probe distinguishes local-launched Selenium from BitBrowser/attach mode.
Attach mode is indicated by `goog:chromeOptions.debuggerAddress` in driver
capabilities or by an explicit launch-path hint from the caller. Local messages
mention `selenium-wire==5.1.0` as a repair option. Attach-mode messages do not
recommend reinstalling `selenium-wire`; they point to the explicit DOM fallback
instead.

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
`PAYMENT_WATCHDOG_TIMEOUT_S + 1s`. Treat the fallback as degraded. For
local-launched Selenium, reinstalling the pinned Selenium flavor may restore CDP
listener support. For BitBrowser attach mode, reinstalling `selenium-wire` does
not help because the attached driver does not expose `add_cdp_listener`.

## Local-launched mode

Use this path when the application launches and owns the Chrome/Selenium
driver.

- Expected CDP listener path: `selenium-wire==5.1.0` provides
  `driver.add_cdp_listener`.
- Missing listener with `ALLOW_DOM_ONLY_WATCHDOG` unset: startup fails fast and
  recommends installing the pinned local-launched Selenium flavor.
- Missing listener with `ALLOW_DOM_ONLY_WATCHDOG=1`: startup logs a warning and
  uses DOM polling as a degraded fallback.

## Attach mode (BitBrowser)

Use this path when attaching to a BitBrowser-managed Chrome session.

- Expected driver shape: `goog:chromeOptions.debuggerAddress` is present in
  capabilities, or the caller supplies an explicit launch-path hint.
- `BITBROWSER_POOL_MODE=1` only indicates BitBrowser profile pool acquisition
  strategy. It does not guarantee attach driver shape; BitBrowser pool mode can
  still return a legacy `webdriver` URL.
- `selenium-wire==5.1.0` does **not** restore `add_cdp_listener` in this mode,
  because the attach path bypasses selenium-wire's wrapped driver.
- Missing listener with `ALLOW_DOM_ONLY_WATCHDOG` unset: startup still fails
  fast. This is intentional; degraded DOM polling is never enabled silently.
- Missing listener with `ALLOW_DOM_ONLY_WATCHDOG=1`: startup logs an
  attach-mode-specific warning and uses DOM polling as the supported fallback.

## Decision tree

1. Does the active driver expose callable `add_cdp_listener`?
   - **Yes:** no fallback is needed; startup continues.
   - **No:** continue to mode detection.
2. Are you in BitBrowser/attach mode?
   - **Yes:** set `ALLOW_DOM_ONLY_WATCHDOG=1` to opt into the supported
     DOM-polling fallback. Do not reinstall `selenium-wire` expecting it to add
     the listener to the attached driver.
   - **No / local-launched Selenium:** install/verify `selenium-wire==5.1.0`.
     If CDP listener support is still unavailable and you accept degraded
     operation, set `ALLOW_DOM_ONLY_WATCHDOG=1`.
3. Is `ALLOW_DOM_ONLY_WATCHDOG=1` set?
   - **Yes:** probe returns `False` and logs a mode-specific warning.
   - **No:** probe raises `RuntimeError` and startup fails fast.

Tests: `tests/verification/test_selenium_flavor_probe.py`,
`tests/test_cdp_attach_warning.py`, `tests/test_phase_a_dom_polling_fallback.py`.

**Verdict: CLEARED.**
