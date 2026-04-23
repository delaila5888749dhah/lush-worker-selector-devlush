#!/usr/bin/env python3
"""Generate a shields.io endpoint badge from blueprint contract coverage.

Reads docs/blueprint_coverage.md, extracts the "Coverage" percentage from the
Summary table, and writes docs/badge.json in the shields.io endpoint format:

    {"schemaVersion": 1, "label": "blueprint coverage",
     "message": "94%", "color": "green"}

Color thresholds:
    >= 95%   green
    80-94%   yellow
    < 80%    red

Usage:
    python ci/generate_coverage_badge.py [--input PATH] [--output PATH]

Exit codes:
    0   Badge written successfully.
    1   Coverage value could not be extracted (badge not written).
    2   Input file not found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT_DIR / "docs" / "blueprint_coverage.md"
DEFAULT_OUTPUT = ROOT_DIR / "docs" / "badge.json"

# Match a Summary table row of the form:
#   | Coverage | 94% |
# (with arbitrary surrounding whitespace and case).
_COVERAGE_RE = re.compile(
    r"^\s*\|\s*Coverage\s*\|\s*(?P<pct>\d{1,3})\s*%\s*\|",
    re.IGNORECASE | re.MULTILINE,
)


def extract_coverage(text: str) -> int | None:
    """Return the integer percentage from the Summary 'Coverage' row, or None.
    """
    m = _COVERAGE_RE.search(text)
    if not m:
        return None
    try:
        pct = int(m.group("pct"))
    except ValueError:
        return None
    if pct < 0 or pct > 100:
        return None
    return pct


def color_for(pct: int) -> str:
    if pct >= 95:
        return "green"
    if pct >= 80:
        return "yellow"
    return "red"


def build_badge(pct: int) -> dict:
    return {
        "schemaVersion": 1,
        "label": "blueprint coverage",
        "message": f"{pct}%",
        "color": color_for(pct),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Coverage report markdown (default: docs/blueprint_coverage.md)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Badge JSON output path (default: docs/badge.json)",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(
            f"generate_coverage_badge: input not found: {input_path}",
            file=sys.stderr,
        )
        return 2

    text = input_path.read_text(encoding="utf-8")
    pct = extract_coverage(text)
    if pct is None:
        print(
            "generate_coverage_badge: could not extract coverage percentage "
            f"from {input_path}",
            file=sys.stderr,
        )
        return 1

    badge = build_badge(pct)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic JSON output (sorted keys, fixed separators, trailing
    # newline) so that hash comparisons across runs are stable.
    output_path.write_text(
        json.dumps(badge, sort_keys=True, separators=(", ", ": ")) + "\n",
        encoding="utf-8",
    )
    print(
        f"generate_coverage_badge: wrote {output_path} "
        f"(coverage={pct}%, color={badge['color']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
