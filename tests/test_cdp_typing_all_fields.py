"""Phase 3A Task 1 — All §5 text fields route via CDP Input.dispatchKeyEvent.

Verifies that ``select_guest_checkout`` and ``fill_payment_and_billing``
(and the legacy ``fill_billing``) never fall back to Selenium
``WebElement.send_keys`` for text input, and that ``_cdp_type_field`` is
properly deprecated (warns by default, raises under
``ENFORCE_CDP_TYPING_STRICT=1``).
"""
from __future__ import annotations

import os
import unittest
import warnings
from unittest.mock import MagicMock, patch

from modules.cdp.driver import (
    GivexDriver,
    SEL_BEGIN_CHECKOUT,
    SEL_GUEST_CONTINUE,
    SEL_GUEST_EMAIL,
    SEL_GUEST_HEADING,
)
from modules.common.exceptions import SelectorTimeoutError
from modules.common.types import BillingProfile, CardInfo, WorkerTask


def _mk_driver():
    d = MagicMock()
    d.find_elements.return_value = [MagicMock()]
    d.current_url = "https://example.com/checkout"
    return d


def _mk_billing():
    return BillingProfile(
        first_name="Jane",
        last_name="Doe",
        address="123 Main St",
        city="Springfield",
        state="IL",
        zip_code="62701",
        country="US",
        phone="2175551212",
        email="guest@example.com",
    )


def _mk_card():
    return CardInfo(
        card_number="4111111111111111",
        exp_month="12",
        exp_year="2030",
        cvv="123",
        card_name="Jane Doe",
    )


class Section5FieldsUseCDPDispatchKeyEventTests(unittest.TestCase):
    """All §5 text fields emit CDP Input.dispatchKeyEvent, never send_keys."""

    def _run_guest_checkout(self, gd, email):
        with patch("time.sleep"), patch.object(gd, "_wait_for_url"):
            gd.select_guest_checkout(email)

    def _run_payment_and_billing(self, gd, card, billing):
        with patch("time.sleep"), patch.object(gd, "_cdp_select_option"):
            gd.fill_payment_and_billing(card, billing)

    def test_guest_email_uses_cdp_dispatch_key_event(self):
        driver = _mk_driver()
        elements = {}

        def find(_by, sel):
            sel = sel.strip()
            el = elements.setdefault(sel, MagicMock())
            return [el]

        driver.find_elements.side_effect = find
        gd = GivexDriver(driver, strict=False)
        with patch("modules.cdp.driver._type_value") as mock_tv:
            self._run_guest_checkout(gd, "guest@example.com")
        # _type_value was called for the guest email field.
        tv_calls = [c for c in mock_tv.call_args_list]
        self.assertTrue(tv_calls, "keyboard.type_value must be invoked for guest email")
        email_el = elements[SEL_GUEST_EMAIL.strip()]
        # Selenium send_keys must not be used for the email field.
        email_el.send_keys.assert_not_called()

    def test_billing_address_uses_cdp(self):
        self._assert_billing_field_uses_cdp(lambda b: b.address)

    def test_billing_city_uses_cdp(self):
        self._assert_billing_field_uses_cdp(lambda b: b.city)

    def test_billing_zip_uses_cdp(self):
        self._assert_billing_field_uses_cdp(lambda b: b.zip_code)

    def test_billing_phone_uses_cdp(self):
        self._assert_billing_field_uses_cdp(lambda b: b.phone)

    def _assert_billing_field_uses_cdp(self, getter):
        driver = _mk_driver()
        element = MagicMock()
        driver.find_elements.return_value = [element]
        gd = GivexDriver(driver, strict=False)
        billing = _mk_billing()
        card = _mk_card()
        with patch("modules.cdp.driver._type_value") as mock_tv:
            self._run_payment_and_billing(gd, card, billing)
        typed_values = [c.args[2] for c in mock_tv.call_args_list if len(c.args) >= 3]
        self.assertIn(getter(billing), typed_values)
        element.send_keys.assert_not_called()

    def test_all_section5_fields_no_send_keys(self):
        """End-to-end: WebElement.send_keys is never called during §5 fill."""
        driver = _mk_driver()
        element = MagicMock()
        driver.find_elements.return_value = [element]
        gd = GivexDriver(driver, strict=False)
        billing = _mk_billing()
        card = _mk_card()
        with patch("modules.cdp.driver._type_value"), \
                patch("time.sleep"), \
                patch.object(gd, "_cdp_select_option"), \
                patch.object(gd, "_wait_for_url"):
            gd.select_guest_checkout(billing.email)
            gd.fill_payment_and_billing(card, billing)
        element.send_keys.assert_not_called()

    def test_fill_billing_legacy_uses_cdp(self):
        driver = _mk_driver()
        element = MagicMock()
        driver.find_elements.return_value = [element]
        gd = GivexDriver(driver, strict=False)
        billing = _mk_billing()
        with patch("modules.cdp.driver._type_value") as mock_tv, \
                patch("time.sleep"), \
                patch.object(gd, "_cdp_select_option"):
            gd.fill_billing(billing)
        typed_values = [c.args[2] for c in mock_tv.call_args_list if len(c.args) >= 3]
        for v in (billing.address, billing.city, billing.zip_code, billing.phone):
            self.assertIn(v, typed_values)
        element.send_keys.assert_not_called()


class CDPTypeFieldStrictModeTests(unittest.TestCase):
    """_cdp_type_field is deprecated; strict env var raises, non-strict warns."""

    def setUp(self):
        self._saved = os.environ.get("ENFORCE_CDP_TYPING_STRICT")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ENFORCE_CDP_TYPING_STRICT", None)
        else:
            os.environ["ENFORCE_CDP_TYPING_STRICT"] = self._saved

    def test_cdp_type_field_strict_mode_raises(self):
        os.environ["ENFORCE_CDP_TYPING_STRICT"] = "1"
        driver = _mk_driver()
        gd = GivexDriver(driver, strict=False)
        with self.assertRaises(RuntimeError):
            gd._cdp_type_field("#x", "value")  # pylint: disable=protected-access

    def test_cdp_type_field_non_strict_deprecation_warning(self):
        os.environ["ENFORCE_CDP_TYPING_STRICT"] = "0"
        driver = _mk_driver()
        element = MagicMock()
        driver.find_elements.return_value = [element]
        gd = GivexDriver(driver, strict=False)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            gd._cdp_type_field("#x", "value")  # pylint: disable=protected-access
        self.assertTrue(
            any(issubclass(x.category, DeprecationWarning) for x in w),
            f"Expected DeprecationWarning, got: {[str(x) for x in w]}",
        )
        element.send_keys.assert_called_once_with("value")

    def test_cdp_type_field_strict_raises_before_selector_lookup(self):
        """Strict mode raises immediately without touching the driver."""
        os.environ["ENFORCE_CDP_TYPING_STRICT"] = "1"
        driver = _mk_driver()
        driver.find_elements.side_effect = AssertionError(
            "strict mode must not touch the driver"
        )
        gd = GivexDriver(driver, strict=False)
        with self.assertRaises(RuntimeError):
            gd._cdp_type_field("#x", "value")  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
