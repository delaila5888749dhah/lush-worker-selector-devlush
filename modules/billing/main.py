from __future__ import annotations

import collections
import hashlib
import logging
import os
import random
import threading
from pathlib import Path

from modules.common.exceptions import CycleExhaustedError
from modules.common.types import BillingProfile

_lock = threading.Lock()
_LOAD_LOCK = threading.Lock()  # serializes cold-start pool loading; exactly one thread reads disk
_local_fill_rng = threading.local()  # pylint: disable=invalid-name
_profiles: "collections.deque[BillingProfile]" = collections.deque()
_logger = logging.getLogger(__name__)

_EMAIL_DOMAINS = ("gmail.com", "yahoo.com", "outlook.com", "icloud.com")
_PHONE_FIRST_DIGITS = "23456789"
_PHONE_OTHER_DIGITS = "0123456789"
_HEX_CHARS = "0123456789abcdef"
# Mix persona seed into a dedicated RNG stream used only for billing field fills.
_FILL_RNG_XOR_MASK = 0xDEAD_BEEF


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
    with _LOAD_LOCK:
        with _lock:
            _profiles = collections.deque()


def _normalize_zip(zip_code: str | int | None) -> str:
    """Return a canonical string form of *zip_code*.

    Both the string ``"12345"`` and the integer ``12345`` normalize to the
    same value (``"12345"``), so callers may pass either type and receive
    identical results for logically equivalent inputs.  Leading/trailing
    whitespace in string values is stripped.
    """
    if zip_code is None:
        return ""
    if isinstance(zip_code, bool):
        raise ValueError("zip_code must be str or int")
    if isinstance(zip_code, int):
        return str(zip_code)
    if isinstance(zip_code, str):
        return zip_code.strip()
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
    This function is only called by select_profile() while the caller holds
    ``_LOAD_LOCK``, ensuring exactly one thread performs disk I/O during cold
    start.  The ``_LOAD_LOCK`` serialization guarantee means this function
    never runs concurrently with itself.

    Each call creates a local Random instance for shuffle to avoid contending
    on the global random module's internal state lock.
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


def _get_fill_rng(persona_seed: int | None = None) -> random.Random:
    """Get per-thread or per-persona deterministic RNG."""
    if persona_seed is not None:
        return random.Random(persona_seed ^ _FILL_RNG_XOR_MASK)
    if not hasattr(_local_fill_rng, "rng"):
        _local_fill_rng.rng = random.Random()
    return _local_fill_rng.rng


def _generate_phone(rng: random.Random | None = None) -> str:
    fill_rng = rng or _get_fill_rng()
    first = fill_rng.choice(_PHONE_FIRST_DIGITS)
    rest = "".join(fill_rng.choice(_PHONE_OTHER_DIGITS) for _ in range(9))
    return f"{first}{rest}"


def _generate_email(
        _first_name: str | None = None,
        _last_name: str | None = None,
        rng: random.Random | None = None,
) -> str:
    # _first_name/_last_name unused intentionally; randomized token prevents PII leakage
    fill_rng = rng or _get_fill_rng()
    token = "".join(fill_rng.choice(_HEX_CHARS) for _ in range(8))
    domain = fill_rng.choice(_EMAIL_DOMAINS)
    return f"user{token}@{domain}"


def _find_matching_index(zip_code: str) -> int | None:
    """Find the index of the first profile matching *zip_code*.

    .. warning::
        This function reads module-level ``_profiles`` without acquiring
        ``_lock``.  It **MUST** only be called while the caller already holds
        ``_lock``.  A runtime check enforces this contract.
    """
    if not _lock.locked():  # pylint: disable=no-member
        raise RuntimeError("_find_matching_index() must be called while holding _lock")
    if not zip_code:
        return None
    for index, profile in enumerate(_profiles):
        if _normalize_zip(profile.zip_code) == zip_code:
            return index
    return None


def _fill_missing(profile: BillingProfile) -> BillingProfile:
    name_key = f"{profile.first_name or ''}{profile.last_name or ''}"
    if not name_key:
        name_key = (
            f"{profile.address}|{profile.city}|{profile.state}|{profile.zip_code}"
        )
    if not name_key.replace("|", "").strip():
        name_key = f"{profile.phone or ''}|{profile.email or ''}" or "anonymous-profile"
    seed = int.from_bytes(hashlib.sha256(name_key.encode("utf-8")).digest()[:4], "big")
    # Keep seed in positive 31-bit range for stable Random seeding semantics.
    seed &= 0x7FFFFFFF
    _rng = _get_fill_rng(seed)
    phone = profile.phone or _generate_phone(rng=_rng)
    email = profile.email or _generate_email(profile.first_name, profile.last_name, rng=_rng)
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
        # Serialize cold-start loading: only the first thread entering this block
        # reads from disk.  Subsequent threads wait on _LOAD_LOCK and then
        # re-check _profiles under _lock; if already populated they skip I/O.
        with _LOAD_LOCK:
            with _lock:
                needs_load = not _profiles
            if needs_load:
                loaded = _read_profiles_from_disk()
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
