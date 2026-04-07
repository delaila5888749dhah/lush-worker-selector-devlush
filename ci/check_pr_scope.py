#!/usr/bin/env python3
"""Check that a PR stays within scope: ≤ 200 changed lines (excluding
ci/, tests/, and spec/) and touches at most one module under modules/.

Modules with ≤ MODULE_MINOR_THRESHOLD non-excluded changed lines are
treated as incidental and excluded from the module count.  This allows
small cross-module fixes (e.g. adding try/finally) without requiring
a CHANGE_CLASS override.

Exception Framework (AI_CONTEXT.md §6):
  CHANGE_CLASS is resolved with the following priority:
    1. Explicit ``CHANGE_CLASS`` env var (highest)
    2. Auto-detection from PR title: [spec-sync]→spec_sync,
       [emergency]→emergency_override, [infra]→infra_change
    3. Default ``'normal'``

  Authorization is required for all non-normal CHANGE_CLASS values.
  All override usage is logged as structured JSON audit trail.

Change Classes & Bypass Table (AI_CONTEXT.md §6):
  normal             — no bypasses (default)
  spec_sync          — bypass line + module limit; requires authorization
  infra_change       — bypass line limit only; requires authorization
  emergency_override — bypass line + module limit; requires authorization
                       + APPROVED review

Authorization (required for ALL non-normal):
  PR label 'approved-override' (exact match) OR CHANGE_CLASS_APPROVED=true
  spec_sync also accepts ALLOW_SPEC_MODIFICATION=true
  (consistent with check_spec_lock and meta_audit)
"""

import json
import os
import re
import subprocess
import sys

# ── configuration ──────────────────────────────────────────────────
MAX_CHANGED_LINES = 200
MODULE_MINOR_THRESHOLD = 20  # Modules with ≤ this many non-excluded lines are incidental
EXCLUDED_PREFIXES = ("tests/", "ci/", "spec/")
VALID_CHANGE_CLASSES = frozenset({
    "normal",
    "emergency_override",
    "spec_sync",
    "infra_change",
})

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


def _get_changed_files(diff_range: str) -> list[str]:
    """Return list of changed file paths.  Exits on git failure."""
    result = subprocess.run(
        ["git", "diff", "--name-only", diff_range],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print("check_pr_scope: git diff --name-only failed",
              file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)
    files = [f for f in result.stdout.splitlines() if f.strip()]
    if not files:
        print("check_pr_scope: no changed files detected", file=sys.stderr)
        sys.exit(1)
    return files


def module_from_path(path: str) -> str | None:
    norm = _normalize(path)
    if not norm.startswith("modules/"):
        return None
    parts = norm.split("/")
    return parts[1] if len(parts) >= 2 and parts[1] else None


# ── label parsing (security: exact match only) ────────────────────

def _parse_labels(raw: str) -> set[str]:
    """Parse comma-separated labels into a normalized set.

    Uses exact match after strip+lower to prevent substring bypass
    attacks (e.g. 'not-approved-override' must NOT grant access).
    """
    return {label.strip().lower() for label in raw.split(",")
            if label.strip()}


# ── change classification ──────────────────────────────────────────

_TITLE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[emergency\]", re.IGNORECASE), "emergency_override"),
    (re.compile(r"\[spec-sync\]", re.IGNORECASE), "spec_sync"),
    (re.compile(r"\[infra\]", re.IGNORECASE), "infra_change"),
]


def _resolve_change_class() -> str:
    """Resolve CHANGE_CLASS from explicit env var, PR title, or default.

    Priority (AI_CONTEXT.md §6):
      1. Explicit ``CHANGE_CLASS`` env var (highest)
      2. Auto-detection from ``PR_TITLE`` patterns
      3. Default ``'normal'``
    """
    explicit = os.environ.get("CHANGE_CLASS", "").strip().lower()
    if explicit:
        return explicit

    pr_title = os.environ.get("PR_TITLE", "").strip()
    for pattern, change_class in _TITLE_PATTERNS:
        if pattern.search(pr_title):
            return change_class

    return "normal"


def _check_authorization(change_class: str) -> list[str]:
    """Check authorization for non-normal CHANGE_CLASS.

    Per AI_CONTEXT.md §6 — Authorization:
      All non-normal require: label 'approved-override' OR
      CHANGE_CLASS_APPROVED=true.
      spec_sync is also authorized by ALLOW_SPEC_MODIFICATION=true
      (consistent with check_spec_lock and meta_audit).
      emergency_override additionally needs APPROVED review.
    """
    if change_class == "normal":
        return []

    labels = _parse_labels(os.environ.get("PR_LABELS", ""))
    admin_approved = (
        os.environ.get("CHANGE_CLASS_APPROVED", "").strip().lower()
    )
    has_label = "approved-override" in labels
    has_admin = admin_approved == "true"
    has_allow_spec = (
        change_class == "spec_sync"
        and os.environ.get(
            "ALLOW_SPEC_MODIFICATION", ""
        ).strip().lower() == "true"
    )

    if not has_label and not has_admin and not has_allow_spec:
        return [
            f"CHANGE_CLASS={change_class} requires explicit authorization: "
            f"PR label 'approved-override' or CHANGE_CLASS_APPROVED=true"
        ]

    if change_class == "emergency_override":
        review_state = (
            os.environ.get("PR_REVIEW_STATE", "").strip().upper()
        )
        if review_state != "APPROVED":
            return [
                f"CHANGE_CLASS=emergency_override requires at least 1 "
                f"APPROVED review (current: {review_state or 'NONE'})"
            ]

    return []


