"""Phase 6 Task 2 — `_make_profile_id` full SHA-256 (64-char) contract.

Blueprint §12 line 703 says "SHA-256 hash".  Prior implementation truncated
to 16 hex characters; this test suite locks in the full-digest format.
"""
from __future__ import annotations

import hashlib
import re
import unittest

from integration.orchestrator import _make_profile_id
from modules.common.types import BillingProfile


def _profile(first="Alice", last="Smith", zip_code="90210") -> BillingProfile:
    return BillingProfile(
        first_name=first,
        last_name=last,
        address="1 Main St",
        city="Beverly Hills",
        state="CA",
        zip_code=zip_code,
        phone="5555555555",
        email="x@example.com",
    )


class ProfileIdHashTests(unittest.TestCase):
    def test_profile_id_is_64_hex_chars(self):
        pid = _make_profile_id(_profile())
        self.assertEqual(len(pid), 64)
        self.assertRegex(pid, r"^[0-9a-f]{64}$")

    def test_profile_id_deterministic(self):
        p1 = _profile("Alice", "Smith", "90210")
        p2 = _profile("Alice", "Smith", "90210")
        self.assertEqual(_make_profile_id(p1), _make_profile_id(p2))

    def test_profile_id_matches_sha256_of_canonical_tuple(self):
        raw = "Alice|Smith|90210".encode("utf-8")
        expected = hashlib.sha256(raw).hexdigest()
        self.assertEqual(_make_profile_id(_profile("Alice", "Smith", "90210")), expected)

    def test_profile_id_changes_with_inputs(self):
        a = _make_profile_id(_profile("Alice", "Smith", "90210"))
        b = _make_profile_id(_profile("Bob", "Smith", "90210"))
        c = _make_profile_id(_profile("Alice", "Jones", "90210"))
        d = _make_profile_id(_profile("Alice", "Smith", "10001"))
        self.assertEqual(len({a, b, c, d}), 4)

    def test_profile_id_no_truncation_regression(self):
        """Guard against re-introduction of `.hexdigest()[:16]` slicing."""
        pid = _make_profile_id(_profile())
        self.assertNotRegex(pid, r"^[0-9a-f]{16}$")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", pid))


if __name__ == "__main__":
    unittest.main()
