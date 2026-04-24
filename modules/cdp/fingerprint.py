"""BitBrowser client utilities for per-worker fingerprint lifecycle."""
# pylint: disable=duplicate-code

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

_log = logging.getLogger(__name__)  # pylint: disable=invalid-name


# ── BitBrowser endpoint scheme validation (INV-BITBROWSER-ENDPOINT-01) ───
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _validate_endpoint_scheme(endpoint: str) -> None:
    """Warn (or raise in strict mode) when ``endpoint`` is HTTP on a non-loopback host.

    Plain HTTP is only safe on loopback (127.0.0.1 / localhost / ::1). On any
    other host the API key transits the network in clear-text, so a warning
    is emitted. When ``BITBROWSER_ENDPOINT_STRICT`` is truthy the condition
    escalates to ``ValueError`` instead.
    """
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "http":
        return
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return
    msg = (
        "BITBROWSER_ENDPOINT is HTTP on non-loopback host %r — the API key "
        "will be sent in clear-text. Use HTTPS or a loopback endpoint."
    ) % host
    if _env_flag("BITBROWSER_ENDPOINT_STRICT"):
        raise ValueError(msg)
    _log.warning(msg)


# ── _post() retry configuration (INV-BITBROWSER-RETRY-01) ────────────────
def _retry_attempts() -> int:
    try:
        return max(1, int(os.getenv("BITBROWSER_RETRY_ATTEMPTS", "3")))
    except ValueError:
        return 3


def _retry_wait_initial_s() -> float:
    try:
        return max(0.0, float(os.getenv("BITBROWSER_RETRY_WAIT_INITIAL_S", "0.5")))
    except ValueError:
        return 0.5


def _retry_wait_max_s() -> float:
    try:
        return max(0.0, float(os.getenv("BITBROWSER_RETRY_WAIT_MAX_S", "8.0")))
    except ValueError:
        return 8.0


def _is_retryable(exc: BaseException) -> bool:
    """Retry only on transient network failures and 5xx responses — NOT 4xx."""
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= exc.code < 600
    return isinstance(exc, (urllib.error.URLError, OSError))


