import collections
import logging
import os
import random
import threading
import uuid
from pathlib import Path

from modules.common.exceptions import CycleExhaustedError
from modules.common.types import BillingProfile

_lock = threading.Lock()
_profiles: "collections.deque[BillingProfile]" = collections.deque()
_logger = logging.getLogger(__name__)

_EMAIL_DOMAINS = ("gmail.com", "yahoo.com", "outlook.com", "icloud.com")
_PHONE_FIRST_DIGITS = "23456789"
_PHONE_OTHER_DIGITS = "0123456789"


def _pool_dir():
    override = os.getenv("BILLING_POOL_DIR", "").strip()
    if override:
        if "\x00" in override:
            _logger.warning("BILLING_POOL_DIR contains null bytes; using default billing_pool.")
            return Path(__file__).resolve().parents[2] / "billing_pool"
        resolved = Path(override).resolve()
        project_root = Path(__file__).resolve().parents[2]
        allowed_prefixes = (project_root, Path("/data"), Path("/tmp"))
        if not any(
            resolved == prefix or str(resolved).startswith(str(prefix) + os.sep)
            for prefix in allowed_prefixes
        ):
            _logger.warning(
                "BILLING_POOL_DIR '%s' is outside allowed prefixes %s; using default billing_pool.",
                resolved,
                [str(p) for p in allowed_prefixes],
            )
            return Path(__file__).resolve().parents[2] / "billing_pool"
        return resolved
    return Path(__file__).resolve().parents[2] / "billing_pool"


def _reset_state():
    global _profiles
    with _lock:
        _profiles = collections.deque()


def _normalize_zip(zip_code):
    if zip_code is None:
        return ""
    if isinstance(zip_code, bool):
        raise ValueError("zip_code must be str or int")
    if isinstance(zip_code, (int, str)):
        return str(zip_code).strip()
    raise ValueError("zip_code must be str or int")


def _parse_profile_line(line):
    cleaned = line.strip()
    if not cleaned:
        return None
    parts = [part.strip() for part in cleaned.split("|")]
    if len(parts) < 6:
        return None
    while len(parts) < 8:
        parts.append("")
    first, last, address, city, state, zip_code, phone, email = parts[:8]
    if not (first and last and address and city and state and zip_code):
        return None
    return BillingProfile(
        first_name=first,
        last_name=last,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
        phone=phone or None,
        email=email or None,
    )


def _read_profiles_from_disk():
    """Read and parse profiles from disk. Must be called without holding _lock."""
    pool_dir = _pool_dir()
    profiles = []
    if pool_dir.is_dir():
        for path in sorted(pool_dir.glob("*.txt")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                profile = _parse_profile_line(line)
                if profile is not None:
                    profiles.append(profile)
    random.shuffle(profiles)
    return collections.deque(profiles)


def _generate_phone():
    first = random.choice(_PHONE_FIRST_DIGITS)
    rest = "".join(random.choice(_PHONE_OTHER_DIGITS) for _ in range(9))
    return f"{first}{rest}"


def _generate_email(first_name=None, last_name=None):
    # Parameters unused intentionally; UUID token prevents PII leakage via name-derived emails
    token = uuid.uuid4().hex[:8]
    domain = random.choice(_EMAIL_DOMAINS)
    return f"user{token}@{domain}"


def _find_matching_index(zip_code):
    """Find the index of the first profile matching *zip_code*.

    .. warning::
        This function reads module-level ``_profiles`` without acquiring
        ``_lock``.  It **MUST** only be called while the caller already holds
        ``_lock``.
    """
    if not zip_code:
        return None
    for index, profile in enumerate(_profiles):
        if _normalize_zip(profile.zip_code) == zip_code:
            return index
    return None


def _fill_missing(profile):
    phone = profile.phone or _generate_phone()
    email = profile.email or _generate_email(profile.first_name, profile.last_name)
    return BillingProfile(
        first_name=profile.first_name,
        last_name=profile.last_name,
        address=profile.address,
        city=profile.city,
        state=profile.state,
        zip_code=profile.zip_code,
        phone=phone,
        email=email,
    )


def select_profile(zip_code):
    global _profiles
    normalized_zip = _normalize_zip(zip_code)

    # Fast path: check if pool is already loaded before doing any I/O
    with _lock:
        needs_load = not _profiles

    if needs_load:
        # Do filesystem I/O outside the lock so other threads are not blocked.
        # Multiple threads may load concurrently on a cold start; the redundant
        # work is intentional — it trades a small amount of extra disk reads for
        # significantly lower lock contention on large pools.
        loaded = _read_profiles_from_disk()
        # Publish under the lock; double-check in case another thread loaded first
        with _lock:
            if not _profiles and loaded:
                _profiles = loaded

    with _lock:
        if not _profiles:
            pool_dir = _pool_dir()
            exists = pool_dir.is_dir()
            raise CycleExhaustedError(
                f"No billing profiles available in billing pool directory '{pool_dir}' "
                f"(exists={exists})"
            )

        index = _find_matching_index(normalized_zip)
        if index is None:
            # Atomic queue rotation: popleft from front, enrich if needed, append to back
            profile = _profiles.popleft()
            if profile.phone is None or profile.email is None:
                profile = _fill_missing(profile)
            _profiles.append(profile)
            return profile

        profile = _profiles[index]
        if profile.phone is None or profile.email is None:
            profile = _fill_missing(profile)
            _profiles[index] = profile
        return profile
