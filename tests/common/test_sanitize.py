"""Unit tests for modules/common/sanitize.py.

Covers every PII category supported by the canonical sanitiser:

* PANs — 13-digit, 15-digit, 16-digit, 19-digit; bare / spaced / dashed
* CVVs — keyword patterns; bare digits adjacent to a redacted PAN
* Email addresses
* Redis URL credentials
* Edge-cases — clean strings, multiple PII types, overlapping patterns
"""

import unittest

from modules.common.sanitize import sanitize_error, sanitize_redis_url


# ── PAN tests ────────────────────────────────────────────────────────────────

class PAN16DigitTests(unittest.TestCase):
    """16-digit PAN in bare, spaced, and dashed forms."""

    def test_bare_16_digit_pan(self):
        msg = "Card 4111111111111111 was declined"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111111", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_spaced_16_digit_pan(self):
        msg = "Card 4111 1111 1111 1111 was declined"
        result = sanitize_error(msg)
        self.assertNotIn("4111 1111 1111 1111", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_dashed_16_digit_pan(self):
        msg = "Card 4111-1111-1111-1111 was declined"
        result = sanitize_error(msg)
        self.assertNotIn("4111-1111-1111-1111", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_mastercard_bare(self):
        msg = "MC 5500005555555559 charged"
        result = sanitize_error(msg)
        self.assertNotIn("5500005555555559", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_discover_bare(self):
        msg = "Discover 6011111111111117 used"
        result = sanitize_error(msg)
        self.assertNotIn("6011111111111117", result)
        self.assertIn("[REDACTED-CARD]", result)


class PAN13DigitTests(unittest.TestCase):
    """13-digit PAN (bare)."""

    def test_bare_13_digit_pan(self):
        msg = "Card 4111111111111 declined"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_13_digit_pan_at_end_of_string(self):
        msg = "PAN=4111111111111"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111", result)
        self.assertIn("[REDACTED-CARD]", result)


class PAN15DigitTests(unittest.TestCase):
    """15-digit PAN (Amex-style: bare, spaced 4-6-5, dashed 4-6-5)."""

    def test_bare_15_digit_pan(self):
        msg = "Amex 411111111111111 processed"
        result = sanitize_error(msg)
        self.assertNotIn("411111111111111", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_spaced_15_digit_amex(self):
        msg = "Amex 4111 111111 11111 processed"
        result = sanitize_error(msg)
        self.assertNotIn("4111 111111 11111", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_dashed_15_digit_amex(self):
        msg = "Amex 4111-111111-11111 processed"
        result = sanitize_error(msg)
        self.assertNotIn("4111-111111-11111", result)
        self.assertIn("[REDACTED-CARD]", result)


class PAN19DigitTests(unittest.TestCase):
    """19-digit PAN (bare, spaced 4-4-4-4-3, dashed 4-4-4-4-3)."""

    def test_bare_19_digit_pan(self):
        msg = "Card 4111111111111111789 was used"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111111789", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_spaced_19_digit_pan(self):
        msg = "Card 4111 1111 1111 1111 789 was used"
        result = sanitize_error(msg)
        self.assertNotIn("4111 1111 1111 1111 789", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_dashed_19_digit_pan(self):
        msg = "Card 4111-1111-1111-1111-789 was used"
        result = sanitize_error(msg)
        self.assertNotIn("4111-1111-1111-1111-789", result)
        self.assertIn("[REDACTED-CARD]", result)


# ── CVV tests ────────────────────────────────────────────────────────────────

class CVVKeywordTests(unittest.TestCase):
    """CVV keyword-based patterns."""

    def test_cvv_equals(self):
        msg = "cvv=123 was rejected"
        result = sanitize_error(msg)
        self.assertNotIn("cvv=123", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_cvv_equals_with_spaces(self):
        msg = "CVV = 9876 mismatch"
        result = sanitize_error(msg)
        self.assertNotIn("9876", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_cvv_colon_separator(self):
        msg = "Field cvv: 321 was rejected"
        result = sanitize_error(msg)
        self.assertNotIn("321", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_cvv_space_only(self):
        msg = "card cvv 654 invalid"
        result = sanitize_error(msg)
        self.assertNotIn("654", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_cvv_hyphen_separator(self):
        msg = "header cvv-2345 declined"
        result = sanitize_error(msg)
        self.assertNotIn("2345", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_cvv_underscore_separator(self):
        """cvv_NNN has no word boundary after 'cvv' (underscore is a word char);
        the pattern intentionally does not match this form."""
        msg = "cvv_777 bad"
        result = sanitize_error(msg)
        # \bcvv\b will not match before '_' — not redacted, and that is correct.
        self.assertEqual(result, msg)

    def test_cvv_case_insensitive(self):
        msg = "CVV=999 error"
        result = sanitize_error(msg)
        self.assertNotIn("999", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_cvv_4_digits(self):
        msg = "cvv=1234 mismatch"
        result = sanitize_error(msg)
        self.assertNotIn("1234", result)
        self.assertIn("[REDACTED-CVV]", result)


class CVVAdjacentToPANTests(unittest.TestCase):
    """Bare CVV digits that immediately follow a PAN."""

    def test_bare_cvv_after_bare_pan(self):
        # "4111111111111111 123" — the trailing " 123" (space + 3 digits) is
        # consumed by the PAN regex as a 19-digit 4-4-4-4-3 pattern.
        # Only [REDACTED-CARD] is produced; no separate CVV token is needed.
        msg = "4111111111111111 123"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111111", result)
        self.assertNotIn("123", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_bare_cvv_comma_after_pan(self):
        # Comma is NOT a PAN separator, so the CVV survives the PAN pass and
        # is then caught by _CVV_POST_PAN_RE.
        msg = "4111111111111111,123"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111111", result)
        self.assertNotIn("123", result)
        self.assertIn("[REDACTED-CARD]", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_bare_cvv_comma_separated_after_pan(self):
        msg = "pan=4111111111111111,cvv=456"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111111", result)
        self.assertNotIn("456", result)
        self.assertIn("[REDACTED-CARD]", result)
        self.assertIn("[REDACTED-CVV]", result)


# ── Email tests ───────────────────────────────────────────────────────────────

class EmailTests(unittest.TestCase):
    """Email address redaction."""

    def test_simple_email(self):
        msg = "User user@example.com submitted payment"
        result = sanitize_error(msg)
        self.assertNotIn("user@example.com", result)
        self.assertIn("[REDACTED-EMAIL]", result)

    def test_admin_email(self):
        msg = "Sent to admin@corp.com"
        result = sanitize_error(msg)
        self.assertNotIn("admin@corp.com", result)
        self.assertIn("[REDACTED-EMAIL]", result)

    def test_email_with_plus(self):
        msg = "Address: user+tag@domain.org"
        result = sanitize_error(msg)
        self.assertNotIn("user+tag@domain.org", result)
        self.assertIn("[REDACTED-EMAIL]", result)

    def test_subdomain_email(self):
        msg = "Contact a.b@mail.example.co.uk failed"
        result = sanitize_error(msg)
        self.assertNotIn("a.b@mail.example.co.uk", result)
        self.assertIn("[REDACTED-EMAIL]", result)


# ── Redis URL credential tests ────────────────────────────────────────────────

class RedisCredentialTests(unittest.TestCase):
    """Redis URL credential redaction via sanitize_error."""

    def test_redis_url_password_in_message(self):
        msg = "Connection redis://alice:s3cr3t@cache.example.com:6379/0 failed"
        result = sanitize_error(msg)
        self.assertNotIn("s3cr3t", result)
        self.assertIn("[REDACTED-REDIS-CREDS]", result)
        # Scheme and host remain for debugging
        self.assertIn("alice:", result)
        self.assertIn("cache.example.com", result)

    def test_rediss_url_password_in_message(self):
        msg = "TLS error rediss://user:p@ssw0rd@host:6380/1"
        result = sanitize_error(msg)
        self.assertNotIn("p@ssw0rd", result)
        self.assertIn("[REDACTED-REDIS-CREDS]", result)

    def test_redis_url_no_password_unchanged(self):
        msg = "Connected to redis://cache.example.com:6379/0"
        result = sanitize_error(msg)
        self.assertEqual(result, msg)


class SanitizeRedisUrlTests(unittest.TestCase):
    """sanitize_redis_url() dedicated function."""

    def test_redacts_password(self):
        url = "redis://alice:s3cr3t@cache.example.com:6379/0"
        result = sanitize_redis_url(url)
        self.assertNotIn("s3cr3t", result)
        self.assertIn("[REDACTED]", result)
        self.assertIn("alice:", result)
        self.assertIn("cache.example.com", result)

    def test_no_password_returned_unchanged(self):
        url = "redis://cache.example.com:6379/0"
        result = sanitize_redis_url(url)
        self.assertEqual(result, url)

    def test_ipv4_host(self):
        url = "redis://user:pass@192.168.1.1:6379"
        result = sanitize_redis_url(url)
        self.assertNotIn("pass", result)
        self.assertIn("192.168.1.1", result)

    def test_ipv6_host(self):
        # IPv6 URLs require bracket notation: redis://user:secret@[::1]:6379
        url = "redis://user:secret@[::1]:6379"
        result = sanitize_redis_url(url)
        self.assertNotIn("secret", result)
        self.assertIn("[REDACTED]", result)

    def test_no_username(self):
        url = "redis://:s3cr3t@host:6379"
        result = sanitize_redis_url(url)
        self.assertNotIn("s3cr3t", result)
        self.assertIn("[REDACTED]", result)


# ── Edge-case tests ───────────────────────────────────────────────────────────

class EdgeCaseTests(unittest.TestCase):
    """Edge cases: no PII, multiple types, overlapping patterns."""

    def test_clean_string_unchanged(self):
        msg = "Connection refused to checkout endpoint"
        result = sanitize_error(msg)
        self.assertEqual(result, msg)

    def test_empty_string_unchanged(self):
        result = sanitize_error("")
        self.assertEqual(result, "")

    def test_multiple_pii_types_all_redacted(self):
        msg = "Card 5500005555555559 from admin@corp.com cvv=456"
        result = sanitize_error(msg)
        self.assertNotIn("5500005555555559", result)
        self.assertNotIn("admin@corp.com", result)
        self.assertNotIn("456", result)
        self.assertIn("[REDACTED-CARD]", result)
        self.assertIn("[REDACTED-EMAIL]", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_multiple_pans_all_redacted(self):
        msg = "Cards: 4111111111111111 and 5500005555555559"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111111", result)
        self.assertNotIn("5500005555555559", result)
        self.assertEqual(result.count("[REDACTED-CARD]"), 2)

    def test_pan_with_adjacent_cvv_and_email(self):
        msg = "4111 1111 1111 1111 123 user@example.com"
        result = sanitize_error(msg)
        self.assertNotIn("4111 1111 1111 1111", result)
        self.assertNotIn(" 123", result)
        self.assertNotIn("user@example.com", result)

    def test_short_digit_sequences_not_redacted(self):
        """5- to 12-digit sequences must NOT be treated as PANs."""
        msg = "Order 123456 timeout after 30 seconds"
        result = sanitize_error(msg)
        self.assertEqual(result, msg)

    def test_number_at_word_boundary_not_overly_redacted(self):
        """14-digit sequences embedded in longer digit runs are correctly handled."""
        msg = "Ref 12345678901234 OK"
        result = sanitize_error(msg)
        # 14 digits → not a supported PAN length, must NOT be redacted
        self.assertNotIn("[REDACTED-CARD]", result)
        self.assertIn("12345678901234", result)

    def test_non_digit_context_not_affected(self):
        msg = "User logged in at 10:30 from IP 192.168.0.1"
        result = sanitize_error(msg)
        self.assertEqual(result, msg)

    def test_pan_with_redis_url(self):
        msg = "Card 4111111111111111 via redis://u:pwd@host:6379"
        result = sanitize_error(msg)
        self.assertNotIn("4111111111111111", result)
        self.assertNotIn("pwd", result)
        self.assertIn("[REDACTED-CARD]", result)
        self.assertIn("[REDACTED-REDIS-CREDS]", result)


if __name__ == "__main__":
    unittest.main()
