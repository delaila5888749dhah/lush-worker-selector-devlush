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


def main() -> int:
    errors = check_consistency()
    if errors:
        print("check_spec_consistency: FAIL — aggregated spec diverges "
              "from segmented files", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print("check_spec_consistency: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
