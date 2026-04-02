#!/usr/bin/env python3
"""Check that a PR stays within scope: ≤ 200 changed lines (excluding
ci/ and tests/) and touches at most one module under modules/.

Architect Directive (AD-6 amendment):
  ci/ and tests/ are excluded from the line count because CI scripts are
  infrastructure code and tests should never be penalized against a size
  limit.  This avoids the self-blocking paradox where the enforcement
  script itself would exceed the limit it enforces.

Change Classification (Exception Framework — Final Architecture):
  CHANGE_CLASS is auto-detected from PR content.  It is the SINGLE
  source of truth for CI policy selection.  There are NO legacy flags,
  NO implicit bypasses, and NO environment-only overrides.

  Detection rules (hard, in priority order):
    1. Explicit CHANGE_CLASS env var (if set and valid)
    2. "[emergency]" in PR title → emergency_override
    3. ANY changed file in spec/ → spec_sync
    4. ANY changed file in ci/ or .github/ → infra_change
    5. Fallback → normal

  Values:
    normal             — ≤200 lines, single module
    spec_sync          — skip line limit, skip module limit; MUST touch spec/
    infra_change       — skip line limit, keep module limit; MUST touch ci/ or .github/
    emergency_override — bypass ALL limits; MUST have [emergency] title + approval

  Authorization:
    spec_sync:          NO authorization required (auto-detected, avoids deadlock)
    infra_change:       Requires PR label "approved-override" OR CHANGE_CLASS_APPROVED=true
    emergency_override: Requires above + PR_REVIEW_STATE == "APPROVED"

  Context Binding:
    - spec_sync:          changed files MUST include spec/
    - infra_change:       changed files MUST include ci/ or .github/
    - emergency_override: PR title MUST contain "[emergency]"

  Audit:
    All override usage is logged as structured JSON to CI output.
"""

import json
import os
import re
import subprocess
import sys

# ── configuration ──────────────────────────────────────────────────
MAX_CHANGED_LINES = 200
EXCLUDED_PREFIXES = ("tests/", "ci/")
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


def module_from_path(path: str) -> str | None:
    norm = _normalize(path)
    if not norm.startswith("modules/"):
        return None
    parts = norm.split("/")
    return parts[1] if len(parts) >= 2 and parts[1] else None


# ── main ───────────────────────────────────────────────────────────

def _analyze_entries(
    entries: list[tuple[int, int, str]],
) -> tuple[int, int, set[str]]:
    """Compute total_lines, excluded_lines, and modules_touched."""
    total_lines = 0
    excluded_lines = 0
    modules_touched: set[str] = set()
    for added, deleted, filepath in entries:
        changed = added + deleted
        mod = module_from_path(filepath)
        if mod:
            modules_touched.add(mod)
        if _is_excluded(filepath):
            excluded_lines += changed
        else:
            total_lines += changed
    return total_lines, excluded_lines, modules_touched


def check(diff_range: str) -> int:
    """Run scope checks.  Returns 0 on PASS, 1 on FAIL."""
    entries = get_numstat(diff_range)
    total_lines, excluded_lines, modules_touched = _analyze_entries(entries)

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


