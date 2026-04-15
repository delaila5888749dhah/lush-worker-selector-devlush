"""BitBrowser client utilities for per-worker fingerprint lifecycle."""
# pylint: disable=duplicate-code

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple
from uuid import uuid4

_log = logging.getLogger(__name__)  # pylint: disable=invalid-name


class BitBrowserClient:
    """Thin HTTP client for BitBrowser profile APIs."""

    def __init__(self, endpoint: str, api_key: str):
        self._endpoint = endpoint.rstrip("/")
        scheme = urllib.parse.urlparse(self._endpoint).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"Unsupported endpoint scheme: {scheme!r}")
        self._api_key = api_key

    def _url(self, path: str) -> str:
        url = f"{self._endpoint}{path}"
        scheme = urllib.parse.urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"Unsupported URL scheme: {scheme!r}")
        return url

    def _post(self, path: str, payload: Dict[str, object],
              timeout: int = 10) -> Dict[str, object]:
        """POST JSON to the given API path and return parsed response dict."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(path),
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": self._api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = json.loads(resp.read().decode("utf-8"))
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            return body["data"]
        if isinstance(body, dict):
            return body
        raise RuntimeError("BitBrowser API returned non-dict JSON payload")

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
    __exit__: close_profile() → delete_profile() (best-effort, log errors)
    """

    def __init__(self, client: BitBrowserClient):
        self._client = client
        self._profile_id: Optional[str] = None

    def __enter__(self) -> Tuple[str, str]:
        profile_id = self._client.create_profile()
        launch_data = self._client.launch_profile(profile_id)
        webdriver_url = launch_data.get("webdriver")
        if not isinstance(webdriver_url, str) or not webdriver_url:
            raise RuntimeError("BitBrowser launch_profile response missing webdriver")
        self._profile_id = profile_id
        return profile_id, webdriver_url

    def __exit__(self, exc_type, exc_value, exc_tb) -> bool:
        if self._profile_id is None:
            return False
        try:
            self._client.close_profile(self._profile_id)
        except (urllib.error.URLError, OSError):
            pass
        try:
            self._client.delete_profile(self._profile_id)
        except (urllib.error.URLError, OSError):
            pass
        return False


def get_bitbrowser_client() -> Optional[BitBrowserClient]:
    """Return BitBrowserClient if env vars set and endpoint reachable, else None."""
    api_key = os.getenv("BITBROWSER_API_KEY")
    if not api_key:
        return None
    endpoint = os.getenv("BITBROWSER_ENDPOINT", "http://127.0.0.1:54345")
    client = BitBrowserClient(endpoint=endpoint, api_key=api_key)
    if not client.is_available():
        return None
    return client
