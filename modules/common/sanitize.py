"""Canonical PII sanitisation helpers.

All production modules that need to redact PII from strings must import
from this module.  No inline sanitisers should exist outside this file.

Supported redaction categories:

* PANs (Payment Account Numbers):
  - 13-digit bare
  - 15-digit bare or 4-6-5 grouped (Amex-style)
  - 16-digit bare or 4-4-4-4 grouped
  - 19-digit bare or 4-4-4-4-3 grouped
  - Separators: bare (no separator), space-separated, dash-separated

* CVVs:
  - Keyword patterns: ``cvv=123``, ``CVV : 456``, ``cvv-789``, ``cvv 0123``
  - Bare 3–4-digit CVV immediately adjacent to a redacted PAN token

* Email addresses (RFC-5321 subset)

* Redis URL credentials (``redis[s]://user:password@host``)
"""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

# ── PAN (Payment Account Number) ─────────────────────────────────────────────
# Handles 13-, 15-, 16-, and 19-digit PANs in bare, space-separated, and
# dash-separated forms.
#
# Match order (longest first to avoid partial matches via (?!\d)):
#   1. 4-4-4-4-3 or 4-4-4-4 (16 or 19 digits) bare/spaced/dashed
#      The trailing (?:[ -]?\d{3})? makes the last group optional, so the
#      same branch covers both 16- and 19-digit cards.
#   2. 4-6-5 (15-digit Amex-style) bare/spaced/dashed
#   3. Bare 13-digit fallback
_PAN_RE = re.compile(
    r"(?<!\d)"
    r"(?:"
    # 4-4-4-4 (16 digits) or 4-4-4-4-3 (19 digits) — bare / spaced / dashed
    r"\d{4}(?:[ -]?\d{4}){3}(?:[ -]?\d{3})?"
    r"|"
    # 4-6-5 (15 digits, Amex-style) — bare / spaced / dashed
    r"\d{4}[ -]?\d{6}[ -]?\d{5}"
    r"|"
    # 13-digit bare
    r"\d{13}"
    r")"
    r"(?!\d)"
)

# ── CVV — keyword-based ───────────────────────────────────────────────────────
# Matches "cvv=123", "CVV : 456", "cvv-789", "cvv 0123", etc.
_CVV_KEYWORD_RE = re.compile(r"\bcvv\b[\s:=_-]*\d{3,4}\b", re.IGNORECASE)

# ── CVV — bare digits adjacent to a redacted PAN token ───────────────────────
# After PAN redaction, a bare 3–4 digit CVV may immediately follow the
# [REDACTED-CARD] token, e.g. "4111 1111 1111 1111 123" →
# "[REDACTED-CARD] 123".  This second pass catches those residual digits.
_CVV_POST_PAN_RE = re.compile(r"(?<=\[REDACTED-CARD\])[ ,;]?\d{3,4}(?!\d)")

# ── Email addresses ───────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# ── Redis URL credentials ─────────────────────────────────────────────────────
# Matches redis[s]://[user]:password@host and redacts the password portion.
_REDIS_CREDS_RE = re.compile(
    r"(rediss?://[^:@/\s]*:)[^@\s]+(@)",
    re.IGNORECASE,
)


def sanitize_error(msg: str) -> str:
    """Redact PII from a message string.

    Applies redaction in this order so that later passes can build on
    earlier results (e.g. Redis URLs are stripped before the email regex
    can mis-fire on URL credentials, and bare-CVV detection runs after
    PAN redaction):

    1. Redis URL credentials → ``[REDACTED-REDIS-CREDS]``
    2. PANs → ``[REDACTED-CARD]``
    3. Bare CVVs adjacent to a redacted PAN → ``[REDACTED-CVV]``
    4. Keyword CVV patterns (``cvv=123``) → ``[REDACTED-CVV]``
    5. Email addresses → ``[REDACTED-EMAIL]``

    Args:
        msg: The raw message that may contain PII.

    Returns:
        The message with all recognised PII replaced by placeholder tokens.
    """
    msg = _PAN_RE.sub("[REDACTED-CARD]", msg)
    msg = _CVV_POST_PAN_RE.sub("[REDACTED-CVV]", msg)
    msg = _CVV_KEYWORD_RE.sub("[REDACTED-CVV]", msg)
    msg = _REDIS_CREDS_RE.sub(r"\1[REDACTED-REDIS-CREDS]\2", msg)
    msg = _EMAIL_RE.sub("[REDACTED-EMAIL]", msg)
    return msg


def sanitize_redis_url(redis_url: str) -> str:
    """Redact credentials from a Redis URL before including it in logs.

    Only the password component is replaced; the username, host, port,
    path, and query string are preserved so that log entries remain
    useful for debugging connection issues.

    Args:
        redis_url: The raw Redis URL that may contain a password.

    Returns:
        The URL with the password replaced by ``[REDACTED]``, or the
        original string unchanged if no password is present.
    """
    parsed = urlsplit(redis_url)
    if not parsed.password:
        return redis_url
    host = parsed.hostname or ""
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            host = f"[{host}]"
    except ValueError:
        pass  # Not a valid IP address — regular hostname, no brackets needed.
    port = f":{parsed.port}" if parsed.port is not None else ""
    username = f"{parsed.username}:" if parsed.username else ":"
    safe_netloc = f"{username}[REDACTED]@{host}{port}"
    return urlunsplit(
        (parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment)
    )
