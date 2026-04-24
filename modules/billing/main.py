from __future__ import annotations

import collections
import hashlib
import logging
import os
import random
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from modules.common.exceptions import CycleExhaustedError
from modules.common.types import BillingProfile

_lock = threading.Lock()
_LOAD_LOCK = threading.Lock()  # serializes cold-start pool loading; exactly one thread reads disk
_local_fill_rng = threading.local()  # pylint: disable=invalid-name
_profiles: "collections.deque[BillingProfile]" = collections.deque()
_logger = logging.getLogger(__name__)
_reload_requested: bool = False  # pylint: disable=invalid-name
_RELOAD_FLAG_LOCK = threading.Lock()

# ── Per-worker billing state (P4) ─────────────────────────────────────────
# Master pool: loaded once at startup (or lazily on first use).  Each worker
# receives its own independently-shuffled copy so workers never share state.
_MASTER_POOL: List[BillingProfile] = []
_MASTER_POOL_LOCK = threading.Lock()
_MASTER_POOL_LOADED: bool = False  # pylint: disable=invalid-name

_WORKER_STATES: Dict[str, "WorkerBillingState"] = {}
_WORKER_STATES_LOCK = threading.Lock()


@dataclass
class WorkerBillingState:
    """Per-worker billing pool state.  Each worker owns an independently
    shuffled copy of the master profile list and an independent index pointer
    for sequential (anti-repeat) selection."""

    profiles: List[BillingProfile]
    index: int = 0
    rng: random.Random = field(default_factory=random.Random)

_EMAIL_DOMAINS = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com")
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


