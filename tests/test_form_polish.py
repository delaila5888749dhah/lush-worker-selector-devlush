"""PR-4 M1/M2/M3/M7 — Form-fill polish tests."""
import unittest
from unittest.mock import MagicMock

from modules.cdp import driver as cdp_driver


class GreetingPoolTests(unittest.TestCase):
    def test_greeting_random_from_pool_size_ge_12(self):
        """Greeting pool must contain ≥ 12 unique messages (M1)."""
        self.assertGreaterEqual(len(cdp_driver._GREETINGS), 12)
        self.assertEqual(
            len(cdp_driver._GREETINGS),
            len(set(cdp_driver._GREETINGS)),
            "_GREETINGS must contain unique messages",
        )

    def test_random_greeting_returns_pool_entry(self):
        for _ in range(50):
            g = cdp_driver._random_greeting()
            self.assertIn(g, cdp_driver._GREETINGS)


class RecipientSelectorsTests(unittest.TestCase):
    def test_recipient_name_uses_billing_fullname(self):
        """M2: #cws_txt_gcBuyTo and #cws_txt_gcBuyFrom get billing full name."""
        self.assertEqual(cdp_driver.SEL_RECIPIENT_NAME, "#cws_txt_gcBuyTo")
        self.assertEqual(cdp_driver.SEL_SENDER_NAME, "#cws_txt_gcBuyFrom")

    def test_confirm_email_selector(self):
        """M3: #cws_txt_confRecipEmail is the confirmation selector."""
        self.assertEqual(
            cdp_driver.SEL_CONFIRM_RECIPIENT_EMAIL, "#cws_txt_confRecipEmail",
        )

    def test_checkout_chain_selectors(self):
        """M7: selectors for the cart → guest checkout chain."""
        self.assertEqual(cdp_driver.SEL_BEGIN_CHECKOUT, "#cws_btn_cartCheckout")
        self.assertEqual(cdp_driver.SEL_GUEST_EMAIL, "#cws_txt_guestEmail")
        self.assertEqual(cdp_driver.SEL_GUEST_CONTINUE, "#cws_btn_guestChkout")


class FormFillInvocationTests(unittest.TestCase):
    def test_fill_egift_form_uses_full_billing_name(self):
        """fill_egift_form must pass billing.first_name + " " + last_name."""
        gd = cdp_driver.GivexDriver.__new__(cdp_driver.GivexDriver)
        gd._sm = None
        gd._persona = None
        gd._cursor = None
        gd._driver = MagicMock()
        calls = []

        def fake_type(sel, text, **kw):
            calls.append((sel, text))

        gd._realistic_type_field = fake_type
        gd._smooth_scroll_to = lambda sel: None

        task = MagicMock()
        task.amount = 25
        task.recipient_email = "alice@example.com"
        billing = MagicMock()
        billing.first_name = "Alice"
        billing.last_name = "Smith"

        gd.fill_egift_form(task, billing)
        by_sel = dict(calls)
        self.assertEqual(by_sel[cdp_driver.SEL_RECIPIENT_NAME], "Alice Smith")
        self.assertEqual(by_sel[cdp_driver.SEL_SENDER_NAME], "Alice Smith")
        # M3: confirm-email must match recipient email exactly.
        self.assertEqual(
            by_sel[cdp_driver.SEL_CONFIRM_RECIPIENT_EMAIL],
            "alice@example.com",
        )
        self.assertEqual(
            by_sel[cdp_driver.SEL_RECIPIENT_EMAIL],
            "alice@example.com",
        )
        # Amount field must be filled with str(task.amount).
        self.assertEqual(by_sel[cdp_driver.SEL_AMOUNT_INPUT], "25")


if __name__ == "__main__":
    unittest.main()
