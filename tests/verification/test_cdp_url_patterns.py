"""Lock-in test for integration.orchestrator._CDP_NETWORK_URL_PATTERNS (U-05).

Asserts the current content of the constant so that drift is caught
immediately.  The patterns cover both /api/checkout/total (via
'/checkout/total' substring) and /api/tax (exact).
"""
# pylint: disable=protected-access,no-self-use
import unittest

from integration import orchestrator


class TestCDPNetworkUrlPatterns(unittest.TestCase):
    """U-05 lock-in: _CDP_NETWORK_URL_PATTERNS membership assertions."""

    def _patterns(self):
        """Return the module-level constant under test."""
        return orchestrator._CDP_NETWORK_URL_PATTERNS

    def test_constant_is_non_empty(self):
        """Constant must be populated."""
        self.assertTrue(self._patterns(), "_CDP_NETWORK_URL_PATTERNS must not be empty")

    def test_checkout_total_coverage(self):
        """'/checkout/total' is present; it matches /api/checkout/total as substring."""
        patterns = self._patterns()
        self.assertIn("/checkout/total", patterns,
                      "'/checkout/total' must be in _CDP_NETWORK_URL_PATTERNS "
                      "to cover /api/checkout/total endpoint")

    def test_api_tax_coverage(self):
        """'/api/tax' is present and covers the tax endpoint."""
        self.assertIn("/api/tax", self._patterns(),
                      "'/api/tax' must be in _CDP_NETWORK_URL_PATTERNS")

    def test_cws40_removed(self):
        """'cws4.0' must NOT be present (P3-F2 audit fix, option A)."""
        self.assertNotIn("cws4.0", self._patterns(),
                         "'cws4.0' broad fallback must be removed from "
                         "_CDP_NETWORK_URL_PATTERNS (P3-F2)")

    def test_api_checkout_coverage(self):
        """'/api/checkout' is present as redundant coverage for checkout endpoints."""
        self.assertIn("/api/checkout", self._patterns(),
                      "'/api/checkout' must be in _CDP_NETWORK_URL_PATTERNS")

    def test_exact_membership(self):
        """Full lock-in: assert exact set to catch any silent additions or removals."""
        expected = {"/checkout/total", "/api/tax", "/api/checkout"}
        self.assertEqual(set(self._patterns()), expected,
                         "_CDP_NETWORK_URL_PATTERNS membership changed — update "
                         "addendum-cdp-url-patterns.md and this test if intentional")


if __name__ == "__main__":
    unittest.main()
