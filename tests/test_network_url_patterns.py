"""Tests for _CDP_NETWORK_URL_PATTERNS narrowing (Phase 4 audit [F2]).

The broad ``cws4.0`` fallback is intentionally retained until live DevTools
inspection of the Givex checkout page confirms the precise pricing endpoint
path.  When a URL matches *only* the fallback (and none of the precise
patterns), the response listener logs a WARNING so inflated callback rates
remain observable.
"""
import unittest

from integration import orchestrator


class TestNetworkUrlPatterns(unittest.TestCase):

    def test_precise_patterns_defined(self):
        """Precise-subset constant exists and excludes the broad fallback."""
        precise = orchestrator._CDP_NETWORK_URL_PATTERNS_PRECISE  # pylint: disable=protected-access
        self.assertIn("/checkout/total", precise)
        self.assertIn("/api/tax", precise)
        self.assertIn("/api/checkout", precise)
        self.assertNotIn("cws4.0", precise,
                         "cws4.0 is a broad fallback, not a precise endpoint")

    def test_network_url_patterns_accepts_pricing_xhr(self):
        """A URL containing a precise pricing path matches."""
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/api/checkout/total"
        self.assertTrue(
            any(p in url for p in orchestrator._CDP_NETWORK_URL_PATTERNS_PRECISE),
        )

    def test_network_url_patterns_rejects_nonpricing_xhr_via_precise(self):
        """Non-pricing XHR (static asset) does not match any precise pattern.

        The broad ``cws4.0`` fallback still matches this URL — by design —
        but the listener logs a WARNING and the precise filter rejects it.
        """
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/static/app.js"
        self.assertFalse(
            any(p in url for p in orchestrator._CDP_NETWORK_URL_PATTERNS_PRECISE),
            "static asset must not match precise pricing patterns",
        )

    def test_url_matching_only_broad_fallback_is_flagged(self):
        """After P3-F2 fix (option A), broad == precise; static asset matches neither."""
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/static/app.js"
        broad_match = any(p in url for p in orchestrator._CDP_NETWORK_URL_PATTERNS)
        precise_match = any(p in url for p in orchestrator._CDP_NETWORK_URL_PATTERNS_PRECISE)
        self.assertFalse(broad_match, "broad pattern no longer matches non-pricing XHR (cws4.0 removed)")
        self.assertFalse(precise_match, "precise pattern rejects non-pricing XHR")


if __name__ == "__main__":
    unittest.main()
