"""Real BitBrowser smoke tests — gated behind BITBROWSER_API_KEY env var.

Skipped by default in CI. Run manually:
  BITBROWSER_API_KEY=xxx pytest tests/smoke/ -m real_browser -v
"""
import os

import pytest  # pylint: disable=import-error

pytestmark = [
    pytest.mark.real_browser,
    pytest.mark.skipif(
        not os.environ.get("BITBROWSER_API_KEY"),
        reason="Real BitBrowser smoke — requires BITBROWSER_API_KEY env var",
    ),
]


def test_real_create_open_close_delete_lifecycle():
    """Scaffold: create → open → close → delete a real BitBrowser profile."""
    pytest.skip("Scaffold — implement when running against a real BitBrowser API.")


def test_real_navigate_to_blank_page_via_cdp():
    """Scaffold: navigate to about:blank through the real CDP endpoint."""
    pytest.skip("Scaffold — implement when running against a real BitBrowser API.")


def test_real_proxy_assignment_visible_in_session():
    """Scaffold: verify the configured proxy is reflected in the live session."""
    pytest.skip("Scaffold — implement when running against a real BitBrowser API.")


def test_real_two_sessions_have_distinct_fingerprints():
    """Scaffold: two concurrent sessions expose distinct fingerprints."""
    pytest.skip("Scaffold — implement when running against a real BitBrowser API.")
