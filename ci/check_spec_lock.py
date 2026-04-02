#!/usr/bin/env python3
import os
import subprocess
import sys


def verify_ref(ref: str) -> tuple[str | None, str]:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout.strip(), ""


def resolve_diff_range() -> str:
    base_ref_raw = os.getenv("GITHUB_BASE_REF")
    head_sha_raw = os.getenv("GITHUB_HEAD_SHA") or os.getenv("GITHUB_SHA")

    is_ci = os.getenv("GITHUB_ACTIONS") == "true"

    if is_ci:
        base_ref = base_ref_raw.strip() if base_ref_raw else ""
        head_sha = head_sha_raw.strip() if head_sha_raw else ""
        if not base_ref or not head_sha:
            print(
                "check_spec_lock: missing GITHUB_BASE_REF or "
                "GITHUB_HEAD_SHA/GITHUB_SHA; "
                "cannot determine diff range in CI",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        base_ref = base_ref_raw.strip() if base_ref_raw else ""
        head_sha = head_sha_raw.strip() if head_sha_raw else ""
        if not base_ref or not head_sha:
            print(
                "check_spec_lock: missing GITHUB_BASE_REF or "
                "GITHUB_HEAD_SHA/GITHUB_SHA; "
                "set them to run locally",
                file=sys.stderr,
            )
            sys.exit(1)

    base_ref_sha, base_ref_error = verify_ref(base_ref)
    if base_ref_sha is not None:
        base = base_ref
    else:
        origin_ref = f"origin/{base_ref}"
        origin_sha, origin_error = verify_ref(origin_ref)
        if origin_sha is not None:
            base = origin_ref
        else:
            print(
                "check_spec_lock: ERROR: unable to resolve base ref "
                f"'{base_ref}' (also tried '{origin_ref}')",
                file=sys.stderr,
            )
            if base_ref_error:
                print(base_ref_error, file=sys.stderr)
            if origin_error:
                print(origin_error, file=sys.stderr)
            sys.exit(1)

    head_sha_resolved, head_sha_error = verify_ref(head_sha)
    if head_sha_resolved is None:
        print(
            f"check_spec_lock: head SHA '{head_sha}' could not be resolved",
            file=sys.stderr,
        )
        if head_sha_error:
            print(head_sha_error, file=sys.stderr)
        sys.exit(1)

    return f"{base}...{head_sha}"


def get_changed_files(diff_range: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", diff_range],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("check_spec_lock: git diff failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_spec_path(path: str) -> bool:
    normalized = normalize_path(path)
    return normalized == "spec" or normalized.startswith("spec/")


def _parse_labels(raw: str) -> set[str]:
    """Parse comma-separated labels into a normalized set (exact match)."""
    return {label.strip().lower() for label in raw.split(",")
            if label.strip()}


def _resolve_change_class() -> str:
    """Resolve CHANGE_CLASS from env var (no auto-detect here)."""
    return os.environ.get("CHANGE_CLASS", "").strip().lower()


def _is_authorized() -> bool:
    """Check authorization: label 'approved-override' OR CHANGE_CLASS_APPROVED=true."""
    labels = _parse_labels(os.environ.get("PR_LABELS", ""))
    admin_approved = os.environ.get("CHANGE_CLASS_APPROVED", "").strip().lower()
    return "approved-override" in labels or admin_approved == "true"


def main() -> None:
    diff_range = resolve_diff_range()
    changed_files = get_changed_files(diff_range)
    spec_files = [path for path in changed_files if is_spec_path(path)]

    if not spec_files:
        print("check_spec_lock: PASS", file=sys.stderr)
        sys.exit(0)

    # Spec files modified — check if spec_sync is authorized
    change_class = _resolve_change_class()

    if change_class == "spec_sync" and _is_authorized():
        print("check_spec_lock: PASS (spec_sync authorized)",
              file=sys.stderr)
        sys.exit(0)

    # Unauthorized spec modification
    print("check_spec_lock: FAIL — spec files modified:", file=sys.stderr)
    for path in spec_files:
        print(f"  {path}", file=sys.stderr)
    if change_class != "spec_sync":
        print("  Spec modification requires CHANGE_CLASS=spec_sync",
              file=sys.stderr)
    else:
        print("  CHANGE_CLASS=spec_sync requires authorization: "
              "PR label 'approved-override' or CHANGE_CLASS_APPROVED=true",
              file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()