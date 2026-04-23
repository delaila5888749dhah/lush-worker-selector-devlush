#!/usr/bin/env python3
"""Blueprint contract gate — validate spec/contracts/*.yaml and run enforced tests.

Usage:
    python ci/check_blueprint_contracts.py [options]

Options:
    --strict            Exit 1 if any block_merge contract is FAIL/ERROR.
    --contracts-dir     Directory containing section*.yaml files (default: spec/contracts).
    --output            Output path for the coverage markdown (default: docs/blueprint_coverage.md).
    --skip-tests        Validate YAML structure only; do not run pytest.

Exit codes:
    0   All contracts pass (or --strict not set).
    1   One or more block_merge contracts fail/error (only when --strict).
    2   Fatal: schema validation error, duplicate IDs, or missing source files.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess  # nosec B404 — subprocess used only to invoke local pytest with hard-coded args derived from validated contract YAML paths; no shell, no untrusted input.
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import yaml
    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT_DIR / "ci" / "contracts" / "contract_schema.json"
AUDIT_LOCK_PATH = ROOT_DIR / "spec" / "audit-lock.md"


# ---------------------------------------------------------------------------
# Change-policy enforcement (INV-META-01)
# ---------------------------------------------------------------------------
# PROTECTED_FILES is parsed at import time from spec/audit-lock.md so that the
# list cannot drift from the policy document. If the audit-lock file is missing
# or unreadable we fall back to a hard-coded list which MUST match
# spec/audit-lock.md#change-policy-post-audit (sync manually until INV-META-02
# is added in a future PR).
_FALLBACK_PROTECTED_FILES: tuple[str, ...] = (
    "modules/fsm/main.py",
    "modules/delay/engine.py",
    "modules/delay/wrapper.py",
    "modules/delay/temporal.py",
    "modules/delay/state.py",
    "modules/watchdog/main.py",
    "integration/orchestrator.py",
    "integration/runtime.py",
    "modules/rollout/main.py",
    "modules/cdp/main.py",
)
AUDIT_LOCK_RELATIVE = "spec/audit-lock.md"

# Match a bullet-list entry like "- `modules/fsm/main.py` (note)" — capture the
# path inside backticks. Restricted to repo-relative POSIX-style .py paths.
_PROTECTED_LINE_RE = re.compile(r"^\s*-\s*`([A-Za-z0-9_./-]+\.py)`")


def _parse_protected_files(audit_lock_path: Path) -> tuple[str, ...]:
    """Extract the CHANGE POLICY file list from spec/audit-lock.md.

    Returns the tuple of repo-relative file paths under the
    "## CHANGE POLICY (Post-Audit)" heading. Falls back to the hard-coded
    list if the file or section is missing.
    """
    try:
        text = audit_lock_path.read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_PROTECTED_FILES

    in_section = False
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                break
            if "CHANGE POLICY" in stripped.upper():
                in_section = True
            continue
        if not in_section:
            continue
        m = _PROTECTED_LINE_RE.match(line)
        if m:
            found.append(m.group(1))

    return tuple(found) if found else _FALLBACK_PROTECTED_FILES


PROTECTED_FILES: tuple[str, ...] = _parse_protected_files(AUDIT_LOCK_PATH)


def _ref_exists(ref: str) -> bool:
    try:
        proc = subprocess.run(  # nosec B603 — fixed args, ref validated by git
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _git_output(args: list[str]) -> str:
    try:
        proc = subprocess.run(  # nosec B603 — fixed git args
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _resolve_base_ref() -> str:
    """Resolve the git ref to diff against for change-policy enforcement.

    Order of preference:
      1. GITHUB_BASE_REF (set in PR context by GitHub Actions) — prefer
         "origin/<base>" if it resolves, else "<base>".
      2. ``git merge-base HEAD origin/main`` (local development).
      3. "HEAD~1" as a final fallback.
    """
    base = os.environ.get("GITHUB_BASE_REF", "").strip()
    if base:
        candidate = f"origin/{base}"
        if _ref_exists(candidate):
            return candidate
        if _ref_exists(base):
            return base

    merge_base = _git_output(["merge-base", "HEAD", "origin/main"])
    if merge_base:
        return merge_base

    return "HEAD~1"


def _changed_files(base_ref: str) -> list[str]:
    out = _git_output(["diff", "--name-only", f"{base_ref}...HEAD"])
    if not out:
        # Triple-dot may not work for every ref; fall back to two-dot.
        out = _git_output(["diff", "--name-only", f"{base_ref}..HEAD"])
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def check_change_policy(
    changed_files: list[str] | None = None,
    protected_files: tuple[str, ...] = PROTECTED_FILES,
    audit_lock_relative: str = AUDIT_LOCK_RELATIVE,
) -> tuple[int, str]:
    """Enforce INV-META-01: protected file edits require audit-lock update.

    Returns ``(exit_code, message)``. ``exit_code`` is 0 on pass, 1 on
    block_merge violation. ``changed_files`` may be supplied by tests; when
    None it is derived from ``git diff`` against the resolved base ref.
    """
    if changed_files is None:
        base = _resolve_base_ref()
        changed_files = _changed_files(base)
        ctx = f"base={base}"
    else:
        ctx = "supplied changed_files"

    changed_set = {p.strip() for p in changed_files if p.strip()}
    touched = sorted(p for p in protected_files if p in changed_set)

    if not touched:
        return 0, (
            f"check_change_policy: PASS (no protected files modified; {ctx})"
        )

    if audit_lock_relative in changed_set:
        return 0, (
            "check_change_policy: PASS — protected files modified and "
            f"{audit_lock_relative} updated. Touched: {', '.join(touched)}"
        )

    msg = (
        "check_change_policy: FAIL — INV-META-01 violation.\n"
        f"  The following audit-lock-protected file(s) were modified ({ctx}):\n"
        + "".join(f"    - {p}\n" for p in touched)
        + f"  but {audit_lock_relative} was NOT updated in this change.\n"
        "  Per spec/audit-lock.md#change-policy-post-audit, every PR touching\n"
        f"  these files MUST also update {audit_lock_relative}."
    )
    return 1, msg

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ContractResult:
    contract_id: str
    priority: str
    section: int
    section_title: str
    rule: str
    fail_severity: str
    status: str = "PENDING"   # PASS | FAIL | ERROR | SKIP
    detail: str = ""


@dataclass
class Report:
    generated_at: str = ""
    contracts: list[ContractResult] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    source_errors: list[str] = field(default_factory=list)
    enforced_by_errors: list[str] = field(default_factory=list)
    fatal_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step 1: Discover contracts
# ---------------------------------------------------------------------------

def discover_contracts(contracts_dir: Path) -> list[Path]:
    """Return sorted list of section*.yaml files under contracts_dir."""
    return sorted(contracts_dir.glob("section*.yaml"))


# ---------------------------------------------------------------------------
# Step 2: Validate schema
# ---------------------------------------------------------------------------

def _load_schema() -> dict[str, Any] | None:
    if not SCHEMA_PATH.exists():
        return None
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _manual_validate(doc: dict, filepath: Path) -> list[str]:
    """Minimal manual validation when jsonschema is unavailable."""
    errors: list[str] = []
    for top_field in ("section", "title", "blueprint_ref", "contracts"):
        if top_field not in doc:
            errors.append(f"{filepath}: missing top-level field '{top_field}'")
    contracts = doc.get("contracts", [])
    if not isinstance(contracts, list) or len(contracts) == 0:
        errors.append(f"{filepath}: 'contracts' must be a non-empty list")
        return errors
    import re
    id_pattern = re.compile(r"^INV-[A-Z]+(-[A-Z]+)?-\d{2,3}$")
    required_contract_fields = (
        "id", "priority", "rule", "blueprint_ref",
        "source_files", "enforced_by", "fail_severity",
    )
    valid_priorities = {"CRITICAL", "MAJOR", "MINOR"}
    valid_severities = {"block_merge", "warn", "info"}
    for idx, c in enumerate(contracts):
        prefix = f"{filepath}[{idx}]"
        for fld in required_contract_fields:
            if fld not in c:
                errors.append(f"{prefix}: missing required field '{fld}'")
        if "id" in c:
            if not id_pattern.match(c["id"]):
                errors.append(
                    f"{prefix}: id '{c['id']}' does not match ^INV-[A-Z]+(-[A-Z]+)?-\\d{{2,3}}$"
                )
        if "priority" in c and c["priority"] not in valid_priorities:
            errors.append(f"{prefix}: invalid priority '{c['priority']}'")
        if "rule" in c and len(str(c["rule"])) < 10:
            errors.append(f"{prefix}: 'rule' must be at least 10 characters")
        if "blueprint_ref" in c and not str(c["blueprint_ref"]).startswith("spec/"):
            errors.append(f"{prefix}: 'blueprint_ref' must start with 'spec/'")
        if "source_files" in c:
            if not isinstance(c["source_files"], list) or len(c["source_files"]) == 0:
                errors.append(f"{prefix}: 'source_files' must be a non-empty list")
        if "enforced_by" in c:
            if not isinstance(c["enforced_by"], list) or len(c["enforced_by"]) == 0:
                errors.append(f"{prefix}: 'enforced_by' must be a non-empty list")
        if "fail_severity" in c and c["fail_severity"] not in valid_severities:
            errors.append(f"{prefix}: invalid fail_severity '{c['fail_severity']}'")
    return errors


def validate_schema(
    doc: dict,
    filepath: Path,
    schema: dict[str, Any] | None,
) -> list[str]:
    """Validate a contract YAML document against the JSON schema."""
    if schema is not None and _HAS_JSONSCHEMA:
        try:
            jsonschema.validate(instance=doc, schema=schema)
            return []
        except jsonschema.ValidationError as exc:
            return [f"{filepath}: {exc.message}"]
        except jsonschema.SchemaError as exc:  # pragma: no cover
            return [f"Schema itself is invalid: {exc.message}"]
    return _manual_validate(doc, filepath)


# ---------------------------------------------------------------------------
# Step 3: Check ID uniqueness
# ---------------------------------------------------------------------------

def check_id_uniqueness(all_contracts: list[dict]) -> list[str]:
    seen: dict[str, str] = {}
    errors: list[str] = []
    for c_data in all_contracts:
        cid = c_data.get("id", "")
        origin = c_data.get("_origin", "unknown")
        if cid in seen:
            errors.append(
                f"Duplicate contract ID '{cid}' in '{origin}' "
                f"(first seen in '{seen[cid]}')"
            )
        else:
            seen[cid] = origin
    return errors


# ---------------------------------------------------------------------------
# Step 4: Verify source files
# ---------------------------------------------------------------------------

def check_source_files(contracts: list[dict]) -> list[str]:
    errors: list[str] = []
    for c in contracts:
        cid = c.get("id", "?")
        for sf in c.get("source_files", []):
            path = ROOT_DIR / sf
            if not path.exists():
                errors.append(
                    f"{cid}: source_files entry '{sf}' not found on disk"
                )
    return errors


# ---------------------------------------------------------------------------
# Step 5: Verify enforced_by (file + symbol)
# ---------------------------------------------------------------------------

def _parse_enforced_by(entry: str) -> tuple[str, str | None, str | None]:
    """Parse 'path::Class::method' → (path, class_name, method_name)."""
    parts = entry.split("::")
    test_path = parts[0]
    class_name = parts[1] if len(parts) > 1 else None
    method_name = parts[2] if len(parts) > 2 else None
    return test_path, class_name, method_name


def _symbol_exists(filepath: Path, class_name: str | None, method_name: str | None) -> bool:
    """Best-effort AST check: does class/method exist in filepath?"""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return False

    if class_name is None:
        return True  # file-level reference, file exists → OK

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            if method_name is None:
                return True
            for item in ast.walk(node):
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return True
            return False
    return False


def check_enforced_by(contracts: list[dict]) -> list[str]:
    errors: list[str] = []
    for c in contracts:
        cid = c.get("id", "?")
        for entry in c.get("enforced_by", []):
            test_path, class_name, method_name = _parse_enforced_by(entry)
            abs_path = ROOT_DIR / test_path
            if not abs_path.exists():
                errors.append(
                    f"{cid}: enforced_by '{entry}' — file '{test_path}' not found"
                )
                continue
            if class_name is not None:
                if not _symbol_exists(abs_path, class_name, method_name):
                    symbol = f"{class_name}{'::' + method_name if method_name else ''}"
                    errors.append(
                        f"{cid}: enforced_by '{entry}' — symbol '{symbol}' "
                        f"not found in '{test_path}'"
                    )
    return errors


# ---------------------------------------------------------------------------
# Step 6: Run tests
# ---------------------------------------------------------------------------

def _collect_test_nodes(contracts: list[dict]) -> list[tuple[str, str]]:
    """Return ordered list of (nodeid, test_path) for all enforced_by entries.

    Duplicates are deduplicated by nodeid so we run each precise reference once.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for c in contracts:
        for entry in c.get("enforced_by", []):
            test_path, class_name, method_name = _parse_enforced_by(entry)
            if class_name and method_name:
                node = f"{test_path}::{class_name}::{method_name}"
            elif class_name:
                node = f"{test_path}::{class_name}"
            else:
                node = test_path
            if node in seen:
                continue
            seen.add(node)
            out.append((node, test_path))
    return out


