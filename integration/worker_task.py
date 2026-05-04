"""Worker task factory — F-01 (entrypoint), F-03 (CDP registration),
F-04 (BitBrowser lifecycle), F-07 (MaxMind zip).

Creates a task_fn suitable for ``integration.runtime.start()``.  The
returned callable wires the full browser lifecycle for one work cycle:

  1. Acquire BitBrowser client (fail fast if unavailable).
  2. Create/launch a browser profile → obtain Selenium attach metadata.
  3. Build a Selenium driver against that metadata.
  4. Wrap in ``GivexDriver``.
  5. Register driver + PID + profile with the CDP registry (F-03).
  6. Probe ``add_cdp_listener`` availability (U-06 guard).
  7. Run ``preflight_geo_check`` to fail fast on a non-US proxy BEFORE any
     MaxMind/persona/run_cycle work (Blueprint §2).
  8. Resolve proxy IP → zip code via MaxMind (F-07).
  9. Execute purchase cycle via ``run_cycle`` when a task_source is wired.
  10. On **all** exits: ``cdp.unregister_driver()`` (GAP-CDP-01).

Feature flag: ``ENABLE_PRODUCTION_TASK_FN`` (default OFF) — the gate is
enforced by the caller (``app/__main__.py``).  This module does **not** read
the flag itself so that tests can import and exercise it freely.
"""
import importlib
import hashlib
import ipaddress
import logging
import threading
import urllib.parse
import uuid
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from modules.cdp import main as cdp
from modules.cdp.driver import (
    _get_proxy_ip,
    _get_current_ip_best_effort,
    _lookup_maxmind_utc_offset,
    maxmind_lookup_geo,
    maxmind_lookup_zip,
)
from modules.cdp.fingerprint import (
    BitBrowserLaunchEndpoint,
    BitBrowserSession,
    get_bitbrowser_client,
)
from integration.cycle_outcome import CycleDidNotCompleteError, normalize_action
from modules.delay.persona import PersonaProfile
from modules.delay.temporal import set_utc_offset

_log = logging.getLogger(__name__)  # pylint: disable=invalid-name

# P1-5: Task-level abort registry.
_abort_lock: threading.Lock = threading.Lock()
_abort_flags: "dict[str, threading.Event]" = {}


@dataclass(frozen=True)
class ProxyGeoResult:
    zip_code: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    utc_offset: float = 0.0
    reason: str = "profile_no_proxy"
    proxy_source: str = "UNKNOWN"
    detected_ip_hash: Optional[str] = None


def abort_task(worker_id: str) -> None:
    """Set abort flag for *worker_id*. Idempotent and thread-safe."""
    try:
        with _abort_lock:
            flag = _abort_flags.setdefault(worker_id, threading.Event())
        flag.set()
    except Exception as exc:  # pylint: disable=broad-except
        _log.warning("worker=%s abort_task=error: %s", worker_id, exc)


def is_task_aborted(worker_id: str) -> bool:
    """Return ``True`` if abort requested for *worker_id*."""
    with _abort_lock:
        flag = _abort_flags.get(worker_id)
    return flag is not None and flag.is_set()


def _register_abort(worker_id: str) -> None:
    with _abort_lock:
        if worker_id not in _abort_flags:
            _abort_flags[worker_id] = threading.Event()


def _clear_abort(worker_id: str) -> None:
    with _abort_lock:
        _abort_flags.pop(worker_id, None)


def _proxy_endpoint_from_metadata(proxy_metadata: object) -> Optional[str]:
    """Extract only proxy host/endpoint fields; credentials are ignored."""
    if isinstance(proxy_metadata, str):
        return proxy_metadata.strip() or None
    if not isinstance(proxy_metadata, dict):
        return None
    nested = proxy_metadata.get("proxy")
    if isinstance(nested, (dict, str)):
        nested_endpoint = _proxy_endpoint_from_metadata(nested)
        if nested_endpoint:
            return nested_endpoint
    for key in (
        "proxyServer",
        "proxy_server",
        "server",
        "proxy",
        "host",
        "ip",
        "proxyHost",
        "proxy_host",
    ):
        value = proxy_metadata.get(key)
        if isinstance(value, str) and value.strip():
            port = proxy_metadata.get("port") or proxy_metadata.get("proxyPort")
            if key in {"host", "ip", "proxyHost", "proxy_host"} and port:
                return f"{value.strip()}:{port}"
            return value.strip()
    return None


