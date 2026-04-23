#!/usr/bin/env python3
"""Ensure the aggregated spec/interface.md does not diverge from the
segmented files spec/core/interface.md and spec/integration/interface.md.

This prevents the dual-source-of-truth risk identified in the audit:
when both aggregated and segmented files exist, they MUST define the
same set of functions with the same signatures.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
AGGREGATED_PATH = ROOT_DIR / "spec" / "interface.md"
SEGMENTED_PATHS = [
    ROOT_DIR / "spec" / "core" / "interface.md",
    ROOT_DIR / "spec" / "integration" / "interface.md",
]
BLUEPRINT_PATH = ROOT_DIR / "spec" / "blueprint.md"

# §11 Synchronization Matrix row format:
#   · Spec §10.1 (Architecture) ↔ Blueprint §8.1 (Tích Hợp Thực Thi):
# or compact, single-line:
#   · Spec §12 (Audit) ↔ Blueprint §12: Status: ✓ ĐỒNG BỘ
_SYNC_ROW_RE = re.compile(
    r"Spec\s*§\s*(?P<spec>[\d.]+)[^↔]*↔\s*Blueprint\s*§\s*(?P<blue>[\d.]+)",
)
# Tolerant of leading bullets / whitespace and trailing punctuation.
_STATUS_RE = re.compile(r"Status\s*:\s*✓\s*ĐỒNG\s*BỘ", re.IGNORECASE)
_EXPECTED_STATUS = "✓ ĐỒNG BỘ"

FUNCTION_RE = re.compile(r"^Function\s*:\s*(?P<name>[A-Za-z_]\w*)\s*$", re.I)
INPUT_RE = re.compile(r"^Input\s*:\s*(?P<input>.*)$", re.I)
OUTPUT_RE = re.compile(r"^Output\s*:\s*(?P<output>.*)$", re.I)
MODULE_RE = re.compile(r"^##\s*Module\s*:\s*(?P<name>\w+)", re.I)


def _normalize(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[#>]+\s*", "", line)
    line = re.sub(r"^[-*]\s+", "", line)
    return line.strip("`").strip()


def _extract_functions(path: Path) -> dict[str, dict]:
    """Extract function definitions from a spec interface file.

    Returns {func_name: {"params": [...], "output": str, "module": str}}.
    """
    if not path.exists():
        return {}

    content = path.read_text(encoding="utf-8")
    functions: dict[str, dict] = {}
    current_module = ""
    current_func: str | None = None
    current_params: list[str] = []
    current_output: str | None = None
    in_input = False

    def _finalize():
        nonlocal current_func, current_params, current_output, in_input
        if current_func:
            functions[current_func] = {
                "params": current_params,
                "output": current_output,
                "module": current_module,
            }
        current_func = None
        current_params = []
        current_output = None
        in_input = False

    for raw_line in content.splitlines():
        normalized = _normalize(raw_line)
        if not normalized:
            continue

        mod_match = MODULE_RE.match(normalized)
        if mod_match:
            _finalize()
            current_module = mod_match.group("name")
            continue

        func_match = FUNCTION_RE.match(normalized)
        if func_match:
            _finalize()
            current_func = func_match.group("name")
            continue

        input_match = INPUT_RE.match(normalized)
        if input_match:
            in_input = True
            val = input_match.group("input").strip()
            if val.lower() not in ("none", "n/a", "na", ""):
                current_params = [p.strip() for p in val.split(",") if p.strip()]
            continue

        output_match = OUTPUT_RE.match(normalized)
        if output_match:
            in_input = False
            current_output = output_match.group("output").strip() or None
            continue

        if in_input and normalized.startswith(("-", "*")):
            param = normalized.lstrip("-* ").split(":")[0].strip()
            if param and (param[0].isalpha() or param[0] == "_"):
                current_params.append(param)

    _finalize()
    return functions


def check_consistency() -> list[str]:
    """Compare aggregated vs segmented spec files.

    Returns list of error messages (empty = consistent).
    """
    errors: list[str] = []

    existing_segmented = [p for p in SEGMENTED_PATHS if p.exists()]
    if not existing_segmented:
        # No segmented files — nothing to compare
        return []

    if not AGGREGATED_PATH.exists():
        # Segmented exists but no aggregated — that's fine (migration done)
        return []

    # Both exist — they MUST NOT diverge
    aggregated_funcs = _extract_functions(AGGREGATED_PATH)
    segmented_funcs: dict[str, dict] = {}
    for sp in existing_segmented:
        segmented_funcs.update(_extract_functions(sp))

    # Check functions in aggregated but not in segmented
    for name in sorted(aggregated_funcs):
        if name not in segmented_funcs:
            errors.append(
                f"Function '{name}' exists in {AGGREGATED_PATH.relative_to(ROOT_DIR)} "
                f"but not in any segmented spec file"
            )
        else:
            agg = aggregated_funcs[name]
            seg = segmented_funcs[name]
            if agg["params"] != seg["params"]:
                errors.append(
                    f"Function '{name}' parameter mismatch: "
                    f"aggregated={agg['params']} vs segmented={seg['params']}"
                )
            if agg["output"] != seg["output"]:
                errors.append(
                    f"Function '{name}' output mismatch: "
                    f"aggregated={agg['output']!r} vs segmented={seg['output']!r}"
                )

    # Check functions in segmented but not in aggregated
    for name in sorted(segmented_funcs):
        if name not in aggregated_funcs:
            errors.append(
                f"Function '{name}' exists in segmented spec but not in "
                f"{AGGREGATED_PATH.relative_to(ROOT_DIR)}"
            )

    return errors


# ---------------------------------------------------------------------------
# §11 Synchronization Matrix verification
# ---------------------------------------------------------------------------

def _blueprint_section_present(content: str, section: str) -> bool:
    """Check whether a Blueprint §A.B (or §A) heading appears in blueprint.md.

    Tolerates several heading styles seen in the file:
      - "§8.1. TÍCH HỢP ..." inline subsection markers
      - "11. ĐỒNG BỘ ..." top-level numbered headings
      - "12. BILLING ..." appended sections
    """
    pattern_dotted = re.compile(rf"(?m)^\s*§\s*{re.escape(section)}\b")
    if pattern_dotted.search(content):
        return True

    if "." not in section:
        pattern_top = re.compile(rf"(?m)^\s*{re.escape(section)}\.\s+\S")
        if pattern_top.search(content):
            return True
    return False


def parse_sync_matrix(blueprint_text: str) -> list[dict]:
    """Extract Synchronization Matrix rows from the blueprint text.

    Returns a list of dicts with keys: spec, blueprint, status, line_no.
    A row is a line matching ``Spec §X ↔ Blueprint §Y``. Its status is the
    first ``Status: …`` line that follows, within a small lookahead window
    (so both compact single-line and multi-line entries are captured).
    """
    rows: list[dict] = []
    lines = blueprint_text.splitlines()
    for idx, line in enumerate(lines):
        m = _SYNC_ROW_RE.search(line)
        if not m:
            continue
        status_text: str | None = None
        for j in range(idx, min(idx + 11, len(lines))):
            cand = lines[j]
            if j > idx and _SYNC_ROW_RE.search(cand):
                break
            if "Status" in cand and ":" in cand:
                status_text = cand.strip()
                break
        rows.append({
            "spec": m.group("spec"),
            "blueprint": m.group("blue"),
            "status": status_text,
            "line_no": idx + 1,
        })
    return rows


def check_sync_matrix(blueprint_path: Path = BLUEPRINT_PATH) -> list[str]:
    """Verify §11 matrix: each row references a real Blueprint section and
    carries the expected status string."""
    if not blueprint_path.exists():
        return [f"blueprint file not found: {blueprint_path}"]

    content = blueprint_path.read_text(encoding="utf-8")
    rows = parse_sync_matrix(content)
    errors: list[str] = []

    if not rows:
        errors.append(
            f"no Synchronization Matrix rows found in {blueprint_path.name}; "
            "expected entries of the form 'Spec §X.Y ↔ Blueprint §A.B'"
        )
        return errors

    for row in rows:
        loc = f"line {row['line_no']}"
        if not _blueprint_section_present(content, row["blueprint"]):
            errors.append(
                f"§11 matrix [{loc}]: Blueprint §{row['blueprint']} referenced "
                f"(Spec §{row['spec']}) but no matching section heading found "
                f"in {blueprint_path.name}"
            )
        if row["status"] is None:
            errors.append(
                f"§11 matrix [{loc}]: row 'Spec §{row['spec']} ↔ Blueprint "
                f"§{row['blueprint']}' is missing a Status line "
                f"(expected 'Status: {_EXPECTED_STATUS}')"
            )
        elif not _STATUS_RE.search(row["status"]):
            errors.append(
                f"§11 matrix [{loc}]: row 'Spec §{row['spec']} ↔ Blueprint "
                f"§{row['blueprint']}' has unexpected status "
                f"({row['status']!r}); expected 'Status: {_EXPECTED_STATUS}'"
            )

    return errors


def main() -> int:
    consistency_errors = check_consistency()
    matrix_errors = check_sync_matrix()

    if consistency_errors:
        print("check_spec_consistency: FAIL — aggregated spec diverges "
              "from segmented files", file=sys.stderr)
        for err in consistency_errors:
            print(f"  {err}", file=sys.stderr)

    if matrix_errors:
        print("check_spec_consistency: FAIL — §11 Synchronization Matrix "
              "verification failed", file=sys.stderr)
        for err in matrix_errors:
            print(f"  {err}", file=sys.stderr)

    if consistency_errors or matrix_errors:
        return 1

    print("check_spec_consistency: PASS (interface + §11 matrix)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
