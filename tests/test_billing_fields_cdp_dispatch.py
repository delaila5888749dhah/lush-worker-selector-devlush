"""P3-C3/G3 — Guest email + all billing text fields MUST type via CDP
``Input.dispatchKeyEvent`` (``isTrusted=true`` events), never via Selenium's
``send_keys`` (which is detectable by anti-fraud).

These tests patch the driver's ``execute_cdp_cmd`` / ``send_keys`` and
exercise the CDP driver end-to-end for guest checkout and
``fill_payment_and_billing``.  After Phase 3:

  * guest email      → _realistic_type_field → CDP dispatchKeyEvent
  * billing address  → _realistic_type_field → CDP dispatchKeyEvent
  * billing city     → _realistic_type_field → CDP dispatchKeyEvent
  * billing zip      → _realistic_type_field → CDP dispatchKeyEvent
  * billing phone    → _realistic_type_field → CDP dispatchKeyEvent
"""

import random
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver


def _build_driver(extra_side_effects=None):
    drv = MagicMock()
    drv.find_elements.return_value = [MagicMock()]
    drv.execute_script.return_value = None
    drv.current_url = "https://example.com/checkout"
    return drv


def _make_profile():
    return SimpleNamespace(
        email="guest@example.com",
        first_name="Alice",
        last_name="Smith",
        address="123 Main Street",
        country="US",
        state="CA",
        city="Los Angeles",
        zip_code="90001",
        phone="5551234567",
    )


def _make_card():
    return SimpleNamespace(
        card_name="Alice Smith",
        card_number="4111111111111111",
        exp_month="01",
        exp_year="2030",
        cvv="123",
    )


class TestBillingFieldsUseCDPDispatch(unittest.TestCase):
    def setUp(self):
        self.drv = _build_driver()
        self.gd = GivexDriver(self.drv, strict=False)
        self.gd._rnd = random.Random(0)
        # Avoid sleeping in the hesitation / burst pacing.
        self._sleep_patch = patch("modules.cdp.keyboard.time.sleep", return_value=None)
        self._sleep_patch.start()

    def tearDown(self):
        self._sleep_patch.stop()

    def _count_dispatch_key_events(self):
        return sum(
            1 for call in self.drv.execute_cdp_cmd.call_args_list
            if call.args and call.args[0] == "Input.dispatchKeyEvent"
        )

    def test_fill_payment_and_billing_uses_cdp_dispatch_only(self):
        """Billing text fields must dispatch CDP key events, never send_keys."""
        card = _make_card()
        profile = _make_profile()

        with patch.object(self.gd, "_cdp_select_option"):
            self.gd.fill_payment_and_billing(card, profile)

        # No send_keys on the element returned by find_elements in driver.py.
        el = self.drv.find_elements.return_value[0]
        el.send_keys.assert_not_called()

        # dispatchKeyEvent called >= 2 × len(value) for each typed field.
        # Count keyDown+keyUp pairs per char; at a minimum we expect the
        # sum of (address + city + zip + phone + card_name + card_number + cvv)
        # characters × 2 = (15 + 11 + 5 + 10 + 11 + 16 + 3) * 2 = 142 events.
        # Use a lenient lower bound that still proves every field was typed.
        total = sum(len(v) for v in (
            profile.address, profile.city, profile.zip_code, profile.phone,
            card.card_name, card.card_number, card.cvv,
        ))
        self.assertGreaterEqual(self._count_dispatch_key_events(), total * 2)

    def test_billing_without_phone_still_uses_cdp_dispatch(self):
        """Phone is optional; the other billing fields must still dispatch via CDP."""
        card = _make_card()
        profile = _make_profile()
        profile.phone = ""

        with patch.object(self.gd, "_cdp_select_option"):
            self.gd.fill_payment_and_billing(card, profile)

        el = self.drv.find_elements.return_value[0]
        el.send_keys.assert_not_called()
        # At least address (15 chars) worth of dispatchKeyEvent calls × 2.
        self.assertGreaterEqual(self._count_dispatch_key_events(), 15 * 2)


class TestGuestEmailUsesCDPDispatch(unittest.TestCase):
    def setUp(self):
        self.drv = _build_driver()
        self.gd = GivexDriver(self.drv, strict=False)
        self.gd._rnd = random.Random(0)
        self._sleep_patch = patch("modules.cdp.keyboard.time.sleep", return_value=None)
        self._sleep_patch.start()

    def tearDown(self):
        self._sleep_patch.stop()

    def test_select_guest_checkout_email_uses_cdp_dispatch(self):
        """select_guest_checkout must dispatch CDP events for the email field."""
        # Patch the internal waits + navigation so the test stays focused on the type path.
        with patch.object(self.gd, "_wait_for_url"), \
             patch.object(self.gd, "_wait_for_element", return_value=True), \
             patch.object(self.gd, "bounding_box_click"):
            self.gd.select_guest_checkout("guest@example.com")

        dispatch_calls = [
            call for call in self.drv.execute_cdp_cmd.call_args_list
            if call.args and call.args[0] == "Input.dispatchKeyEvent"
        ]
        # 'guest@example.com' = 17 chars × 2 events (keyDown + keyUp) each.
        self.assertGreaterEqual(len(dispatch_calls), 17 * 2)
        self.drv.find_elements.return_value[0].send_keys.assert_not_called()


class TestNoSendKeysInModulesCDP(unittest.TestCase):
    """Codebase grep test — production CDP / integration code must not call
    Selenium's ``send_keys`` except in the two documented last-resort fallbacks
    (keyboard.py strict-fallback and driver.py's ``_cdp_type_field`` which is
    only reached when the keyboard helper is unavailable).
    """

    WHITELIST = {
        # modules/cdp/keyboard.py — strict-aware last-resort fallback when the
        # CDP Input.dispatchKeyEvent command itself fails mid-type.
        ("modules/cdp/keyboard.py", "el.send_keys(ch)"),
        # modules/cdp/driver.py — _cdp_type_field legacy helper kept as the
        # strict-fallback path of _realistic_type_field when the keyboard
        # helper is unavailable at import time.
        ("modules/cdp/driver.py", "el.send_keys(value)"),
    }

    def test_no_production_send_keys_outside_whitelist(self):
        import os
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir)
        )
        offenders = []
        for rel_dir in ("modules/cdp", "integration"):
            abs_dir = os.path.join(repo_root, rel_dir)
            for dirpath, _, filenames in os.walk(abs_dir):
                for fname in filenames:
                    if not fname.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, fname)
                    with open(path, encoding="utf-8") as fh:
                        for line in fh:
                            stripped = line.strip()
                            if "send_keys(" not in stripped:
                                continue
                            # Ignore comments and log strings (case-insensitive
                            # literal match on the substring).
                            if stripped.startswith("#"):
                                continue
                            if '"' in stripped or "'" in stripped:
                                # Quoted reference (log message / docstring) —
                                # not a real call.
                                if "el.send_keys" not in stripped:
                                    continue
                            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
                            if (rel, stripped) in self.WHITELIST:
                                continue
                            offenders.append((rel, stripped))
        self.assertEqual(
            offenders, [],
            f"Unexpected send_keys callers outside the whitelist: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