def _resolve_bitbrowser_proxy_geo(client: object, profile_id: str) -> ProxyGeoResult:
    """Resolve assigned BitBrowser profile proxy through local DNS + MaxMind."""
    try:
        get_proxy = getattr(client, "get_profile_proxy")
        proxy_metadata = get_proxy(profile_id)
    except Exception:  # pylint: disable=broad-except
        return ProxyGeoResult(reason="bitbrowser_api_error")

    proxy_endpoint = _proxy_endpoint_from_metadata(proxy_metadata)
    if not proxy_endpoint:
        return ProxyGeoResult(reason="profile_no_proxy")

    proxy_source = "BITBROWSER_PROFILE"
    raw = proxy_endpoint
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urllib.parse.urlparse(raw).hostname or ""
        if host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                proxy_source = "BITBROWSER_PROFILE_HOSTNAME_DNS"
    except Exception:  # pylint: disable=broad-except
        pass

    detected_ip = _get_proxy_ip(proxy_endpoint)
    if not detected_ip:
        return ProxyGeoResult(
            reason="profile_proxy_unreachable",
            proxy_source=proxy_source,
        )

    # Issue contract requires the first 12 SHA-256 hex chars; never log raw IPs.
    ip_hash = hashlib.sha256(detected_ip.encode("utf-8")).hexdigest()[:12]
    geo = maxmind_lookup_geo(detected_ip)
    zip_code = geo.get("zip") if isinstance(geo, dict) else None
    city = geo.get("city") if isinstance(geo, dict) else None
    state = geo.get("state") if isinstance(geo, dict) else None
    offset = _lookup_maxmind_utc_offset(detected_ip)
    reason = "ok"
    if not zip_code:
        reason = "maxmind_zip_missing" if (city or state) else "maxmind_geo_incomplete"
    return ProxyGeoResult(
        zip_code=zip_code,
        city=city,
        state=state,
        utc_offset=float(offset) if offset is not None else 0.0,
        reason=reason,
        proxy_source=proxy_source,
        detected_ip_hash=ip_hash,
    )


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
        _register_abort(worker_id)
        if is_task_aborted(worker_id):
            _clear_abort(worker_id)
            return
        bb_client = get_bitbrowser_client()
        if bb_client is None:
            _clear_abort(worker_id)
            raise RuntimeError(
                f"BitBrowser client unavailable for worker {worker_id}. "
                "Set BITBROWSER_API_KEY and ensure the endpoint is reachable."
            )

        with BitBrowserSession(bb_client) as (profile_id, launch_endpoint):
            selenium_driver = _build_remote_driver(launch_endpoint)
            givex_driver = None
            try:
                # Wrap in GivexDriver and register with CDP registry (F-03).
                # Persona is derived deterministically from worker_id using
                # the same formula as integration.runtime.start_worker so the
                # production path keeps Layer 2 anti-detection (4x4 card
                # pattern, ghost-cursor, temporal night factor) active.
                from modules.cdp.driver import GivexDriver  # noqa: PLC0415
                persona_seed = zlib.crc32(worker_id.encode()) & 0xFFFFFFFF
                persona = PersonaProfile(persona_seed)
                givex_driver = GivexDriver(selenium_driver, persona=persona)
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

                # Geo pre-flight (Blueprint §2): run immediately after the
                # browser/session is up — before MaxMind, persona, and any
                # purchase/run_cycle logic.  A non-US proxy must abort the
                # cycle here so we don't waste MaxMind/zip work or seed a
                # persona for a session that will never proceed.  On
                # failure, the raised RuntimeError propagates out of the
                # ``with BitBrowserSession(...)`` block whose ``__exit__``
                # releases the profile (POOL-NO-DELETE: pool mode does
                # NOT delete the profile, legacy mode runs close+delete).
                givex_driver.preflight_geo_check()

                # Resolve BitBrowser profile proxy → local MaxMind geo (F-07).
                geo_result = _resolve_bitbrowser_proxy_geo(bb_client, profile_id)
                zip_code = geo_result.zip_code
                utc_offset = geo_result.utc_offset

                # Propagate UTC offset to TemporalModel via ContextVar so all
                # delay computations on this worker thread see the proxy-derived
                # local-hour for DAY/NIGHT detection (Blueprint §10).
                set_utc_offset(utc_offset)

                if zip_code:
                    _log.info(
                        "worker=%s zip_selection=zip_match zip=%s utc_offset=%+.1fh",
                        worker_id,
                        zip_code,
                        utc_offset,
                    )
                else:
                    _log.info(
                        "worker=%s zip_selection=round_robin "
                        "(MaxMind zip unavailable) utc_offset=%+.1fh",
                        worker_id,
                        utc_offset,
                    )

                # Run purchase cycle when a task source is wired (F-02/F-07).
                if task_source is not None:
                    task = task_source(worker_id)
                    if task is not None:
                        from modules.common.types import CycleContext  # noqa: PLC0415
                        ctx = CycleContext(
                            cycle_id=uuid.uuid4().hex,
                            worker_id=worker_id,
                            zip_code=zip_code,
                            utc_offset_hours=utc_offset,
                        )
                        orchestrator_module = importlib.import_module(
                            "integration.orchestrator"
                        )
                        run_cycle = orchestrator_module.run_cycle
                        action, _state, _total = run_cycle(
                            task, zip_code=zip_code, worker_id=worker_id,
                            ctx=ctx,
                            proxy_city=geo_result.city,
                            proxy_state=geo_result.state,
                            proxy_geo_reason=geo_result.reason,
                            proxy_source=geo_result.proxy_source,
                            detected_ip_hash=geo_result.detected_ip_hash,
                            abort_check=lambda: is_task_aborted(worker_id),
                        )
                        normalized = normalize_action(action)
                        if normalized != "complete":
                            if normalized == "abort_cycle":
                                _log.info(
                                    "worker=%s abort_cycle — releasing profile",
                                    worker_id,
                                )
                            raise CycleDidNotCompleteError(action=normalized)
                else:
                    _log.debug(
                        "worker=%s profile=%s driver registered; "
                        "no task_source wired — purchase cycle skipped.",
                        worker_id,
                        profile_id,
                    )
            finally:
                # Blueprint §7 end-of-cycle hard-reset: wipe Cookies/Cache
                # at the browser level *before* BitBrowserSession.__exit__
                # closes the profile.  This is defense-in-depth — the same
                # call also runs at the start of the next cycle inside
                # navigate_to_egift (INV-SESSION-01) — but issuing it here
                # makes the implementation literally match Blueprint §7
                # ("Thực hiện lệnh xóa Cookies/Cache lần cuối ở cấp độ
                # trình duyệt") and guarantees a clean state even if a
                # transient /browser/close failure leaves the session
                # alive.  Best-effort: never propagate exceptions out of
                # the cleanup path, since the driver may already be torn
                # down (e.g. Selenium session crashed mid-cycle).
                try:
                    if givex_driver is not None:
                        givex_driver._clear_browser_state()  # pylint: disable=protected-access
                except Exception:  # pylint: disable=broad-except
                    _log.debug(
                        "worker=%s end-of-cycle _clear_browser_state failed",
                        worker_id,
                        exc_info=True,
                    )
                # Always unregister the driver to prevent registry leaks (GAP-CDP-01)
                cdp.unregister_driver(worker_id)
                _clear_abort(worker_id)

    return task_fn


