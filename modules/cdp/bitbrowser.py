"""BitBrowser local API client — launch/close browser profiles."""

import requests

BITBROWSER_API_BASE = "http://127.0.0.1:54345"


def launch_profile(profile_id: str, timeout: int = 30) -> dict:
    """Launch a BitBrowser profile and return its connection details.

    Args:
        profile_id: The BitBrowser profile identifier.
        timeout: HTTP request timeout in seconds.

    Returns:
        A dict containing at least::

            {
                "ws": {"selenium": "ws://..."},
                "http": "http://localhost:PORT",
            }

    Raises:
        RuntimeError: if the API call fails or returns a non-2xx status.
    """
    url = f"{BITBROWSER_API_BASE}/browser/open"
    try:
        response = requests.post(url, json={"id": profile_id}, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"BitBrowser launch_profile failed for profile {profile_id!r}: {exc}"
        ) from exc
    data = response.json()
    return data


def close_profile(profile_id: str, timeout: int = 10) -> None:
    """Close a running BitBrowser profile.

    Args:
        profile_id: The BitBrowser profile identifier.
        timeout: HTTP request timeout in seconds.
    """
    url = f"{BITBROWSER_API_BASE}/browser/close"
    try:
        response = requests.post(url, json={"id": profile_id}, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"BitBrowser close_profile failed for profile {profile_id!r}: {exc}"
        ) from exc


def get_debugger_address(profile_id: str) -> str:
    """Launch a profile and return its remote debugger address.

    The returned string is suitable for use as
    ``ChromeOptions.debugger_address``.

    Args:
        profile_id: The BitBrowser profile identifier.

    Returns:
        The debugger address as ``"127.0.0.1:PORT"`` (scheme-less).

    Raises:
        RuntimeError: if the launch fails or the ``http`` field is absent.
    """
    data = launch_profile(profile_id)
    http_addr = data.get("http", "")
    if not http_addr:
        raise RuntimeError(
            f"BitBrowser launch_profile response missing 'http' field: {data!r}"
        )
    # Strip scheme if present (e.g. "http://127.0.0.1:9222" -> "127.0.0.1:9222")
    if "://" in http_addr:
        http_addr = http_addr.split("://", 1)[1]
    return http_addr
