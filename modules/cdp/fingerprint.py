"""BitBrowser client utilities for per-worker fingerprint lifecycle."""

from __future__ import annotations

import logging
import os
from uuid import uuid4

import requests

_logger = logging.getLogger(__name__)


class BitBrowserClient:
    """Thin HTTP client for BitBrowser profile APIs."""

    def __init__(self, endpoint: str, api_key: str):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._headers = {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        }

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def _response_data(self, response: requests.Response) -> dict:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        if isinstance(payload, dict):
            return payload
        raise RuntimeError("BitBrowser API returned non-dict JSON payload")

    def create_profile(self) -> str:
        payload = {
            "platform": "windows",
            "name": f"worker-{uuid4().hex[:8]}",
        }
        try:
            response = requests.post(
                self._url("/api/v1/browser/create"),
                json=payload,
                headers=self._headers,
                timeout=10,
            )
            response.raise_for_status()
            data = self._response_data(response)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError("BitBrowser create_profile request failed") from exc
        profile_id = data.get("id") or data.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id:
            raise RuntimeError("BitBrowser create_profile response missing profile id")
        return profile_id

    def launch_profile(self, profile_id: str) -> dict:
        try:
            response = requests.post(
                self._url("/api/v1/browser/open"),
                json={"id": profile_id},
                headers=self._headers,
                timeout=30,
            )
            response.raise_for_status()
            data = self._response_data(response)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError("BitBrowser launch_profile request failed") from exc
        if not isinstance(data, dict):
            raise RuntimeError("BitBrowser launch_profile response payload must be dict")
        return data

    def close_profile(self, profile_id: str) -> None:
        try:
            response = requests.post(
                self._url("/api/v1/browser/close"),
                json={"id": profile_id},
                headers=self._headers,
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover
            _logger.warning("BitBrowser close_profile failed for %s: %s", profile_id, exc)

    def delete_profile(self, profile_id: str) -> None:
        try:
            response = requests.post(
                self._url("/api/v1/browser/delete"),
                json={"id": profile_id},
                headers=self._headers,
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover
            _logger.warning("BitBrowser delete_profile failed for %s: %s", profile_id, exc)

    def is_available(self) -> bool:
        try:
            response = requests.get(
                self._url("/api/v1/browser/list"),
                headers=self._headers,
                timeout=2,
            )
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException:
            return False


class BitBrowserSession:
    """Context manager for BitBrowser profile lifecycle."""

    def __init__(self, client: BitBrowserClient):
        self._client = client
        self._profile_id: str | None = None

    def __enter__(self) -> tuple[str, str]:
        profile_id = self._client.create_profile()
        launch_data = self._client.launch_profile(profile_id)
        webdriver_url = launch_data.get("webdriver")
        if not isinstance(webdriver_url, str) or not webdriver_url:
            raise RuntimeError("BitBrowser launch_profile response missing webdriver")
        self._profile_id = profile_id
        return profile_id, webdriver_url

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._profile_id is None:
            return False
        try:
            self._client.close_profile(self._profile_id)
        except Exception:  # pragma: no cover
            pass
        try:
            self._client.delete_profile(self._profile_id)
        except Exception:  # pragma: no cover
            pass
        return False


def get_bitbrowser_client() -> BitBrowserClient | None:
    """Return available BitBrowser client if env config is present, else None."""
    api_key = os.getenv("BITBROWSER_API_KEY")
    if not api_key:
        return None
    endpoint = os.getenv("BITBROWSER_ENDPOINT", "http://127.0.0.1:54345")
    client = BitBrowserClient(endpoint=endpoint, api_key=api_key)
    if not client.is_available():
        return None
    return client
