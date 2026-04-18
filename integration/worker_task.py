"""Worker task factory — F-01 (entrypoint), F-03 (CDP registration), F-04 (BitBrowser lifecycle).

Creates a task_fn suitable for ``integration.runtime.start()``.  The
returned callable wires the full browser lifecycle for one work cycle:

  1. Acquire BitBrowser client (fail fast if unavailable).
  2. Create/launch a browser profile → obtain the ChromeDriver WebSocket URL.
  3. Build a Selenium Remote driver against that URL.
  4. Wrap in ``GivexDriver``.
  5. Register driver + PID + profile with the CDP registry (F-03).
  6. Probe ``add_cdp_listener`` availability (U-06 guard).
  7. [Purchase sequence placeholder — wired in PR-05 / F-02]
  8. On **all** exits: ``cdp.unregister_driver()`` (GAP-CDP-01).

Feature flag: ``ENABLE_PRODUCTION_TASK_FN`` (default OFF) — the gate is
enforced by the caller (``app/__main__.py``).  This module does **not** read
the flag itself so that tests can import and exercise it freely.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from modules.cdp import main as cdp
from modules.cdp.fingerprint import BitBrowserSession, get_bitbrowser_client
from modules.cdp.proxy import get_default_pool

_log = logging.getLogger(__name__)


def make_task_fn() -> Callable[[str], None]:
    """Return a production task_fn for ``runtime.start()``.

    The returned callable is stateless between calls; all per-cycle
    resources are created fresh on each invocation.

    Returns:
        A callable that accepts *worker_id* (str) and executes one
        browser lifecycle cycle.

    Raises:
        RuntimeError: propagated on startup failure (BitBrowser unavailable,
            Selenium not installed, or CDP listener probe fails).
    """

    def task_fn(worker_id: str) -> None:
        bb_client = get_bitbrowser_client()
        if bb_client is None:
            raise RuntimeError(
                f"BitBrowser client unavailable for worker {worker_id}. "
                "Set BITBROWSER_API_KEY and ensure the endpoint is reachable."
            )

        with BitBrowserSession(bb_client) as (profile_id, webdriver_url):
            selenium_driver = _build_remote_driver(webdriver_url)
            try:
                # Wrap in GivexDriver and register with CDP registry (F-03)
                from modules.cdp.driver import GivexDriver  # noqa: PLC0415
                givex_driver = GivexDriver(selenium_driver)
                cdp.register_driver(worker_id, givex_driver)

                # Register browser process PID when available (F-03)
                pid = _get_browser_pid(selenium_driver)
                if pid is not None:
                    cdp._register_pid(worker_id, pid)  # pylint: disable=protected-access

                # Register BitBrowser profile id (F-04)
                cdp.register_browser_profile(worker_id, profile_id)

                # Guard: verify driver exposes add_cdp_listener (U-06)
                from integration.runtime import probe_cdp_listener_support  # noqa: PLC0415
                probe_cdp_listener_support(selenium_driver)

                # Purchase sequence is wired in PR-05 (F-02).
                # Driver is registered and ready; placeholder returns cleanly.
                _log.debug(
                    "worker=%s profile=%s driver registered; "
                    "purchase sequence pending (PR-05).",
                    worker_id,
                    profile_id,
                )
            finally:
                # Always unregister the driver to prevent registry leaks (GAP-CDP-01)
                cdp.unregister_driver(worker_id)

    return task_fn


def _build_remote_driver(webdriver_url: str):
    """Build a Selenium Remote WebDriver against *webdriver_url*.

    Raises:
        RuntimeError: if selenium is not installed.
    """
    try:
        from selenium.webdriver import Remote  # type: ignore[import]  # noqa: PLC0415
        from selenium.webdriver.common.desired_capabilities import (  # type: ignore[import]  # noqa: PLC0415
            DesiredCapabilities,
        )
        capabilities = dict(DesiredCapabilities.CHROME)
        return Remote(
            command_executor=webdriver_url,
            desired_capabilities=capabilities,
        )
    except ImportError as exc:
        raise RuntimeError(
            "selenium is not installed; cannot build Remote driver. "
            "Install selenium-wire==5.1.0 for production use."
        ) from exc


def _get_browser_pid(driver) -> Optional[int]:
    """Try to read the browser process PID from *driver*.

    Returns ``None`` if the PID cannot be determined (e.g. plain Remote
    driver, non-seleniumwire driver, or driver not yet connected).
    """
    try:
        pid = getattr(driver, "browser_pid", None)
        if pid is not None:
            return int(pid)
        service = getattr(driver, "service", None)
        if service is not None:
            proc = getattr(service, "process", None)
            if proc is not None:
                return int(proc.pid)
    except Exception:  # pylint: disable=broad-except
        _log.debug("_get_browser_pid: could not read PID", exc_info=True)
    return None