def _get_changed_files(diff_range: str) -> list[str]:
    """Return list of changed file paths from git diff --name-only."""
    result = subprocess.run(
        ["git", "diff", "--name-only", diff_range],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print("check_pr_scope: git diff --name-only failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return []
    return [_normalize(f) for f in result.stdout.splitlines() if f.strip()]


def _auto_detect_change_class(
    pr_title: str,
    changed_files: list[str],
) -> str:
    """Auto-detect CHANGE_CLASS from PR title and changed files.

    Hard rules only — no ratios, no scoring, no heuristics.

    Rule 1: "[emergency]" in PR title → emergency_override
    Rule 2: ANY file starts with "spec/" → spec_sync
    Rule 3: ANY file starts with "ci/" or ".github/" → infra_change
    Rule 4: fallback → normal
    """
    title_lower = pr_title.strip().lower()

    # Rule 1 — emergency_override (title-based, highest priority)
    if "[emergency]" in title_lower:
        return "emergency_override"

    # Rule 2 — spec_sync (file-based)
    if any(f.startswith("spec/") for f in changed_files):
        return "spec_sync"

    # Rule 3 — infra_change (file-based)
    if any(f.startswith("ci/") or f.startswith(".github/") for f in changed_files):
        return "infra_change"

    # Rule 4 — fallback
    return "normal"


def _resolve_change_class(diff_range: str) -> str:
    """Resolve CHANGE_CLASS: explicit env var OR auto-detect from files.

    Priority:
      1. Explicit CHANGE_CLASS env var (if set and valid; reject if invalid)
      2. Auto-detect from PR title + changed files
    """
    explicit = os.environ.get("CHANGE_CLASS", "").strip().lower()

    if explicit:
        if explicit in VALID_CHANGE_CLASSES:
            print(f"check_pr_scope: CHANGE_CLASS from env: {explicit}",
                  file=sys.stderr)
            return explicit
        # Explicit but invalid — return as-is so main() rejects it
        print(f"check_pr_scope: CHANGE_CLASS from env is invalid: {explicit}",
              file=sys.stderr)
        return explicit

    pr_title = os.environ.get("PR_TITLE", "")
    changed_files = _get_changed_files(diff_range)

    print(f"check_pr_scope: FILES: {changed_files}", file=sys.stderr)

    detected = _auto_detect_change_class(pr_title, changed_files)
    print(f"check_pr_scope: DETECTED CHANGE_CLASS: {detected}",
          file=sys.stderr)
    return detected


def _check_authorization(change_class: str) -> list[str]:
    """Validate that a non-normal CHANGE_CLASS has explicit approval.

    spec_sync requires NO authorization (auto-detected from files,
    avoids governance deadlock where CI must pass before approval).

    infra_change requires at least one of:
      - PR label "approved-override" present in PR_LABELS
      - CHANGE_CLASS_APPROVED=true (repo variable set by admin)

    emergency_override additionally requires:
      - At least one APPROVED review (PR_REVIEW_STATE contains "APPROVED")

    Returns list of error strings (empty if authorized).
    """
    if change_class in ("normal", "spec_sync"):
        return []

    pr_labels = {
        label.strip().lower()
        for label in os.environ.get("PR_LABELS", "").split(",")
        if label.strip()
    }
    admin_approved = (
        os.environ.get("CHANGE_CLASS_APPROVED", "").strip().lower() == "true"
    )

    errors: list[str] = []

    if not admin_approved and "approved-override" not in pr_labels:
        errors.append(
            f"CHANGE_CLASS={change_class} requires explicit authorization: "
            f"PR label 'approved-override' or CHANGE_CLASS_APPROVED=true. "
            f"Current labels: {sorted(pr_labels) if pr_labels else '<none>'}"
        )

    if change_class == "emergency_override":
        review_state = os.environ.get("PR_REVIEW_STATE", "").strip().upper()
        if review_state != "APPROVED":
            errors.append(
                f"CHANGE_CLASS=emergency_override requires at least one "
                f"APPROVED review. Current review state: "
                f"'{review_state or '<not set>'}'"
            )

    return errors


def _check_context_binding(
    change_class: str,
    changed_paths: list[str],
) -> list[str]:
    """Validate that CHANGE_CLASS matches the PR content.

    Rules:
      - emergency_override: PR title MUST contain "[emergency]"
      - spec_sync:          changed files MUST include spec/
      - infra_change:       changed files MUST include ci/ or .github/

    Returns list of error strings (empty if valid).
    """
    if change_class == "normal":
        return []

    errors: list[str] = []
    pr_title = os.environ.get("PR_TITLE", "").strip().lower()
    normalized_paths = [_normalize(p) for p in changed_paths]

    if change_class == "emergency_override":
        if "[emergency]" not in pr_title:
            errors.append(
                f"CHANGE_CLASS=emergency_override requires PR title "
                f"containing '[emergency]'. "
                f"PR title: '{pr_title or '<not set>'}'"
            )

    elif change_class == "spec_sync":
        has_spec = any(p.startswith("spec/") for p in normalized_paths)
        if not has_spec:
            errors.append(
                f"CHANGE_CLASS=spec_sync requires changes in spec/ but "
                f"none found. Changed paths: "
                f"{sorted(set(p.split('/')[0] for p in normalized_paths))}"
            )

    elif change_class == "infra_change":
        has_ci = any(
            p.startswith("ci/") or p.startswith(".github/")
            for p in normalized_paths
        )
        if not has_ci:
            errors.append(
                f"CHANGE_CLASS=infra_change requires changes in ci/ or "
                f".github/ but none found. Changed paths: "
                f"{sorted(set(p.split('/')[0] for p in normalized_paths))}"
            )

    return errors


def _emit_audit_log(
    change_class: str,
    authorization_result: str,
    context_result: str,
    validation_result: str,
) -> None:
    """Emit a structured JSON audit log line for override usage."""
    if change_class == "normal":
        return

    log_entry = {
        "event": "change_class_override",
        "change_class": change_class,
        "pr_title": os.environ.get("PR_TITLE", ""),
        "pr_labels": os.environ.get("PR_LABELS", ""),
        "authorization": authorization_result,
        "context_binding": context_result,
        "validation": validation_result,
    }
    print(f"AUDIT_LOG: {json.dumps(log_entry)}", file=sys.stderr)


def main() -> int:
    diff_range = resolve_diff_range()
    change_class = _resolve_change_class(diff_range)

    # Validate CHANGE_CLASS value
    if change_class not in VALID_CHANGE_CLASSES:
        print(f"check_pr_scope: FAIL — invalid CHANGE_CLASS '{change_class}'; "
              f"valid values: {', '.join(sorted(VALID_CHANGE_CLASSES))}",
              file=sys.stderr)
        return 1

    # For normal PRs, skip governance checks entirely
    if change_class == "normal":
        return check(diff_range)

    # ── Override path: authorization + context binding ──────────
    auth_errors = _check_authorization(change_class)
    if auth_errors:
        _emit_audit_log(change_class, "DENIED", "SKIPPED", "FAIL")
        print("check_pr_scope: FAIL — override authorization denied",
              file=sys.stderr)
        for err in auth_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    entries = get_numstat(diff_range)
    total_lines, excluded_lines, modules_touched = _analyze_entries(entries)
    changed_paths = [filepath for _, _, filepath in entries]

    context_errors = _check_context_binding(change_class, changed_paths)
    if context_errors:
        _emit_audit_log(change_class, "OK", "MISMATCH", "FAIL")
        print("check_pr_scope: FAIL — CHANGE_CLASS context mismatch",
              file=sys.stderr)
        for err in context_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    # ── Policy matrix ──────────────────────────────────────────
    # normal:             line_limit=ON,  module_limit=ON
    # spec_sync:          line_limit=OFF, module_limit=OFF
    # infra_change:       line_limit=OFF, module_limit=ON
    # emergency_override: line_limit=OFF, module_limit=OFF
    skip_line_limit = change_class in (
        "emergency_override", "spec_sync", "infra_change",
    )
    skip_module_limit = change_class in (
        "emergency_override", "spec_sync",
    )

    errors: list[str] = []
    if not skip_line_limit and total_lines > MAX_CHANGED_LINES:
        errors.append(
            f"total lines changed ({total_lines}) exceeds "
            f"{MAX_CHANGED_LINES} (excluding {', '.join(EXCLUDED_PREFIXES)})"
        )
    if not skip_module_limit and len(modules_touched) > 1:
        errors.append(
            f"PR touches {len(modules_touched)} modules "
            f"({', '.join(sorted(modules_touched))}); max 1 allowed"
        )

    if errors:
        _emit_audit_log(change_class, "OK", "OK", "FAIL")
        print("check_pr_scope: FAIL")
        for err in errors:
            print(f"  {err}")
        return 1

    _emit_audit_log(change_class, "OK", "OK", "PASS")
    bypassed = []
    if skip_line_limit:
        bypassed.append("line_limit")
    if skip_module_limit:
        bypassed.append("module_limit")
    print(f"check_pr_scope: PASS ({total_lines} lines changed"
          + (f", {excluded_lines} excluded" if excluded_lines else "")
          + f", change_class={change_class}"
          + (f", bypassed=[{','.join(bypassed)}]" if bypassed else "")
          + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
