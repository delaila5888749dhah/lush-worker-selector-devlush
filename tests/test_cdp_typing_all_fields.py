"""Phase 3A Task 1 — CDP send_keys migration for all §5 text fields.

Verifies that every checkout/billing text field routes through
``modules.cdp.keyboard.type_value`` (CDP ``Input.dispatchKeyEvent``)
instead of Selenium-native ``WebElement.send_keys`` (which emits
``isTrusted=False`` events and increases anti-fraud risk).

Audit findings: [C3] / [G3].
"""
from __future__ import annotations

import os
import unittest
import warnings
from unittest.mock import MagicMock, patch

from modules.cdp.driver import (
    GivexDriver,
    SEL_BILLING_ADDRESS,
    SEL_BILLING_CITY,
    SEL_BILLING_PHONE,
    SEL_BILLING_ZIP,
    SEL_GUEST_EMAIL,
)
from modules.common.types import BillingProfile, CardInfo, WorkerTask


def _make_driver():
    d = MagicMock()
    d.current_url = "https://example.com/payment"
    d.find_elements.return_value = [MagicMock()]
    body_el = MagicMock()
    body_el.text = ""
    d.find_element.return_value = body_el
    return d


def _make_billing() -> BillingProfile:
    return BillingProfile(
        first_name="Jane",
        last_name="Doe",
        address="123 Main St",
        city="Portland",
        state="OR",
        zip_code="97201",
        phone="5035550100",
        email="jane@example.com",
        country="US",
    )


def _make_card() -> CardInfo:
    return CardInfo(
        card_number="4111111111111111",
        exp_month="12",
        exp_year="2027",
        cvv="123",
        card_name="Jane Doe",
    )


def _make_task() -> WorkerTask:
    return WorkerTask(
        recipient_email="r@example.com",
        amount=50,
        primary_card=_make_card(),
        order_queue=(),
    )


class GuestEmailRoutesThroughCDP(unittest.TestCase):
    """Task 1 §5 entry-point: select_guest_checkout uses CDP for email."""

    def test_guest_email_uses_cdp_dispatch_key_event(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.driver._type_value") as spy, \
             patch.object(gd, "_wait_for_element", return_value=True), \
             patch.object(gd, "_wait_for_url"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_field_value_length", return_value=len("guest@example.com")), \
             patch("time.sleep"):
            gd.select_guest_checkout("guest@example.com")
        self.assertTrue(spy.called, "type_value (CDP dispatchKeyEvent) must be invoked")
        # The 3rd positional arg of type_value is the value being typed.
        called_values = [c.args[2] for c in spy.call_args_list]
        self.assertIn("guest@example.com", called_values)


class BillingFieldsRouteThroughCDP(unittest.TestCase):
    """Task 1 §5 fields: address / city / zip / phone all use CDP keyboard."""

    def _run_fill(self, gd):
        with patch.object(gd, "_cdp_select_option"), \
             patch.object(gd, "_sm"), \
             patch("time.sleep"):
            gd.fill_payment_and_billing(_make_card(), _make_billing())

    def test_billing_address_uses_cdp(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.driver._type_value") as spy:
            self._run_fill(gd)
        called_pairs = [(c.args[2],) for c in spy.call_args_list]
        self.assertIn(("123 Main St",), called_pairs)

    def test_billing_city_uses_cdp(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.driver._type_value") as spy:
            self._run_fill(gd)
        called = [c.args[2] for c in spy.call_args_list]
        self.assertIn("Portland", called)

    def test_billing_zip_uses_cdp(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.driver._type_value") as spy:
            self._run_fill(gd)
        called = [c.args[2] for c in spy.call_args_list]
        self.assertIn("97201", called)

    def test_billing_phone_uses_cdp(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.driver._type_value") as spy:
            self._run_fill(gd)
        called = [c.args[2] for c in spy.call_args_list]
        self.assertIn("5035550100", called)


class AllSection5FieldsAvoidSendKeys(unittest.TestCase):
    """Parametrized regression: WebElement.send_keys NEVER called on §5 hot-path."""

    SECTION5_FIELDS = [
        ("guest_email", SEL_GUEST_EMAIL),
        ("billing_address", SEL_BILLING_ADDRESS),
        ("billing_city", SEL_BILLING_CITY),
        ("billing_zip", SEL_BILLING_ZIP),
        ("billing_phone", SEL_BILLING_PHONE),
    ]

    def test_all_section5_fields_use_cdp_dispatch_key_event(self):
        """Run the full payment fill flow; no element.send_keys should ever fire."""
        selenium = _make_driver()
        # Make a single shared element mock so we can assert globally.
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        gd = GivexDriver(selenium, strict=False)

        with patch("modules.cdp.driver._type_value") as spy, \
             patch.object(gd, "_cdp_select_option"), \
             patch.object(gd, "_wait_for_element", return_value=True), \
             patch.object(gd, "_wait_for_url"), \
             patch.object(gd, "bounding_box_click"), \
             patch.object(gd, "_field_value_length", return_value=len("guest@example.com")), \
             patch.object(gd, "_sm"), \
             patch("time.sleep"):
            gd.select_guest_checkout("guest@example.com")
            gd.fill_payment_and_billing(_make_card(), _make_billing())

        # Assert send_keys NEVER invoked.
        element.send_keys.assert_not_called()
        # All §5 string values were observed on the CDP keyboard path.
        all_typed = "\n".join(c.args[2] for c in spy.call_args_list)
        for label, _sel in self.SECTION5_FIELDS:
            if label == "guest_email":
                self.assertIn("guest@example.com", all_typed)
            elif label == "billing_address":
                self.assertIn("123 Main St", all_typed)
            elif label == "billing_city":
                self.assertIn("Portland", all_typed)
            elif label == "billing_zip":
                self.assertIn("97201", all_typed)
            elif label == "billing_phone":
                self.assertIn("5035550100", all_typed)


class CdpTypeFieldDeprecation(unittest.TestCase):
    """The legacy ``_cdp_type_field`` is deprecated and gated by env var."""

    def test_cdp_type_field_strict_mode_raises(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch.dict(os.environ, {"ENFORCE_CDP_TYPING_STRICT": "1"}):
            with self.assertRaises(RuntimeError) as ctx:
                gd._cdp_type_field("#x", "value")
        self.assertIn("strict mode", str(ctx.exception))

    def test_cdp_type_field_non_strict_deprecation_warning(self):
        selenium = _make_driver()
        gd = GivexDriver(selenium, strict=False)
        with patch.dict(os.environ, {"ENFORCE_CDP_TYPING_STRICT": "0"}):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                gd._cdp_type_field("#x", "value")
            deprecation = [x for x in w if issubclass(x.category, DeprecationWarning)]
            self.assertTrue(
                deprecation,
                f"expected DeprecationWarning, got: {[(x.category, str(x.message)) for x in w]}",
            )


if __name__ == "__main__":
    unittest.main()
