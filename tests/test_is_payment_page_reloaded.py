"""Unit tests for ``integration.orchestrator.is_payment_page_reloaded``.

These tests pin the URL-match-based detection that replaces the prior
billing-field-empty heuristic. The billing-empty heuristic is retained
as an opt-in (``check_billing_empty=True``) belt-and-suspenders check.

Issue: [CRITICAL] ``is_payment_page_reloaded`` should use URL match, not
billing-field heuristic — false negatives (skip refill) risk double-charge.
"""
# pylint: disable=protected-access
import unittest
from unittest.mock import MagicMock, PropertyMock

from integration.orchestrator import (
    _payment_url_matches,
    is_payment_page_reloaded,
)
from modules.cdp.driver import URL_PAYMENT


def _driver(current_url: str, *, billing_value: str | None = None,
            find_elements_raises: bool = False):
    """Build a minimal Selenium-like mock driver.

    Args:
        current_url: Value returned by ``driver.current_url``.
        billing_value: When None → no billing elements. When str → a single
            element whose ``get_attribute('value')`` returns that string.
        find_elements_raises: When True, ``find_elements`` raises.
    """
    drv = MagicMock()
    drv.current_url = current_url
    if find_elements_raises:
        drv.find_elements.side_effect = Exception("DOM not ready")
        return drv
    if billing_value is None:
        drv.find_elements.return_value = []
    else:
        el = MagicMock()
        el.get_attribute.return_value = billing_value
        drv.find_elements.return_value = [el]
    return drv


class TestPaymentUrlMatches(unittest.TestCase):
    """Low-level URL comparison helper."""

    def test_exact_match(self):
        self.assertTrue(_payment_url_matches(URL_PAYMENT, URL_PAYMENT))

    def test_ignores_query_string(self):
        self.assertTrue(_payment_url_matches(URL_PAYMENT + "?t=12345", URL_PAYMENT))

    def test_ignores_fragment(self):
        self.assertTrue(_payment_url_matches(URL_PAYMENT + "#section", URL_PAYMENT))

    def test_trailing_slash_normalised(self):
        self.assertTrue(_payment_url_matches(URL_PAYMENT + "/", URL_PAYMENT))

    def test_host_case_insensitive(self):
        upper = URL_PAYMENT.replace("givex.com", "GIVEX.COM")
        self.assertTrue(_payment_url_matches(upper, URL_PAYMENT))

    def test_different_path_rejected(self):
        other = URL_PAYMENT.replace("payment.html", "checkout.html")
        self.assertFalse(_payment_url_matches(other, URL_PAYMENT))

    def test_different_host_rejected(self):
        other = URL_PAYMENT.replace("wwws-usa2.givex.com", "evil.example.com")
        self.assertFalse(_payment_url_matches(other, URL_PAYMENT))

    def test_empty_inputs(self):
        self.assertFalse(_payment_url_matches("", URL_PAYMENT))
        self.assertFalse(_payment_url_matches(URL_PAYMENT, ""))


class TestIsPaymentPageReloadedUrlMatch(unittest.TestCase):
    """Primary URL-match signal (default behaviour, check_billing_empty=False)."""

    def test_on_payment_url_returns_true(self):
        drv = _driver(URL_PAYMENT)
        self.assertTrue(is_payment_page_reloaded(drv))

    def test_on_payment_url_with_query_returns_true(self):
        drv = _driver(URL_PAYMENT + "?error=vv")
        self.assertTrue(is_payment_page_reloaded(drv))

    def test_off_payment_url_returns_false(self):
        """Key anti-regression: URL mismatch → False even when billing is empty.

        Previously, the billing-empty heuristic returned True here, causing
        a refill to fire on an unrelated page.
        """
        drv = _driver("https://wwws-usa2.givex.com/cws4.0/lushusa/e-gifts/checkout.html",
                      billing_value="")
        self.assertFalse(is_payment_page_reloaded(drv))

    def test_url_mismatch_does_not_consult_billing_field(self):
        """When URL does not match, billing field must not be queried."""
        drv = _driver("https://example.com/other")
        is_payment_page_reloaded(drv)
        drv.find_elements.assert_not_called()

    def test_empty_current_url_returns_false(self):
        """An empty current_url (about:blank-ish) is not the payment page."""
        drv = _driver("")
        self.assertFalse(is_payment_page_reloaded(drv))

    def test_stale_billing_value_still_detects_reload_via_url(self):
        """Anti-regression for the false-negative → double-charge risk.

        The old heuristic returned False here (billing value present) and
        skipped the refill. URL-match now returns True and the refill runs.
        """
        drv = _driver(URL_PAYMENT, billing_value="123 Main St")
        self.assertTrue(is_payment_page_reloaded(drv))

    def test_current_url_exception_returns_true_conservative(self):
        """If current_url access raises, stay conservative (refill rather than skip)."""
        drv = MagicMock()
        type(drv).current_url = PropertyMock(side_effect=Exception("transition"))
        self.assertTrue(is_payment_page_reloaded(drv))


class TestIsPaymentPageReloadedBillingOptIn(unittest.TestCase):
    """Opt-in legacy billing-empty heuristic (check_billing_empty=True)."""

    def test_url_match_and_empty_billing_returns_true(self):
        drv = _driver(URL_PAYMENT, billing_value="")
        self.assertTrue(is_payment_page_reloaded(drv, check_billing_empty=True))

    def test_url_match_no_billing_element_returns_true(self):
        drv = _driver(URL_PAYMENT, billing_value=None)  # empty find_elements
        self.assertTrue(is_payment_page_reloaded(drv, check_billing_empty=True))

    def test_url_match_but_populated_billing_returns_false(self):
        drv = _driver(URL_PAYMENT, billing_value="123 Main St")
        self.assertFalse(is_payment_page_reloaded(drv, check_billing_empty=True))

    def test_url_mismatch_short_circuits_regardless_of_billing(self):
        drv = _driver("https://example.com/other", billing_value="")
        self.assertFalse(is_payment_page_reloaded(drv, check_billing_empty=True))
        drv.find_elements.assert_not_called()

    def test_find_elements_exception_returns_true_conservative(self):
        drv = _driver(URL_PAYMENT, find_elements_raises=True)
        self.assertTrue(is_payment_page_reloaded(drv, check_billing_empty=True))


if __name__ == "__main__":
    unittest.main()
