from __future__ import annotations

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


def _get_max_billing_profiles() -> int:
    default = 10000
    raw_value = os.getenv("MAX_BILLING_PROFILES", str(default))
    try:
        max_profiles = int(raw_value)
    except (TypeError, ValueError):
        _logger.warning(
            "Invalid MAX_BILLING_PROFILES value %r; using default %d.",
            raw_value,
            default,
        )
        return default
    if max_profiles < 1:
        _logger.warning(
            "MAX_BILLING_PROFILES must be at least 1; got %r. Using 1.",
            raw_value,
        )
        return 1
    return max_profiles


_MAX_BILLING_PROFILES = _get_max_billing_profiles()


def _get_min_billing_profiles() -> int:
    raw = os.getenv("MIN_BILLING_PROFILES", "0")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _logger.warning("Invalid MIN_BILLING_PROFILES %r; treating as 0.", raw)
        return 0
    return max(0, value)


_MIN_BILLING_PROFILES = _get_min_billing_profiles()


def _pool_dir() -> Path:
    raw = os.environ.get("BILLING_POOL_DIR")
    if raw is not None and not raw.strip():
        raise ValueError(
            "BILLING_POOL_DIR is set but contains only whitespace; "
            "provide a non-empty directory path."
        )
    override = raw.strip() if raw is not None else ""
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
                "BILLING_POOL_DIR resolves outside allowed prefixes %s;"
                " using default billing_pool.",
                [str(p) for p in allowed_prefixes],
            )
            return Path(__file__).resolve().parents[2] / "billing_pool"
        return resolved
    return Path(__file__).resolve().parents[2] / "billing_pool"


def _reset_state() -> None:
    global _profiles
    with _lock:
        _profiles = collections.deque()


def _normalize_zip(zip_code: str | int | None) -> str:
    if zip_code is None:
        return ""
    if isinstance(zip_code, bool):
        raise ValueError("zip_code must be str or int")
    if isinstance(zip_code, (int, str)):
        return str(zip_code).strip()
    raise ValueError("zip_code must be str or int")


def _parse_profile_line(line: str) -> BillingProfile | None:
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


def _read_profiles_from_disk() -> collections.deque[BillingProfile]:
    """Read and parse profiles from disk. Must be called without holding _lock.

    Cold-start concurrency note:
    Multiple threads may call this function simultaneously during cold-start.
    This is intentional — redundant disk reads are cheaper than holding _lock
    during I/O. The double-check publish-under-lock pattern in select_profile()
    ensures only the first loaded result is used.

    Each call creates a local Random instance for shuffle to avoid contending
    on the global random module's internal state lock under concurrent cold-start.
    """
    pool_dir = _pool_dir()
    profiles: list[BillingProfile] = []
    n_scanned = n_opened = n_lines = n_accepted = n_rejected = n_skipped = 0
    if pool_dir.is_dir():
        for path in sorted(pool_dir.glob("*.txt")):
            n_scanned += 1
            if len(profiles) >= _MAX_BILLING_PROFILES:
                break
            try:
                with path.open("r", encoding="utf-8") as handle:
                    n_opened += 1
                    for line in handle:
                        n_lines += 1
                        if len(profiles) >= _MAX_BILLING_PROFILES:
                            break
                        profile = _parse_profile_line(line)
                        if profile is not None:
                            n_accepted += 1
                            profiles.append(profile)
                        elif line.strip():
                            n_rejected += 1
            except UnicodeDecodeError as exc:
                n_skipped += 1
                _logger.warning("Skipping %s: decode error (%s).", path.name, exc)
            except OSError as exc:
                n_skipped += 1
                _logger.warning("Skipping %s: OS error (%s).", path.name, exc)
                continue
    _logger.info(
        "Billing pool load: scanned=%d opened=%d skipped=%d "
        "lines=%d accepted=%d rejected=%d pool=%d",
        n_scanned, n_opened, n_skipped, n_lines, n_accepted, n_rejected, len(profiles),
    )
    # Use a local RNG instance instead of the global random module.
    # This avoids shared global RNG state contention when multiple threads
    # call _read_profiles_from_disk() concurrently during cold-start.
    _local_rng = random.Random()
    _local_rng.shuffle(profiles)
    return collections.deque(profiles)


def _generate_phone() -> str:
    first = random.choice(_PHONE_FIRST_DIGITS)
    rest = "".join(random.choice(_PHONE_OTHER_DIGITS) for _ in range(9))
    return f"{first}{rest}"


def _generate_email(first_name: str | None = None, last_name: str | None = None) -> str:
    # Parameters unused intentionally; UUID token prevents PII leakage via name-derived emails
    token = uuid.uuid4().hex[:8]
    domain = random.choice(_EMAIL_DOMAINS)
    return f"user{token}@{domain}"


def _find_matching_index(zip_code: str) -> int | None:
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


def _fill_missing(profile: BillingProfile) -> BillingProfile:
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


def select_profile(zip_code: str | int | None = None) -> BillingProfile:
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
        if _MIN_BILLING_PROFILES > 0 and len(_profiles) < _MIN_BILLING_PROFILES:
            raise CycleExhaustedError(
                f"Billing pool has {len(_profiles)} profiles, "
                f"below minimum threshold {_MIN_BILLING_PROFILES}."
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
        del _profiles[index]
        if profile.phone is None or profile.email is None:
            profile = _fill_missing(profile)
        _profiles.append(profile)
        return profile
