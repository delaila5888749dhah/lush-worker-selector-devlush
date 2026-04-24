"""Phase 6 Task 3 — FileTaskLoader parser field validation.

Tests that card number, expiry month/year, and CVV fields are validated by
regex in ``_make_card`` and that malformed lines are skipped by
``_parse_line`` without leaking raw PAN digits into log messages.
"""
from __future__ import annotations

import logging
import os
import tempfile
import unittest

from integration.task_loader import FileTaskLoader, _make_card


class MakeCardValidationTests(unittest.TestCase):
    def test_accepts_15_digit_card(self):
        card = _make_card(["341111111111111", "01", "2030", "1234"])
        self.assertEqual(card.card_number, "341111111111111")

    def test_accepts_16_digit_card(self):
        card = _make_card(["4111111111111111", "12", "2030", "123"])
        self.assertEqual(card.card_number, "4111111111111111")

    def test_accepts_card_with_spaces_and_dashes(self):
        card = _make_card(["4111-1111 1111-1111", "12", "2030", "123"])
        self.assertEqual(card.card_number, "4111111111111111")

    def test_rejects_14_digit_card(self):
        digits = "4" * 14
        with self.assertRaises(ValueError) as ctx:
            _make_card([digits, "01", "2030", "123"])
        # Privacy: message must NOT contain the raw digits.
        self.assertNotIn(digits, str(ctx.exception))
        self.assertIn("14", str(ctx.exception))  # length summary ok

    def test_rejects_17_digit_card(self):
        digits = "4" * 17
        with self.assertRaises(ValueError) as ctx:
            _make_card([digits, "01", "2030", "123"])
        self.assertNotIn(digits, str(ctx.exception))

    def test_rejects_non_digit_card(self):
        with self.assertRaises(ValueError):
            _make_card(["4111-ABCD-1111-1111", "01", "2030", "123"])

    def test_rejects_exp_month_00(self):
        with self.assertRaises(ValueError):
            _make_card(["4111111111111111", "00", "2030", "123"])

    def test_rejects_exp_month_13(self):
        with self.assertRaises(ValueError):
            _make_card(["4111111111111111", "13", "2030", "123"])

    def test_accepts_exp_month_1_and_01(self):
        self.assertEqual(_make_card(["4111111111111111", "1", "2030", "123"]).exp_month, "01")
        self.assertEqual(_make_card(["4111111111111111", "01", "2030", "123"]).exp_month, "01")

    def test_accepts_exp_year_yy(self):
        card = _make_card(["4111111111111111", "01", "30", "123"])
        self.assertEqual(card.exp_year, "30")

    def test_accepts_exp_year_yyyy(self):
        card = _make_card(["4111111111111111", "01", "2030", "123"])
        self.assertEqual(card.exp_year, "2030")

    def test_rejects_exp_year_3_digits(self):
        with self.assertRaises(ValueError):
            _make_card(["4111111111111111", "01", "203", "123"])

    def test_rejects_cvv_2_digits(self):
        with self.assertRaises(ValueError) as ctx:
            _make_card(["4111111111111111", "01", "2030", "12"])
        # CVV itself must not be echoed either.
        self.assertNotIn("12", str(ctx.exception).replace("3-4", ""))

    def test_rejects_cvv_5_digits(self):
        with self.assertRaises(ValueError):
            _make_card(["4111111111111111", "01", "2030", "12345"])

    def test_rejects_non_digit_cvv(self):
        with self.assertRaises(ValueError):
            _make_card(["4111111111111111", "01", "2030", "abc"])


class ParseLineSkipMalformedTests(unittest.TestCase):
    def _write(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_skip_malformed_line_continues_loading(self):
        bad_pan = "4" * 14
        path = self._write(
            "good@example.com|100|4111111111111111|01|2030|123\n"
            f"bad@example.com|100|{bad_pan}|01|2030|123\n"
            "good2@example.com|200|4222222222222222|02|2031|456\n"
        )
        loader = FileTaskLoader(file_path=path)
        with self.assertLogs("integration.task_loader", level="WARNING") as cm:
            t1 = loader.get_task("w1")
            t2 = loader.get_task("w1")
            t3 = loader.get_task("w1")
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        self.assertIsNone(t3)
        self.assertEqual(t1.recipient_email, "good@example.com")
        self.assertEqual(t2.recipient_email, "good2@example.com")
        # Privacy: raw 14-digit PAN must not appear in the log output.
        full_log = "\n".join(cm.output)
        self.assertNotIn(bad_pan, full_log)
        # But a warning about the skip should be present.
        self.assertTrue(
            any("skipping line" in msg.lower() for msg in cm.output),
            msg=f"Expected a 'skipping line' warning; got: {cm.output}",
        )

    def test_multiple_bad_lines_all_skipped(self):
        path = self._write(
            "a@x.com|10|123|01|2030|123\n"          # 3-digit card
            "b@x.com|10|4111111111111111|13|2030|123\n"  # bad month
            "c@x.com|10|4111111111111111|01|2030|12\n"   # short cvv
            "good@x.com|10|4111111111111111|01|2030|123\n"
        )
        loader = FileTaskLoader(file_path=path)
        # Silence logger for this case but verify only the good task returned.
        logging.getLogger("integration.task_loader").setLevel(logging.CRITICAL)
        try:
            task = loader.get_task("w1")
            self.assertIsNotNone(task)
            self.assertEqual(task.recipient_email, "good@x.com")
            self.assertIsNone(loader.get_task("w1"))
        finally:
            logging.getLogger("integration.task_loader").setLevel(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
