"""Tests for _CDP_NETWORK_URL_PATTERNS narrowing (Phase 4 audit [F2]).

After live DevTools verification of the Givex checkout pricing endpoints,
the broad ``cws4.0`` fallback was removed and the precise/broad tuples were
merged into a single ``_CDP_NETWORK_URL_PATTERNS`` tuple.
"""
import unittest

from integration import orchestrator


class TestNetworkUrlPatterns(unittest.TestCase):

    def test_patterns_defined(self):
        """Patterns constant exists and contains only precise pricing endpoints."""
        patterns = orchestrator._CDP_NETWORK_URL_PATTERNS  # pylint: disable=protected-access
        self.assertIn("/checkout/total", patterns)
        self.assertIn("/api/tax", patterns)
        self.assertIn("/api/checkout", patterns)
        self.assertNotIn("cws4.0", patterns,
                         "cws4.0 broad fallback must be removed")

    def test_precise_alias_removed(self):
        """The legacy precise-subset alias must be gone after cleanup."""
        self.assertFalse(
            hasattr(orchestrator, "_CDP_NETWORK_URL_PATTERNS_PRECISE"),
            "_CDP_NETWORK_URL_PATTERNS_PRECISE should be removed; patterns merged",
        )

    def test_network_url_patterns_accepts_pricing_xhr(self):
        """A URL containing a pricing path matches."""
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/api/checkout/total"
        self.assertTrue(
            any(p in url for p in orchestrator._CDP_NETWORK_URL_PATTERNS),
        )

    def test_network_url_patterns_rejects_nonpricing_xhr(self):
        """Non-pricing XHR (static asset) does not match any pattern."""
        url = "https://wwws-usa2.givex.com/cws4.0/lushusa/static/app.js"
        self.assertFalse(
            any(p in url for p in orchestrator._CDP_NETWORK_URL_PATTERNS),
            "static asset must not match pricing patterns",
        )


if __name__ == "__main__":
    unittest.main()
