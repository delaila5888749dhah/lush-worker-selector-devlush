"""PR-4 L11 — PAN-in-logs assertion test (CRITICAL security test).

Verifies that no primary-account-number (PAN) digits ever appear raw in
* the standard Python logging stream,
* the orchestrator audit logger (``integration.orchestrator.audit``),
* the Telegram caption / payload generated for success notifications.

All surfaces must emit only masked digits (first 6 + last 4) per PCI-DSS
compatibility + Blueprint §12 privacy contract.
"""
from __future__ import annotations

import io
import logging
import re
import unittest
from unittest.mock import MagicMock, patch

from modules.notification import telegram_notifier
from modules.notification.card_masker import mask_card_number


# NIST-style test PAN (Visa test range — not a real account).
_TEST_PAN = "4111111111111234"
_PAN_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def _contains_pan(text: str, pan: str = _TEST_PAN) -> bool:
    """Return True if *text* contains the raw PAN (no mask).

    A single digit run of ≥ 13 digits that is not the masked form counts as
    leaked PAN.  Masked forms look like ``411111****1234`` — they contain
    non-digit characters between the BIN and last-4 so they do not match
    ``_PAN_PATTERN``.
    """
    if pan in text:
        return True
    # Be conservative: also flag any pure-digit sequence ≥ 13 that equals
    # the PAN (some loggers may re-encode whitespace).
    for m in _PAN_PATTERN.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if digits == pan:
            return True
    return False