def _build_remote_driver(launch_endpoint):
    """Build a Selenium driver from BitBrowser launch metadata.

    Legacy BitBrowser responses provide a Selenium Remote ``webdriver`` URL.
    BitBrowser v144+ responses provide a DevTools ``http`` endpoint plus a
    local chromedriver path; those must attach through ``Chrome`` with
    ``ChromeOptions.debugger_address`` rather than ``Remote(.../session)``.

    Raises:
        RuntimeError: if selenium is not installed.
    """
    if isinstance(launch_endpoint, str):
        return _build_legacy_remote_driver(launch_endpoint)
    if isinstance(launch_endpoint, BitBrowserLaunchEndpoint):
        if launch_endpoint.uses_remote() and launch_endpoint.webdriver_url is not None:
            return _build_legacy_remote_driver(launch_endpoint.webdriver_url)
        if (
            launch_endpoint.uses_chromedriver_attach()
            and launch_endpoint.debugger_address is not None
            and launch_endpoint.driver_path is not None
        ):
            return _build_chromedriver_attach_driver(
                debugger_address=launch_endpoint.debugger_address,
                driver_path=launch_endpoint.driver_path,
            )
        raise RuntimeError(
            "BitBrowser launch endpoint requires either webdriver_url or both "
            "debugger_address and driver_path"
        )
    raise RuntimeError(
        "BitBrowser launch endpoint requires either webdriver_url or both "
        "debugger_address and driver_path"
    )


