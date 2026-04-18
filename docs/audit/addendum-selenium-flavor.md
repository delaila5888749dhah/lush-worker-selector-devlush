# Addendum — Selenium Flavor Pin (U-06)

## Flavor choice

**Pinned: `selenium-wire==5.1.0`**

Rationale: the orchestrator's Total Watchdog uses `driver.add_cdp_listener` to
intercept `Network.responseReceived` events. This method is provided by
`selenium-wire` (not by stock `selenium`). Neither flavor was previously pinned
in `requirements.txt`. Per the remediation plan, `selenium-wire==5.1.0` (latest
stable at audit time) is pinned as the authoritative flavor.

## Version

`selenium-wire==5.1.0` — added to `requirements.txt`.

No known security advisories (verified against GitHub Advisory Database).

## Startup probe

`probe_cdp_listener_support(driver_obj)` in `integration/runtime.py`:
- Checks `hasattr(driver_obj, "add_cdp_listener")` and
  `callable(getattr(driver_obj, "add_cdp_listener", None))`.
- Raises `RuntimeError` with a clear operator message if the check fails.
- **Not wired** into any current call site because no driver is constructed in
  `runtime.py` yet (F-01 is unfixed). The function is exported as a public helper.
  A TODO comment directs PR-04 (F-01) to invoke it during `task_fn` bring-up.

Unit test: `tests/verification/test_selenium_flavor_probe.py`.

**U-06 verdict: CLEARED** — flavor pinned, probe implemented and tested.