class BitBrowserClient:
    """Thin HTTP client for BitBrowser profile APIs."""

    def __init__(self, endpoint: str, api_key: str):
        self._endpoint = endpoint.rstrip("/")
        scheme = urllib.parse.urlparse(self._endpoint).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"Unsupported endpoint scheme: {scheme!r}")
        _validate_endpoint_scheme(self._endpoint)
        self._api_key = api_key

    def _url(self, path: str) -> str:
        url = f"{self._endpoint}{path}"
        scheme = urllib.parse.urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"Unsupported URL scheme: {scheme!r}")
        return url

    def _post(self, path: str, payload: Dict[str, object],
              timeout: int = 10) -> Dict[str, object]:
        """POST JSON with exponential backoff on transient failures.

        Retries on ``URLError``, ``OSError``, and 5xx responses up to
        ``BITBROWSER_RETRY_ATTEMPTS`` total attempts. 4xx responses and
        other errors fail fast. Backoff doubles each attempt, capped at
        ``BITBROWSER_RETRY_WAIT_MAX_S``.
        """
        data = json.dumps(payload).encode("utf-8")
        attempts = _retry_attempts()
        wait = _retry_wait_initial_s()
        wait_max = _retry_wait_max_s()
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                self._url(path),
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "X-Api-Key": self._api_key,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
                    body = json.loads(resp.read().decode("utf-8"))
                if isinstance(body, dict) and isinstance(body.get("data"), dict):
                    return body["data"]
                if isinstance(body, dict):
                    return body
                raise RuntimeError("BitBrowser API returned non-dict JSON payload")
            except Exception as exc:  # pylint: disable=broad-except
                last_exc = exc
                if not _is_retryable(exc) or attempt >= attempts:
                    raise
                _log.warning(
                    "BitBrowser _post %s attempt %d/%d failed (%s); retrying in %.2fs",
                    path, attempt, attempts, type(exc).__name__, wait,
                )
                time.sleep(wait)
                wait = min(wait * 2, wait_max)
        # Unreachable — loop either returns or re-raises.
        raise RuntimeError("BitBrowser _post exhausted retries") from last_exc

    def create_profile(self) -> str:
        """POST /api/v1/browser/create → returns profile_id string."""
        payload = {
            "platform": "windows",
            "name": f"worker-{uuid4().hex[:8]}",
        }
        try:
            data = self._post("/api/v1/browser/create", payload, timeout=10)
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("BitBrowser create_profile request failed") from exc
        profile_id = data.get("id") or data.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id:
            raise RuntimeError("BitBrowser create_profile response missing profile id")
        return profile_id

    def launch_profile(self, profile_id: str) -> Dict[str, object]:
        """POST /api/v1/browser/open → returns response dict."""
        try:
            data = self._post("/api/v1/browser/open", {"id": profile_id}, timeout=30)
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("BitBrowser launch_profile request failed") from exc
        if not isinstance(data, dict):
            raise RuntimeError("BitBrowser launch_profile response payload must be dict")
        return data

    def close_profile(self, profile_id: str) -> None:
        """POST /api/v1/browser/close. No-op if request fails."""
        try:
            self._post("/api/v1/browser/close", {"id": profile_id}, timeout=10)
        except (urllib.error.URLError, OSError) as exc:
            _log.warning("BitBrowser close_profile failed for %s: %s", profile_id, exc)

    def delete_profile(self, profile_id: str) -> None:
        """POST /api/v1/browser/delete. No-op if request fails."""
        try:
            self._post("/api/v1/browser/delete", {"id": profile_id}, timeout=10)
        except (urllib.error.URLError, OSError) as exc:
            _log.warning("BitBrowser delete_profile failed for %s: %s", profile_id, exc)

    def is_available(self) -> bool:
        """GET /api/v1/browser/list → True if 2xx response."""
        try:
            req = urllib.request.Request(
                self._url("/api/v1/browser/list"),
                headers={"X-Api-Key": self._api_key},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:  # nosec B310
                resp.read()
            return True
        except (urllib.error.URLError, OSError):
            return False


class BitBrowserSession:
    """Context manager for BitBrowser profile lifecycle.

    __enter__: create_profile() → launch_profile() → return (profile_id, webdriver_url)
    __exit__: release_profile() — idempotent close + delete, best-effort
    """

    def __init__(self, client: BitBrowserClient):
        self._client = client
        self._profile_id: Optional[str] = None
        self._released: bool = False

    @property
    def profile_id(self) -> Optional[str]:
        """Return the bound profile id (or None before __enter__)."""
        return self._profile_id

    def __enter__(self) -> Tuple[str, str]:
        profile_id = self._client.create_profile()
        launch_data = self._client.launch_profile(profile_id)
        webdriver_url = launch_data.get("webdriver")
        if not isinstance(webdriver_url, str) or not webdriver_url:
            raise RuntimeError("BitBrowser launch_profile response missing webdriver")
        self._profile_id = profile_id
        self._released = False
        return profile_id, webdriver_url

    def release_profile(self) -> None:
        """Return the BitBrowser profile to a clean state.

        Blueprint §7: "Trả Profile BitBrowser về trạng thái sạch".  MUST be
        called on every cycle end (success / abort / exception).  Idempotent
        — safe to call multiple times.  All best-effort: underlying client
        calls log and swallow network errors individually, and
        ``_released`` is set in a ``finally`` block so a transient failure
        never causes a re-release on a subsequent call.
        """
        if self._released:
            return
        if self._profile_id is None:
            self._released = True
            return
        profile_id = self._profile_id
        try:
            # Order: close the browser process first so no in-flight state is
            # clobbered, then delete the profile from the pool.  Each step
            # is already best-effort inside the client wrappers.
            try:
                self._client.close_profile(profile_id)
            except (urllib.error.URLError, OSError) as exc:
                _log.warning(
                    "Best-effort BitBrowser close_profile failed for %s: %s",
                    profile_id, exc)
            try:
                self._client.delete_profile(profile_id)
            except (urllib.error.URLError, OSError) as exc:
                _log.warning(
                    "Best-effort BitBrowser delete_profile failed for %s: %s",
                    profile_id, exc)
        finally:
            self._released = True

    def __exit__(self, exc_type, exc_value, exc_tb) -> bool:
        self.release_profile()
        return False


def get_bitbrowser_client() -> Optional[BitBrowserClient]:
    """Return BitBrowserClient if env vars set and endpoint reachable, else None.

    Blueprint §2.1: when ``BITBROWSER_POOL_MODE`` is truthy ("1"/"true"/"yes")
    returns a :class:`BitBrowserPoolClient` built from ``BITBROWSER_PROFILE_IDS``
    (CSV) instead. Legacy behaviour is preserved for all other values.
    """
    api_key = os.getenv("BITBROWSER_API_KEY")
    if not api_key:
        return None
    endpoint = os.getenv("BITBROWSER_ENDPOINT", "http://127.0.0.1:54345")
    pool_mode = os.getenv("BITBROWSER_POOL_MODE", "0").strip().lower()
    if pool_mode in ("1", "true", "yes"):
        ids_raw = os.getenv("BITBROWSER_PROFILE_IDS", "")
        profile_ids = [pid.strip() for pid in ids_raw.split(",") if pid.strip()]
        if not profile_ids:
            raise RuntimeError(
                "BITBROWSER_POOL_MODE=1 but BITBROWSER_PROFILE_IDS is empty. "
                "Add a CSV of profile IDs to .env (Blueprint §2.1)."
            )
        return BitBrowserPoolClient(
            endpoint=endpoint,
            api_key=api_key,
            profile_ids=profile_ids,
        )
    client = BitBrowserClient(endpoint=endpoint, api_key=api_key)
    if not client.is_available():
        return None
    return client


class BitBrowserPoolClient(BitBrowserClient):
    """Pool-mode BitBrowser client — round-robin sequential, thread-safe.

    Blueprint §2.1: uses a pre-created profile pool and randomises
    fingerprints per cycle via ``/browser/update/partial``. Avoids the
    legacy ``create → delete`` flow which is blocked by BitBrowser's
    Operation Password prompt.

    Inherits :class:`BitBrowserClient` for shared ``_post``/scheme validation
    helpers; overrides ``launch_profile`` to use the pool-mode endpoint.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        profile_ids: List[str],
        acquire_timeout_s: float = 60.0,
        poll_interval_s: float = 0.5,
    ):
        if not profile_ids:
            raise ValueError(
                "BITBROWSER_POOL_MODE=1 requires BITBROWSER_PROFILE_IDS "
                "to be a non-empty CSV list."
            )
        super().__init__(endpoint=endpoint, api_key=api_key)
        # Copy to avoid caller mutation affecting pool order.
        self._pool: List[str] = list(profile_ids)
        self._cursor: int = 0
        self._busy: set = set()
        self._lock = threading.Lock()
        self._acquire_timeout_s = acquire_timeout_s
        self._poll_interval_s = poll_interval_s
        _log.info(
            "BitBrowserPoolClient initialised with %d profiles",
            len(self._pool),
        )

    def acquire_profile(self) -> str:
        """Thread-safe round-robin sequential pick.

        Returns a profile id marked as BUSY. Caller MUST call
        :meth:`release_profile` in a ``finally`` block.

        Raises:
            RuntimeError: if no profile becomes AVAILABLE within
                ``acquire_timeout_s``, or if the pool is empty.
        """
        deadline = time.time() + self._acquire_timeout_s
        while True:
            with self._lock:
                n = len(self._pool)
                if n == 0:
                    raise RuntimeError(
                        "Profile pool is empty (all profiles may have been "
                        "evicted due to 404 from BitBrowser)."
                    )
                for offset in range(n):
                    idx = (self._cursor + offset) % n
                    pid = self._pool[idx]
                    if pid not in self._busy:
                        self._busy.add(pid)
                        self._cursor = (idx + 1) % n
                        _log.debug(
                            "acquired profile=%s cursor=%d busy=%d",
                            pid, self._cursor, len(self._busy),
                        )
                        return pid
            if time.time() > deadline:
                raise RuntimeError(
                    f"All {n} profiles BUSY for > "
                    f"{self._acquire_timeout_s}s; cannot acquire."
                )
            time.sleep(self._poll_interval_s)

    def release_profile(self, profile_id: str) -> None:
        """Return a profile to the pool (called from a ``finally`` block).

        Best-effort closes the browser window (no delete), then always
        clears the BUSY flag regardless of close errors.
        """
        try:
            self._close_browser(profile_id)
        except Exception as exc:  # pylint: disable=broad-except
            _log.warning("close_browser failed for %s: %s", profile_id, exc)
        finally:
            with self._lock:
                self._busy.discard(profile_id)
                _log.debug(
                    "released profile=%s busy=%d",
                    profile_id, len(self._busy),
                )

    def randomize_fingerprint(self, profile_id: str) -> None:
        """POST ``/browser/update/partial`` to randomise an existing profile.

        Raises:
            RuntimeError: if the profile is not found (HTTP 404). The
                profile is evicted from the pool before the error is raised.
        """
        payload = {
            "ids": [profile_id],
            "browserFingerPrint": {
                "batchRandom": True,
                "batchUpdateFingerPrint": True,
            },
        }
        try:
            self._post("/browser/update/partial", payload, timeout=10)
            _log.info("fingerprint randomised for %s", profile_id)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._evict_profile(profile_id)
                raise RuntimeError(
                    f"Profile {profile_id} not found (404) — "
                    f"evicted from pool."
                ) from exc
            raise

    def launch_profile(self, profile_id: str) -> Dict[str, object]:
        """POST ``/browser/open`` → ``{"webdriver": ..., ...}``."""
        data = self._post("/browser/open", {"id": profile_id}, timeout=30)
        if not isinstance(data, dict):
            raise RuntimeError("BitBrowser /browser/open returned non-dict")
        return data

    def _close_browser(self, profile_id: str) -> None:
        """POST ``/browser/close`` (no delete)."""
        self._post("/browser/close", {"id": profile_id}, timeout=10)

    def _evict_profile(self, profile_id: str) -> None:
        """Remove a 404 profile from the pool at runtime."""
        with self._lock:
            if profile_id in self._pool:
                self._pool.remove(profile_id)
                self._busy.discard(profile_id)
                if self._cursor >= len(self._pool) and self._pool:
                    self._cursor = 0
                _log.error(
                    "evicted profile %s; pool size=%d",
                    profile_id, len(self._pool),
                )