# ── main ───────────────────────────────────────────────────────────

def _analyze_entries(
    entries: list[tuple[int, int, str]],
) -> tuple[int, int, dict[str, int]]:
    """Compute total_lines, excluded_lines, and per-module line counts.

    Returns:
        (total_lines, excluded_lines, module_lines) where module_lines
        maps module name → non-excluded changed lines in that module.
    """
    total_lines = 0
    excluded_lines = 0
    module_lines: dict[str, int] = {}
    for added, deleted, filepath in entries:
        changed = added + deleted
        mod = module_from_path(filepath)
        if mod and not _is_excluded(filepath):
            module_lines[mod] = module_lines.get(mod, 0) + changed
        if _is_excluded(filepath):
            excluded_lines += changed
        else:
            total_lines += changed
    return total_lines, excluded_lines, module_lines


def _emit_audit_log(
    change_class: str,
    authorization: str,
    context_binding: str,
    validation: str,
) -> None:
    """Emit structured audit trail for override usage.

    stdout: pure JSON (machine-readable)
    stderr: diagnostics (human-readable)
    """
    if change_class == "normal":
        return
    log = {
        "change_class": change_class,
        "pr_title": os.environ.get("PR_TITLE", ""),
        "pr_labels": os.environ.get("PR_LABELS", ""),
        "authorization": authorization,
        "context_binding": context_binding,
        "validation": validation,
    }
    print(json.dumps(log))


def check(diff_range: str) -> int:
    """Run scope checks.  Returns 0 on PASS, 1 on FAIL."""
    entries = get_numstat(diff_range)
    total_lines, excluded_lines, module_lines = _analyze_entries(entries)

    primary_modules = {
        m for m, lines in module_lines.items()
        if lines > MODULE_MINOR_THRESHOLD
    }

    errors: list[str] = []
    if total_lines > MAX_CHANGED_LINES:
        errors.append(
            f"total lines changed ({total_lines}) exceeds "
            f"{MAX_CHANGED_LINES} (excluding {', '.join(EXCLUDED_PREFIXES)})"
        )
    if len(primary_modules) > 1:
        errors.append(
            f"PR touches {len(primary_modules)} modules "
            f"({', '.join(sorted(primary_modules))}); max 1 allowed"
        )

    if errors:
        print("check_pr_scope: FAIL", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        if excluded_lines:
            print(f"  (excluded {excluded_lines} lines in "
                  f"{', '.join(EXCLUDED_PREFIXES)})", file=sys.stderr)
        return 1

    print(f"check_pr_scope: PASS ({total_lines} lines changed"
          + (f", {excluded_lines} excluded" if excluded_lines else "")
          + ")", file=sys.stderr)
    return 0


def _export_to_github_env(name: str, value: str) -> None:
    """Write a variable to $GITHUB_ENV for subsequent workflow steps."""
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def main() -> int:
    diff_range = resolve_diff_range()
    change_class = _resolve_change_class()

    # Export resolved CHANGE_CLASS to GITHUB_ENV so downstream steps
    # (e.g. check_spec_lock) receive the same value.
    _export_to_github_env("CHANGE_CLASS", change_class)

    # Validate CHANGE_CLASS
    if change_class not in VALID_CHANGE_CLASSES:
        print(f"check_pr_scope: invalid CHANGE_CLASS '{change_class}'; "
              f"valid values: {', '.join(sorted(VALID_CHANGE_CLASSES))}",
              file=sys.stderr)
        return 1

    # Normal: enforce all limits, no governance needed
    if change_class == "normal":
        return check(diff_range)

    # Non-normal: governance checks

    # Authorization (AI_CONTEXT.md §6)
    auth_errors = _check_authorization(change_class)
    if auth_errors:
        print("check_pr_scope: FAIL", file=sys.stderr)
        for err in auth_errors:
            print(f"  {err}", file=sys.stderr)
        _emit_audit_log(change_class, "DENIED", "N/A", "FAIL")
        return 1

    # Bypass table (AI_CONTEXT.md §6)
    skip_line_limit = change_class in (
        "spec_sync", "infra_change", "emergency_override",
    )
    skip_module_limit = change_class in (
        "spec_sync", "emergency_override",
    )

    entries = get_numstat(diff_range)
    total_lines, excluded_lines, module_lines = _analyze_entries(entries)

    primary_modules = {
        m for m, lines in module_lines.items()
        if lines > MODULE_MINOR_THRESHOLD
    }

    errors: list[str] = []
    if not skip_line_limit and total_lines > MAX_CHANGED_LINES:
        errors.append(
            f"total lines changed ({total_lines}) exceeds "
            f"{MAX_CHANGED_LINES} (excluding {', '.join(EXCLUDED_PREFIXES)})"
        )
    if not skip_module_limit and len(primary_modules) > 1:
        errors.append(
            f"PR touches {len(primary_modules)} modules "
            f"({', '.join(sorted(primary_modules))}); max 1 allowed"
        )

    if errors:
        print("check_pr_scope: FAIL", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        _emit_audit_log(change_class, "GRANTED", "MATCH", "FAIL")
        return 1

    _emit_audit_log(change_class, "GRANTED", "MATCH", "PASS")
    print(f"check_pr_scope: PASS ({total_lines} lines changed"
          + (f", {excluded_lines} excluded" if excluded_lines else "")
          + f", change_class={change_class})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