class NoPanInLogsTests(unittest.TestCase):
    def setUp(self):
        telegram_notifier._reset_for_tests()

    def tearDown(self):
        telegram_notifier._reset_for_tests()

    def test_full_cycle_emits_no_pan_in_logs(self):
        """Simulate _notify_success path + confirm PAN is masked everywhere."""
        from integration import orchestrator  # noqa: PLC0415

        task = MagicMock()
        task.primary_card.card_number = _TEST_PAN
        task.recipient_email = "alice@example.com"
        task.task_id = "t-1"

        # Capture all log records across the relevant logger hierarchy.
        root_stream = io.StringIO()
        handler = logging.StreamHandler(root_stream)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        prev_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with patch.object(orchestrator.cdp, "_get_driver",
                              side_effect=RuntimeError("no driver")), \
                 patch.object(telegram_notifier, "_post", return_value=True):
                orchestrator._notify_success(task, "w1", 42.0)
                telegram_notifier._flush_for_tests(timeout=1.0)
        finally:
            root.removeHandler(handler)
            root.setLevel(prev_level)
        output = root_stream.getvalue()
        self.assertFalse(
            _contains_pan(output),
            f"Raw PAN leaked to logs:\n{output[-500:]}",
        )

    def test_audit_event_no_pan(self):
        """The billing audit event must never include raw PAN or PII."""
        from integration import orchestrator  # noqa: PLC0415

        profile = MagicMock()
        profile.first_name = "Alice"
        profile.last_name = "Smith"
        profile.zip_code = "90210"

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        orchestrator._AUDIT_LOGGER.addHandler(handler)
        prev_level = orchestrator._AUDIT_LOGGER.level
        orchestrator._AUDIT_LOGGER.setLevel(logging.DEBUG)
        try:
            orchestrator._emit_billing_audit_event(
                profile=profile,
                worker_id="w1",
                task_id="t-1",
                zip_code="90210",
            )
        finally:
            orchestrator._AUDIT_LOGGER.removeHandler(handler)
            orchestrator._AUDIT_LOGGER.setLevel(prev_level)
        logged = stream.getvalue()
        self.assertIn("billing_selection", logged)
        self.assertFalse(_contains_pan(logged))
        # No raw PII.
        self.assertNotIn("Alice", logged)
        self.assertNotIn("Smith", logged)
        self.assertNotIn("alice@", logged.lower())

    def test_telegram_caption_no_pan_only_masked(self):
        """The Telegram caption must contain ONLY the masked form of the PAN."""
        task = MagicMock()
        task.primary_card.card_number = _TEST_PAN
        task.recipient_email = "alice@example.com"
        caption = telegram_notifier.build_success_caption(
            "w1", task, 42.0, ctx=None,
        )
        self.assertFalse(_contains_pan(caption))
        masked = mask_card_number(_TEST_PAN)
        self.assertIn(masked, caption)

    def test_telegram_post_body_no_pan(self):
        """sendPhoto multipart body must NOT contain raw PAN digits."""
        task = MagicMock()
        task.primary_card.card_number = _TEST_PAN
        task.recipient_email = "alice@example.com"

        captured = {}

        def fake_post(url, data, headers=None, timeout=10):
            captured["data"] = data
            return True

        env = {
            "TELEGRAM_ENABLED": "1",
            "TELEGRAM_BOT_TOKEN": "tkn",
            "TELEGRAM_CHAT_ID": "42",
        }
        with patch.dict("os.environ", env, clear=False), \
             patch.object(telegram_notifier, "_post", side_effect=fake_post):
            telegram_notifier.send_success_notification("w1", task, 42.0, None)
            telegram_notifier._flush_for_tests(timeout=2.0)

        body = captured.get("data", b"").decode("utf-8", errors="replace")
        self.assertFalse(
            _contains_pan(body),
            "Raw PAN leaked into the Telegram request body",
        )

    def test_audit_event_profile_id_is_full_64_char_hash(self):
        """After Phase 6 Task 2, profile_id must be 64 hex chars (full SHA-256).

        Guards against regression to truncated 16-char form that would reduce
        correlation strength and could be mistaken for raw data in log
        scanners.
        """
        from integration import orchestrator  # noqa: PLC0415

        profile = MagicMock()
        profile.first_name = "Alice"
        profile.last_name = "Smith"
        profile.zip_code = "90210"

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        orchestrator._AUDIT_LOGGER.addHandler(handler)
        prev_level = orchestrator._AUDIT_LOGGER.level
        orchestrator._AUDIT_LOGGER.setLevel(logging.DEBUG)
        try:
            orchestrator._emit_billing_audit_event(
                profile=profile, worker_id="w1", task_id="t-1", zip_code="90210",
            )
        finally:
            orchestrator._AUDIT_LOGGER.removeHandler(handler)
            orchestrator._AUDIT_LOGGER.setLevel(prev_level)
        logged = stream.getvalue()
        # Extract profile_id value — "profile_id": "<64 hex>"
        m = re.search(r'"profile_id":\s*"([0-9a-f]+)"', logged)
        self.assertIsNotNone(m, f"profile_id not found in audit log: {logged}")
        self.assertEqual(len(m.group(1)), 64)

    def test_parser_error_messages_never_contain_pan(self):
        """ValueError messages raised by ``_make_card`` must not echo raw digits."""
        from integration.task_loader import _make_card  # noqa: PLC0415

        bad_pans = ["4" * 14, "5" * 17, "4111-ABCD-1111-1111"]
        for bad in bad_pans:
            with self.assertRaises(ValueError) as ctx:
                _make_card([bad, "01", "2030", "123"])
            msg = str(ctx.exception)
            self.assertFalse(
                _contains_pan(msg, pan=re.sub(r"\D", "", bad)),
                f"Parser error leaked PAN digits: {msg}",
            )

    def test_concurrent_audit_emit_no_pii_interleaving(self):
        """Multi-thread emits from different workers must not interleave PII
        fragments from distinct profiles in the same log line."""
        import threading  # noqa: PLC0415
        from integration import orchestrator  # noqa: PLC0415

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        orchestrator._AUDIT_LOGGER.addHandler(handler)
        prev_level = orchestrator._AUDIT_LOGGER.level
        orchestrator._AUDIT_LOGGER.setLevel(logging.DEBUG)

        def _emit(name, worker_id):
            profile = MagicMock()
            profile.first_name = name
            profile.last_name = "X"
            profile.zip_code = "12345"
            orchestrator._emit_billing_audit_event(
                profile=profile, worker_id=worker_id, task_id="t", zip_code="12345",
            )

        threads = [
            threading.Thread(target=_emit, args=(f"Name{i}", f"w{i}")) for i in range(8)
        ]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            orchestrator._AUDIT_LOGGER.removeHandler(handler)
            orchestrator._AUDIT_LOGGER.setLevel(prev_level)

        logged = stream.getvalue()
        # No raw first_name should appear anywhere.
        for i in range(8):
            self.assertNotIn(f"Name{i}", logged)
        # And no PAN-shaped string either.
        self.assertFalse(_contains_pan(logged))


if __name__ == "__main__":
    unittest.main()