def _is_production_mode() -> bool:
    """Return True when the production task-fn feature flag is active.

    Mirrors the check in ``integration.runtime.is_production_task_fn_enabled``.
    When production mode is on, ``/tmp`` paths are rejected as ``BILLING_POOL_DIR``
    because ``/tmp`` is a volatile, world-writable directory unsuitable for
    production billing data.
    """
    return os.environ.get("ENABLE_PRODUCTION_TASK_FN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


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
        if _is_production_mode():
            allowed_prefixes = (project_root, Path("/data"))
            tmp_path = Path("/tmp").resolve()
            if resolved == tmp_path or str(resolved).startswith(str(tmp_path) + os.sep):
                _logger.warning(
                    "BILLING_POOL_DIR resolves to a /tmp path which is not permitted in"
                    " production mode; using default billing_pool.",
                )
                return Path(__file__).resolve().parents[2] / "billing_pool"
        else:
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
    global _profiles, _reload_requested, _MASTER_POOL, _MASTER_POOL_LOADED, _WORKER_STATES
    with _MASTER_POOL_LOCK:
        _MASTER_POOL = []
        _MASTER_POOL_LOADED = False
    with _WORKER_STATES_LOCK:
        _WORKER_STATES = {}
    with _LOAD_LOCK:
        with _lock:
            _profiles = collections.deque()
    with _RELOAD_FLAG_LOCK:
        _reload_requested = False


def request_pool_reload() -> None:
    """Trigger a full reload of the billing pool from disk.

    Clears *all* in-memory caches — ``_MASTER_POOL`` (used by the per-worker
    sharded selection path), ``_WORKER_STATES`` (so each worker re-shuffles
    from a fresh master on next access), and the legacy global deque — then
    eagerly re-reads the pool directory.

    Call this after adding new profiles to ``BILLING_POOL_DIR`` so that
    workers pick up the new files immediately.  Thread-safe.
    """
    global _profiles, _reload_requested, _MASTER_POOL, _MASTER_POOL_LOADED
    # Set the legacy invalidation flag first so any in-flight legacy caller
    # sees a conservative "reload pending" signal until the eager reload has
    # completed and the caches are fresh again.
    with _RELOAD_FLAG_LOCK:
        _reload_requested = True
    # Clear per-worker shuffled caches first (outer lock), then the shared
    # master pool, matching the get_worker_state() -> _ensure_master_pool_loaded()
    # lock acquisition order (_WORKER_STATES_LOCK -> _MASTER_POOL_LOCK).
    with _WORKER_STATES_LOCK:
        _WORKER_STATES.clear()
        with _MASTER_POOL_LOCK:
            _logger.info("Billing pool reload requested; clearing caches")
            _MASTER_POOL = []
            _MASTER_POOL_LOADED = False
    # Clear the legacy deque under the same load-serialization lock used by
    # the cold-start path so cache invalidation cannot race with a concurrent
    # lazy load that repopulates _profiles from stale disk state.
    with _LOAD_LOCK:
        with _lock:
            _profiles.clear()
    try:
        # Eagerly re-read from disk so subsequent select_profile() calls see
        # the fresh content without a lazy-load roundtrip.
        load_billing_pool()
    finally:
        # The eager reload has completed (or failed), so clear the legacy
        # invalidation flag to avoid an unnecessary second cache flush on the
        # next select_profile() call.
        with _RELOAD_FLAG_LOCK:
            _reload_requested = False


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


def load_billing_pool() -> int:
    """Eagerly load the billing pool from disk into the master pool and legacy deque.

    Call once at startup (before workers start) to populate both the legacy
    ``_profiles`` deque (used when ``worker_id`` is omitted) and the new
    ``_MASTER_POOL`` list (used for per-worker shuffled copies).

    Returns:
        Number of profiles loaded.

    This is a no-op if the pool is already loaded; subsequent calls return
    the current pool size without re-reading disk.
    """
    global _MASTER_POOL, _MASTER_POOL_LOADED, _profiles  # pylint: disable=global-statement
    with _MASTER_POOL_LOCK:
        if _MASTER_POOL_LOADED:
            return len(_MASTER_POOL)
        loaded_deque = _read_profiles_from_disk()
        master = list(loaded_deque)
        _MASTER_POOL = master
        _MASTER_POOL_LOADED = True
    with _LOAD_LOCK:
        with _lock:
            if not _profiles:
                _profiles = loaded_deque
    count = len(_MASTER_POOL)
    _logger.info("Billing pool eagerly loaded at startup: %d profiles.", count)
    return count


def _ensure_master_pool_loaded() -> None:
    """Lazily load the master pool from disk if not yet loaded.

    Thread-safe.  Subsequent calls after the first load are no-ops.
    """
    global _MASTER_POOL, _MASTER_POOL_LOADED  # pylint: disable=global-statement
    with _MASTER_POOL_LOCK:
        if _MASTER_POOL_LOADED:
            return
        loaded_deque = _read_profiles_from_disk()
        _MASTER_POOL = list(loaded_deque)
        _MASTER_POOL_LOADED = True
        _logger.info(
            "Billing master pool lazily loaded: %d profiles.", len(_MASTER_POOL)
        )


def get_worker_state(worker_id: str) -> "WorkerBillingState":
    """Return the per-worker :class:`WorkerBillingState`, creating it on first access.

    On first access for *worker_id* the master pool is copied, shuffled with a
    per-worker seed, and stored.  Subsequent calls return the same state object.

    Thread-safe: uses :data:`_WORKER_STATES_LOCK`.
    """
    with _WORKER_STATES_LOCK:
        if worker_id in _WORKER_STATES:
            return _WORKER_STATES[worker_id]
        # Ensure master pool is available before creating worker state.
        _ensure_master_pool_loaded()
        profiles = list(_MASTER_POOL)
        rng = random.Random(hash(worker_id))
        rng.shuffle(profiles)
        state = WorkerBillingState(profiles=profiles, rng=rng)
        _WORKER_STATES[worker_id] = state
        return state


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
    def _sanitize(name: str) -> str:
        return "".join(c for c in name.lower() if c.isalnum() or c in ".-")[:20]

    fill_rng = rng or _get_fill_rng()
    stripped_first_name = (_first_name or "").strip()
    stripped_last_name = (_last_name or "").strip()
    sanitized_first_name = _sanitize(stripped_first_name)
    sanitized_last_name = _sanitize(stripped_last_name)
    if stripped_first_name and stripped_last_name and sanitized_first_name and sanitized_last_name:
        local = f"{sanitized_first_name}.{sanitized_last_name}"
        domain = fill_rng.choice(_EMAIL_DOMAINS)
        return f"{local}@{domain}"
    token = "".join(fill_rng.choice(_HEX_CHARS) for _ in range(8))
    domain = fill_rng.choice(_EMAIL_DOMAINS)
    return f"user{token}@{domain}"


def _find_matching_index(zip_code: str) -> int | None:
    """Find the index of the first profile matching *zip_code*.

    .. warning::
        This function reads module-level ``_profiles`` without acquiring
        ``_lock``.  It **MUST** only be called while the caller already holds
        ``_lock``.  The runtime check below is a best-effort programming-error
        detector; it is not an atomic safety guarantee because another thread
        could release the lock between the ``locked()`` test and the function
        body executing.  The check exists to catch accidental direct calls in
        tests and development, not to substitute for correct lock discipline.
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


def select_profile(
    zip_code: str | int | None = None,
    worker_id: Optional[str] = None,
) -> BillingProfile:
    """Select a billing profile from the pool.

    When *worker_id* is ``None`` (default), the legacy global-deque path is
    used — all existing callers that omit *worker_id* see unchanged behaviour.

    When *worker_id* is provided, the per-worker path is used:
    each worker gets its own independently-shuffled copy of the master pool
    and an independent sequential index pointer.

    Per-worker selection algorithm:
      1. Search from ``state.index`` forward (with wrap-around) for a profile
         whose ``zip_code`` matches the requested *zip_code*.
      2. If found: return it **without** advancing ``state.index`` (blueprint
         rule — the pointer is reserved for the sequential fallback).
      3. If not found: return ``state.profiles[state.index]``, then advance
         ``state.index = (state.index + 1) % len(state.profiles)``.

    Args:
        zip_code: Optional zip/postal code for affinity matching.
        worker_id: When supplied, enables per-worker state isolation
            (anti-repeat + independent shuffle).

    Returns:
        A :class:`~modules.common.types.BillingProfile` with ``phone`` and
        ``email`` guaranteed to be set (auto-generated if missing in the file).

    Raises:
        CycleExhaustedError: If the billing pool is empty or below the
            configured minimum threshold.
        ValueError: If *zip_code* has an unsupported type.
    """
    if worker_id is not None:
        return _select_profile_per_worker(zip_code, worker_id)
    return _select_profile_legacy(zip_code)


def _select_profile_per_worker(
    zip_code: str | int | None,
    worker_id: str,
) -> BillingProfile:
    """Per-worker profile selection with independent shuffled list + index pointer."""
    normalized_zip = _normalize_zip(zip_code)
    state = get_worker_state(worker_id)

    with _WORKER_STATES_LOCK:
        profiles = state.profiles
        if not profiles:
            pool_dir = _pool_dir()
            raise CycleExhaustedError(
                f"No billing profiles available for worker '{worker_id}' "
                f"(pool_dir='{pool_dir}')"
            )
        if _MIN_BILLING_PROFILES > 0 and len(profiles) < _MIN_BILLING_PROFILES:
            raise CycleExhaustedError(
                f"Billing pool has {len(profiles)} profiles, "
                f"below minimum threshold {_MIN_BILLING_PROFILES}."
            )

        n = len(profiles)
        # Search from state.index forward for a profile matching zip_code.
        if normalized_zip:
            for offset in range(n):
                i = (state.index + offset) % n
                if _normalize_zip(profiles[i].zip_code) == normalized_zip:
                    profile = profiles[i]
                    if profile.phone is None or profile.email is None:
                        profile = _fill_missing(profile)
                        profiles[i] = profile
                    # Do NOT advance state.index (blueprint rule).
                    return profile

        # No zip match (or no zip requested): use sequential pointer, advance it.
        profile = profiles[state.index]
        if profile.phone is None or profile.email is None:
            profile = _fill_missing(profile)
            profiles[state.index] = profile
        state.index = (state.index + 1) % n
        return profile


def _select_profile_legacy(zip_code: str | int | None) -> BillingProfile:
    """Legacy global-deque profile selection (unchanged behaviour)."""
    global _profiles, _reload_requested  # pylint: disable=global-statement
    normalized_zip = _normalize_zip(zip_code)

    # Hot-reload: if a reload was requested (e.g., after CB pause), invalidate cache.
    with _RELOAD_FLAG_LOCK:
        do_reload = _reload_requested
        if do_reload:
            _reload_requested = False
    if do_reload:
        with _lock:
            _profiles.clear()
        _logger.info(
            "Billing pool cache invalidated by reload request; "
            "profiles will be re-read from disk."
        )

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
