#!/usr/bin/env python3
import os
import subprocess
import sys


def verify_ref(ref: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_diff_range() -> str:
    base_ref = os.getenv("GITHUB_BASE_REF")
    head_sha = os.getenv("GITHUB_HEAD_SHA")

    def has_value(value: str | None) -> bool:
        return value is not None and value.strip() != ""

    if os.getenv("GITHUB_ACTIONS") == "true":
        if not has_value(base_ref) or not has_value(head_sha):
            print(
                "check_spec_lock: missing GITHUB_BASE_REF or GITHUB_HEAD_SHA; "
                "cannot determine diff range in CI",
                file=sys.stderr,
            )
            sys.exit(1)

    if base_ref is None or head_sha is None:
        print(
            "check_spec_lock: WARNING: local mode, using develop...HEAD",
            file=sys.stderr,
        )
        base_ref = "develop"
        head_sha = "HEAD"

    if verify_ref(base_ref):
        base = base_ref
    elif verify_ref(f"origin/{base_ref}"):
        base = f"origin/{base_ref}"
    else:
        print(
            "check_spec_lock: ERROR: unable to resolve base ref",
            file=sys.stderr,
        )
        sys.exit(1)

    if not verify_ref(head_sha):
        print(
            f"check_spec_lock: head sha '{head_sha}' could not be resolved",
            file=sys.stderr,
        )
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


def main() -> None:
    diff_range = resolve_diff_range()
    changed_files = get_changed_files(diff_range)
    spec_files = [path for path in changed_files if is_spec_path(path)]

    if spec_files:
        print("check_spec_lock: spec files modified:", file=sys.stderr)
        for path in spec_files:
            print(path, file=sys.stderr)
        print(
            "check_spec_lock: FAIL - modifications in spec/ are not allowed",
            file=sys.stderr,
        )
        sys.exit(1)

    print("check_spec_lock: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