def _build_legacy_remote_driver(webdriver_url: str):
    """Build a Selenium Remote WebDriver against *webdriver_url*.

    Forward-compatible with Selenium >= 4.10 by passing ``options=ChromeOptions()``
    instead of the deprecated/removed ``desired_capabilities=`` keyword argument.
    Falls back to ``desired_capabilities=`` only if the installed Selenium build
    rejects ``options=`` (i.e. very old pre-4.x clients).
    """
    try:
        remote_module = importlib.import_module("selenium.webdriver")
        Remote = remote_module.Remote
        ChromeOptions = remote_module.ChromeOptions
    except ImportError as exc:
        raise RuntimeError(
            "selenium is not installed; cannot build Remote driver. "
            "Install selenium-wire==5.1.0 for production use."
        ) from exc

    options = ChromeOptions()
    try:
        return Remote(command_executor=webdriver_url, options=options)
    except TypeError as exc:
        # Legacy fallback for Selenium clients that don't accept ``options=``.
        # Selenium >= 4.10 has removed ``desired_capabilities=``, so this branch
        # is only taken on older installs where ``options=`` is unsupported.
        # Re-raise unrelated TypeErrors so callers see the original failure.
        if "options" not in str(exc):
            raise
        capabilities_module = importlib.import_module(
            "selenium.webdriver.common.desired_capabilities"
        )
        DesiredCapabilities = capabilities_module.DesiredCapabilities
        capabilities = dict(DesiredCapabilities.CHROME)
        return Remote(
            command_executor=webdriver_url,
            desired_capabilities=capabilities,
        )


def _build_chromedriver_attach_driver(debugger_address: str, driver_path: str):
    """Attach Selenium to an already-open BitBrowser Chrome instance."""
    try:
        webdriver_module = importlib.import_module("selenium.webdriver")
        service_module = importlib.import_module("selenium.webdriver.chrome.service")
        Chrome = webdriver_module.Chrome
        ChromeOptions = webdriver_module.ChromeOptions
        Service = service_module.Service
    except ImportError as exc:
        raise RuntimeError(
            "selenium is not installed; cannot build Chrome attach driver. "
            "Install selenium-wire==5.1.0 for production use."
        ) from exc

    options = ChromeOptions()
    options.debugger_address = debugger_address
    service = Service(executable_path=driver_path)
    return Chrome(service=service, options=options)


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
