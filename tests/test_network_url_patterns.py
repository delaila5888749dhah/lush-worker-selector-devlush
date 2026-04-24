"""Phase 4 [F2] — CDP Network URL pattern narrowing tests.

The broad substring ``"cws4.0"`` was removed because every XHR on the
Givex payment page contains it (static assets included).  Only narrow
path fragments should remain so that the first-notify-wins guard is not
inflated by non-pricing responses.
"""
from __future__ import annotations

import unittest

from integration import orchestrator


def _matches_any(url: str, patterns) -> bool:
    return any(p in url for p in patterns)


class TestCdpNetworkUrlPatterns(unittest.TestCase):
    def test_cws40_standalone_substring_removed(self):
        for pat in orchestrator._CDP_NETWORK_URL_PATTERNS:
            self.assertNotEqual(pat, "cws4.0")
            self.assertNotIn("cws4.0", pat)

    def test_static_asset_does_not_match(self):
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/static/app.js"
        self.assertFalse(
            _matches_any(url, orchestrator._CDP_NETWORK_URL_PATTERNS),
            f"Static asset should not match any pattern: {url!r}",
        )

    def test_pricing_endpoint_matches(self):
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/api/checkout/total"
        self.assertTrue(_matches_any(url, orchestrator._CDP_NETWORK_URL_PATTERNS))

    def test_tax_endpoint_matches(self):
        url = "https://example.com/api/tax/calculate"
        self.assertTrue(_matches_any(url, orchestrator._CDP_NETWORK_URL_PATTERNS))

    def test_generic_cws40_page_does_not_match(self):
        # Non-pricing page that still contains ``cws4.0``.
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/checkout.html"
        self.assertFalse(
            _matches_any(url, orchestrator._CDP_NETWORK_URL_PATTERNS),
        )


if __name__ == "__main__":
    unittest.main()
