#!/usr/bin/env python3
"""Check that a PR stays within scope: ≤ 200 changed lines (excluding
ci/ and tests/) and touches at most one module under modules/.

Architect Directive (AD-6 amendment):
  ci/ and tests/ are excluded from the line count because CI scripts are
  infrastructure code and tests should never be penalised against a size
  limit.  This avoids the self-blocking paradox where the enforcement
  script itself would exceed the limit it enforces.
"""

import os
import re
import subprocess
import sys

# ── configuration ──────────────────────────────────────────────────
MAX_CHANGED_LINES = 200
EXCLUDED_PREFIXES = ("tests/", "ci/")

# ── git helpers ────────────────────────────────────────────────────

REF_PATTERN = re.compile(r"^[A-Za-z0-9._/~-]+$")


def _sanitize_ref(ref: str) -> str:
    return ref.replace("\n", " ").replace("\r", " ").strip()


def _validate_ref(ref: str) -> tuple[str | None, str]:
    if not ref or ref.startswith("-"):
        return None, f"invalid git ref '{_sanitize_ref(ref)}'"
    if not REF_PATTERN.fullmatch(ref):
        return None, f"invalid git ref '{_sanitize_ref(ref)}'"
    if ".." in ref or "/." in ref or "./" in ref:
        return None, f"invalid git ref '{_sanitize_ref(ref)}'"
    if ref.startswith("/") or ref.endswith("/"):
        return None, f"invalid git ref '{_sanitize_ref(ref)}'"
    return ref, ""


def _verify_ref(ref: str) -> tuple[str | None, str]:
    safe, err = _validate_ref(ref)
    if safe is None:
        return None, err
    result = subprocess.run(
        ["git", "rev-parse", "--verify", safe],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip())
        msg = f"git rev-parse --verify {safe} failed"
        return None, f"{msg}: {detail}" if detail else msg
    return result.stdout.strip(), ""


def resolve_diff_range() -> str:
    base_raw = (os.getenv("GITHUB_BASE_REF") or "").strip()
    head_raw = (os.getenv("GITHUB_HEAD_SHA")
                or os.getenv("GITHUB_SHA") or "").strip()
    is_ci = os.getenv("GITHUB_ACTIONS") == "true"

    if base_raw and head_raw:
        # resolve base
        base_sha, _ = _verify_ref(base_raw)
        if base_sha is not None:
            base = base_raw
        else:
            origin = f"origin/{base_raw}"
            origin_sha, _ = _verify_ref(origin)
            if origin_sha is not None:
                base = origin
            else:
                print(f"check_pr_scope: cannot resolve base ref "
                      f"'{_sanitize_ref(base_raw)}'", file=sys.stderr)
                sys.exit(1)
        # resolve head
        head_sha, _ = _verify_ref(head_raw)
        if head_sha is None:
            print(f"check_pr_scope: cannot resolve head SHA "
                  f"'{_sanitize_ref(head_raw)}'", file=sys.stderr)
            sys.exit(1)
        return f"{base}...{head_raw}"

    if is_ci:
        print("check_pr_scope: missing GITHUB_BASE_REF or "
              "GITHUB_HEAD_SHA/GITHUB_SHA", file=sys.stderr)
        sys.exit(1)

    # local fallback
    for candidate in ("origin/main", "main", "origin/develop", "develop"):
        sha, _ = _verify_ref(candidate)
        if sha is not None:
            return f"{candidate}...HEAD"

    parent, _ = _verify_ref("HEAD~1")
    if parent is not None:
        return "HEAD~1...HEAD"

    print("check_pr_scope: unable to determine diff range",
          file=sys.stderr)
    sys.exit(1)


# ── diff analysis ──────────────────────────────────────────────────

def _normalize(path: str) -> str:
    p = path.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def _is_excluded(path: str) -> bool:
    norm = _normalize(path)
    return any(norm.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def get_numstat(diff_range: str) -> list[tuple[int, int, str]]:
    """Return list of (added, deleted, filepath) from git diff --numstat."""
    result = subprocess.run(
        ["git", "diff", "--numstat", diff_range],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print("check_pr_scope: git diff --numstat failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)

    entries: list[tuple[int, int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_s, deleted_s, filepath = parts[0], parts[1], parts[2]
        if added_s == "-" or deleted_s == "-":
            # binary file
            continue
        entries.append((int(added_s), int(deleted_s), filepath))
    return entries


def module_from_path(path: str) -> str | None:
    norm = _normalize(path)
    if not norm.startswith("modules/"):
        return None
    parts = norm.split("/")
    return parts[1] if len(parts) >= 2 and parts[1] else None


# ── main ───────────────────────────────────────────────────────────

def check(diff_range: str) -> int:
    """Run scope checks.  Returns 0 on PASS, 1 on FAIL."""
    entries = get_numstat(diff_range)
    total_lines = 0
    modules_touched: set[str] = set()
    excluded_lines = 0

    for added, deleted, filepath in entries:
        changed = added + deleted
        mod = module_from_path(filepath)
        if mod:
            modules_touched.add(mod)
        if _is_excluded(filepath):
            excluded_lines += changed
        else:
            total_lines += changed

    errors: list[str] = []
    if total_lines > MAX_CHANGED_LINES:
        errors.append(
            f"total lines changed ({total_lines}) exceeds "
            f"{MAX_CHANGED_LINES} (excluding {', '.join(EXCLUDED_PREFIXES)})"
        )
    if len(modules_touched) > 1:
        errors.append(
            f"PR touches {len(modules_touched)} modules "
            f"({', '.join(sorted(modules_touched))}); max 1 allowed"
        )

    if errors:
        print("check_pr_scope: FAIL")
        for err in errors:
            print(f"  {err}")
        if excluded_lines:
            print(f"  (excluded {excluded_lines} lines in "
                  f"{', '.join(EXCLUDED_PREFIXES)})")
        return 1

    print(f"check_pr_scope: PASS ({total_lines} lines changed"
          + (f", {excluded_lines} excluded" if excluded_lines else "")
          + ")")
    return 0


def main() -> int:
    diff_range = resolve_diff_range()
    return check(diff_range)


if __name__ == "__main__":
    sys.exit(main())
