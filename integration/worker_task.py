"""Worker task factory — F-01 (entrypoint), F-03 (CDP registration), F-04 (BitBrowser lifecycle), F-07 (MaxMind zip).

Creates a task_fn suitable for ``integration.runtime.start()``.  The
returned callable wires the full browser lifecycle for one work cycle:

  1. Acquire BitBrowser client (fail fast if unavailable).
  2. Create/launch a browser profile → obtain the ChromeDriver WebSocket URL.
  3. Build a Selenium Remote driver against that URL.
  4. Wrap in ``GivexDriver``.
  5. Register driver + PID + profile with the CDP registry (F-03).
  6. Probe ``add_cdp_listener`` availability (U-06 guard).
  7. Resolve proxy/public IP → zip code via MaxMind (F-07).
  8. Execute purchase cycle via ``run_cycle`` when a task_source is wired.
  9. On **all** exits: ``cdp.unregister_driver()`` (GAP-CDP-01).

Feature flag: ``ENABLE_PRODUCTION_TASK_FN`` (default OFF) — the gate is
enforced by the caller (``app/__main__.py``).  This module does **not** read
the flag itself so that tests can import and exercise it freely.
"""
import logging
from typing import Any, Callable, Optional

from modules.cdp import main as cdp
from modules.cdp.driver import _get_current_ip_best_effort, maxmind_lookup_zip
from modules.cdp.fingerprint import BitBrowserSession, get_bitbrowser_client

_log = logging.getLogger(__name__)  # pylint: disable=invalid-name


def make_task_fn(task_source: Optional[Callable[[str], Any]] = None) -> Callable[[str], None]:
    """Return a production task_fn for ``runtime.start()``.

    The returned callable is stateless between calls; all per-cycle
    resources are created fresh on each invocation.

    Args:
        task_source: Optional callable ``(worker_id) -> WorkerTask | None``.
            When provided, the task_fn will call it each cycle to obtain the
            next task and then invoke ``run_cycle`` with the resolved zip code
            (F-07).  When ``None``, the browser lifecycle is exercised without
            running a purchase cycle.

    Returns:
        A callable that accepts *worker_id* (str) and executes one
        browser lifecycle cycle.

    Raises:
        RuntimeError: propagated on startup failure (BitBrowser unavailable,
            Selenium not installed, or CDP listener probe fails).
    """

    def task_fn(worker_id: str) -> None:
        """Execute one browser lifecycle cycle for *worker_id*."""
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

                # Resolve proxy/public IP → zip code via MaxMind (F-07)
                zip_code: Optional[str] = None
                try:
                    detected_ip = _get_current_ip_best_effort()
                    if detected_ip:
                        zip_code = maxmind_lookup_zip(detected_ip)
                except Exception as exc:  # pylint: disable=broad-except
                    _log.debug(
                        "worker=%s zip derivation error: %s", worker_id, exc
                    )

                if zip_code:
                    _log.info(
                        "worker=%s zip_selection=zip_match zip=%s",
                        worker_id,
                        zip_code,
                    )
                else:
                    _log.info(
                        "worker=%s zip_selection=round_robin "
                        "(MaxMind zip unavailable)",
                        worker_id,
                    )

                # Run purchase cycle when a task source is wired (F-02/F-07)
                if task_source is not None:
                    task = task_source(worker_id)
                    if task is not None:
                        from integration.orchestrator import run_cycle  # noqa: PLC0415
                        run_cycle(task, zip_code=zip_code, worker_id=worker_id)
                else:
                    _log.debug(
                        "worker=%s profile=%s driver registered; "
                        "no task_source wired — purchase cycle skipped.",
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
        # pylint: disable=C0415  # import-outside-toplevel; keep selenium optional
        from selenium.webdriver import Remote  # type: ignore[import]
        from selenium.webdriver.common.desired_capabilities import (  # type: ignore[import]
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
    except (AttributeError, TypeError, ValueError):  # pylint: disable=broad-except
        _log.debug("_get_browser_pid: could not read PID", exc_info=True)
    return None
