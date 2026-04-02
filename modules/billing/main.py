import os
import random
import threading
from pathlib import Path

from modules.common.exceptions import CycleExhaustedError
from modules.common.types import BillingProfile

_lock = threading.Lock()
_profiles = []
_cursor = 0

_EMAIL_DOMAINS = ("gmail.com", "yahoo.com", "outlook.com", "icloud.com")
_PHONE_FIRST_DIGITS = "23456789"


def _pool_dir():
    override = os.getenv("BILLING_POOL_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "billing_pool"


def _reset_state():
    global _profiles, _cursor
    with _lock:
        _profiles = []
        _cursor = 0


def _normalize_zip(zip_code):
    if zip_code is None:
        return ""
    return str(zip_code).strip()


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


def _load_profiles_locked():
    global _profiles, _cursor
    if _profiles:
        return
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
    _profiles = profiles
    _cursor = 0
    if profiles:
        _profiles = profiles
        _cursor = 0
        _initialized = True


def _generate_email(first_name, last_name):
    local = f"{first_name}.{last_name}".strip(".").lower().replace(" ", "")
    if not local:
        local = "user"
    domain = random.choice(_EMAIL_DOMAINS)
    return f"{local}@{domain}"


def _find_matching_index(zip_code):
    if not zip_code:
        return None
    count = len(_profiles)
    start = _cursor
    for offset in range(count):
        index = (start + offset) % count
        if _normalize_zip(_profiles[index].zip_code) == zip_code:
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
    global _cursor
    normalized_zip = _normalize_zip(zip_code)
    with _lock:
        _load_profiles_locked()
        if not _profiles:
            raise CycleExhaustedError("No billing profiles available")

        index = _find_matching_index(normalized_zip)
        if index is None:
            pool_dir = _pool_dir()
            exists = pool_dir.is_dir()
            raise CycleExhaustedError(
                f"No billing profiles available in billing pool directory '{pool_dir}' "
                f"(exists={exists})"
            )

        profile = _profiles[index]
        if profile.phone is None or profile.email is None:
            profile = _fill_missing(profile)
            _profiles[index] = profile
        return profile