def run_tests(contracts: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Run pytest per unique nodeid from `enforced_by`.

    Running each nodeid individually (rather than an entire file) isolates
    contract verification from pre-existing drift in unrelated tests that
    happen to live in the same file — the precise reference a contract
    makes is the *only* thing that should determine its PASS/FAIL status.

    Returns:
        (results, failure_details) where:
          results[nodeid]          ∈ {"PASS", "FAIL", "ERROR"}
          failure_details[nodeid]  — short message (pytest tail) when not PASS
    """
    nodes = _collect_test_nodes(contracts)
    results: dict[str, str] = {}
    details: dict[str, str] = {}

    for nodeid, test_path in nodes:
        abs_path = ROOT_DIR / test_path
        if not abs_path.exists():
            results[nodeid] = "ERROR"
            details[nodeid] = f"test file '{test_path}' not found"
            continue

        # Pass the full nodeid (not just the file) so unrelated test failures
        # in the same file do NOT spuriously fail this contract.
        cmd = [
            sys.executable, "-m", "pytest",
            nodeid,
            "-q", "--no-header",
            "-m", "not real_browser",
            "--tb=short",
        ]
        try:
            proc = subprocess.run(  # nosec B603 — args are a fixed list; nodeid derives from validated YAML (no shell, no untrusted input).
                cmd,
                capture_output=True,
                text=True,
                cwd=str(ROOT_DIR),
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results[nodeid] = "ERROR"
            details[nodeid] = "pytest timed out after 120s"
            continue
        except (OSError, ValueError) as exc:  # pragma: no cover
            results[nodeid] = "ERROR"
            details[nodeid] = f"pytest invocation error: {exc}"
            continue

        if proc.returncode == 0:
            results[nodeid] = "PASS"
            continue

        # returncode 5 = "no tests collected" (nodeid doesn't match anything).
        # That's an ERROR (broken reference), not a FAIL.
        combined = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 5:
            results[nodeid] = "ERROR"
            details[nodeid] = _summarize_pytest_output(combined) or "no tests collected"
        elif "ModuleNotFoundError" in combined or "ImportError" in combined:
            results[nodeid] = "ERROR"
            details[nodeid] = _summarize_pytest_output(combined) or "import error"
        else:
            results[nodeid] = "FAIL"
            details[nodeid] = _summarize_pytest_output(combined) or "test failed"

    return results, details


def _summarize_pytest_output(output: str) -> str:
    """Extract a short, one-line summary from pytest output (best-effort)."""
    if not output:
        return ""
    # Look for the pytest short summary line, e.g. "FAILED tests/x.py::Class::m - AssertionError"
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED ", "ERROR ")):
            return stripped[:200]
    # Fallback: last non-empty line of output
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


# ---------------------------------------------------------------------------
# Step 7: Classify contracts
# ---------------------------------------------------------------------------

def classify_contracts(
    contracts: list[dict],
    test_results: dict[str, str],
    test_details: dict[str, str],
    source_errors: list[str],
    enforced_by_errors: list[str],
    section: int,
    section_title: str,
) -> list[ContractResult]:
    results: list[ContractResult] = []

    error_ids = {
        e.split(":")[0].strip()
        for e in source_errors + enforced_by_errors
    }

    for c in contracts:
        cid = c.get("id", "?")
        rule_text = str(c.get("rule", "")).replace("\n", " ").strip()

        cr = ContractResult(
            contract_id=cid,
            priority=c.get("priority", "?"),
            section=section,
            section_title=section_title,
            rule=rule_text,
            fail_severity=c.get("fail_severity", "warn"),
        )

        if cid in error_ids:
            cr.status = "ERROR"
            cr.detail = "missing source or enforced_by file/symbol"
            results.append(cr)
            continue

        # Determine status from test results
        per_node: list[tuple[str, str, str]] = []  # (nodeid, status, detail)
        for entry in c.get("enforced_by", []):
            test_path, class_name, method_name = _parse_enforced_by(entry)
            if class_name and method_name:
                nid = f"{test_path}::{class_name}::{method_name}"
            elif class_name:
                nid = f"{test_path}::{class_name}"
            else:
                nid = test_path
            per_node.append(
                (nid, test_results.get(nid, "PENDING"), test_details.get(nid, ""))
            )

        statuses = [s for _, s, _ in per_node]
        if not statuses:
            cr.status = "SKIP"
        elif all(s == "PASS" for s in statuses):
            cr.status = "PASS"
        elif any(s == "ERROR" for s in statuses):
            cr.status = "ERROR"
        elif any(s == "FAIL" for s in statuses):
            cr.status = "FAIL"
        else:
            cr.status = "PENDING"

        # Surface failed/errored test nodeids + one-line summary in the detail.
        if cr.status in ("FAIL", "ERROR"):
            bad = [
                f"{nid} [{status}]: {detail or 'no detail'}"
                for nid, status, detail in per_node
                if status in ("FAIL", "ERROR", "PENDING")
            ]
            cr.detail = "; ".join(bad)

        results.append(cr)

    return results


# ---------------------------------------------------------------------------
# Step 8: Generate report
# ---------------------------------------------------------------------------

def generate_report(report: Report, output_path: Path) -> None:
    """Write the markdown coverage report to output_path."""
    total = len(report.contracts)
    passed = sum(1 for c in report.contracts if c.status == "PASS")
    failed = sum(1 for c in report.contracts if c.status == "FAIL")
    errored = sum(1 for c in report.contracts if c.status == "ERROR")
    skipped = sum(1 for c in report.contracts if c.status == "SKIP")
    pct = f"{100 * passed / total:.0f}%" if total else "N/A"

    lines: list[str] = [
        "# Blueprint Coverage Report",
        "",
        f"Generated: {report.generated_at}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total contracts | {total} |",
        f"| Passed | {passed} |",
        f"| Failed | {failed} |",
        f"| Errors | {errored} |",
        f"| Skipped / Pending | {skipped} |",
        f"| Coverage | {pct} |",
        "",
    ]

    # Per-section summary
    sections: dict[int, dict] = {}
    for c in report.contracts:
        sec = c.section
        if sec not in sections:
            sections[sec] = {"title": c.section_title, "total": 0, "passed": 0, "failed": 0}
        sections[sec]["total"] += 1
        if c.status == "PASS":
            sections[sec]["passed"] += 1
        elif c.status in ("FAIL", "ERROR"):
            sections[sec]["failed"] += 1

    if sections:
        lines += [
            "## Per-Section Summary",
            "",
            "| Section | Title | Contracts | Passed | Failed |",
            "|---------|-------|-----------|--------|--------|",
        ]
        for sec_num in sorted(sections):
            s = sections[sec_num]
            lines.append(
                f"| §{sec_num} | {s['title']} | {s['total']} | {s['passed']} | {s['failed']} |"
            )
        lines.append("")

    # Full contract table
    lines += [
        "## Contract Detail",
        "",
        "| ID | Priority | §  | Rule (truncated) | Status | Severity |",
        "|----|----------|----|------------------|--------|----------|",
    ]
    for c in report.contracts:
        rule_trunc = c.rule[:80] + ("…" if len(c.rule) > 80 else "")
        lines.append(
            f"| {c.contract_id} | {c.priority} | {c.section} "
            f"| {rule_trunc} | {c.status} | {c.fail_severity} |"
        )
    lines.append("")

    # Failed contracts
    failing = [c for c in report.contracts if c.status in ("FAIL", "ERROR")]
    if failing:
        lines += [
            "## Failed Contracts Requiring Attention",
            "",
        ]
        for c in failing:
            lines += [
                f"### {c.contract_id} ({c.status})",
                "",
                f"- **Priority:** {c.priority}",
                f"- **Severity:** {c.fail_severity}",
                f"- **Rule:** {c.rule}",
                f"- **Detail:** {c.detail or 'see test output'}",
                "",
            ]

    # Fatal errors
    all_errors = (
        report.fatal_errors
        + report.schema_errors
        + report.source_errors
        + report.enforced_by_errors
    )
    if all_errors:
        lines += [
            "## Validation Errors",
            "",
        ]
        for err in all_errors:
            lines.append(f"- {err}")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:  # noqa: C901 — complexity is intentional
    parser = argparse.ArgumentParser(description="Blueprint contract gate")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any block_merge contract is FAIL/ERROR",
    )
    parser.add_argument(
        "--contracts-dir",
        default="spec/contracts",
        help="Directory containing section*.yaml files (default: spec/contracts)",
    )
    parser.add_argument(
        "--output",
        default="docs/blueprint_coverage.md",
        help="Output path for the coverage markdown (default: docs/blueprint_coverage.md)",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Validate YAML structure only; skip pytest invocation",
    )
    parser.add_argument(
        "--check-change-policy",
        action="store_true",
        help=(
            "Enforce INV-META-01: fail if any audit-lock-protected file is "
            "modified without spec/audit-lock.md also being updated. "
            "Exits 0/1 immediately and skips contract validation."
        ),
    )
    args = parser.parse_args(argv)

    if args.check_change_policy:
        code, msg = check_change_policy()
        print(msg)
        return code

    report = Report(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    # Sanity checks
    if not _HAS_YAML:  # pragma: no cover
        print("ERROR: PyYAML not installed. Run: pip install pyyaml")
        return 2

    contracts_dir = ROOT_DIR / args.contracts_dir
    output_path = ROOT_DIR / args.output

    # Step 1: Discover
    yaml_files = discover_contracts(contracts_dir)
    if not yaml_files:
        print(f"WARNING: no section*.yaml files found in {contracts_dir}")

    # Step 2: Load + validate schema
    schema = _load_schema()
    all_raw_contracts: list[dict] = []
    section_meta: list[tuple[int, str]] = []
    had_load_error = False

    for yaml_path in yaml_files:
        with yaml_path.open(encoding="utf-8") as f:
            try:
                doc = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                report.schema_errors.append(f"{yaml_path}: YAML parse error: {exc}")
                had_load_error = True
                continue

        errors = validate_schema(doc, yaml_path, schema)
        if errors:
            report.schema_errors.extend(errors)
            had_load_error = True
            continue

        sec_num = int(doc.get("section", 0))
        sec_title = str(doc.get("title", ""))
        section_meta.append((sec_num, sec_title))

        for c in doc.get("contracts", []):
            c["_section"] = sec_num
            c["_section_title"] = sec_title
            c["_origin"] = str(yaml_path.relative_to(ROOT_DIR))
            all_raw_contracts.append(c)

    if had_load_error:
        print("FATAL: schema validation errors. Aborting.")
        for e in report.schema_errors:
            print(f"  {e}")
        return 2

    # Step 3: Uniqueness
    dup_errors = check_id_uniqueness(all_raw_contracts)
    if dup_errors:
        report.fatal_errors.extend(dup_errors)
        for e in dup_errors:
            print(f"FATAL: {e}")
        return 2

    # Step 4: Source files
    source_errors = check_source_files(all_raw_contracts)
    report.source_errors = source_errors
    if source_errors:
        for e in source_errors:
            print(f"ERROR: {e}")

    # Step 5: enforced_by
    enforced_errors = check_enforced_by(all_raw_contracts)
    report.enforced_by_errors = enforced_errors
    if enforced_errors:
        for e in enforced_errors:
            print(f"ERROR: {e}")

    # Step 6: Run tests (unless --skip-tests)
    test_results: dict[str, str] = {}
    test_details: dict[str, str] = {}
    if not args.skip_tests:
        print("Running pytest for enforced_by test nodes (per-nodeid isolation)...")
        test_results, test_details = run_tests(all_raw_contracts)
    else:
        print("--skip-tests: skipping pytest invocation")
        for c in all_raw_contracts:
            for entry in c.get("enforced_by", []):
                test_results[entry] = "SKIP"

    # Step 7: Classify
    for c in all_raw_contracts:
        sec = c.get("_section", 0)
        sec_title = c.get("_section_title", "")
        cr_list = classify_contracts(
            [c], test_results, test_details,
            source_errors, enforced_errors, sec, sec_title,
        )
        report.contracts.extend(cr_list)

    # Step 8: Generate report
    generate_report(report, output_path)
    print(f"Coverage report written to {output_path.relative_to(ROOT_DIR)}")

    # Summary
    total = len(report.contracts)
    passed = sum(1 for c in report.contracts if c.status == "PASS")
    failed = sum(1 for c in report.contracts if c.status == "FAIL")
    errored = sum(1 for c in report.contracts if c.status == "ERROR")
    pct = f"{100 * passed / total:.0f}%" if total else "N/A"
    print(
        f"Contracts: {total} total | {passed} passed | {failed} failed | "
        f"{errored} errored | {pct} coverage"
    )

    if args.strict:
        blocking = [
            c for c in report.contracts
            if c.status in ("FAIL", "ERROR") and c.fail_severity == "block_merge"
        ]
        if blocking or source_errors or enforced_errors or report.fatal_errors:
            print(
                f"STRICT: {len(blocking)} block_merge contract(s) failed. Exiting 1."
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
