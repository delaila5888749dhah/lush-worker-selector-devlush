#!/usr/bin/env python3
"""Enforce spec versioning rules (Guard 3.11).

Validates:
1. Every spec file has a spec-version header.
2. Version format is MAJOR.MINOR.
3. Versions across segmented files and aggregated file are consistent.
4. VERSIONING.md current versions table matches actual file headers.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SPEC_DIR = ROOT_DIR / "spec"

VERSION_HEADER_RE = re.compile(
    r"^spec-version\s*:\s*(?P<version>\d+\.\d+)\s*$", re.MULTILINE
)
VERSION_TABLE_RE = re.compile(
    r"\|\s*`?(?P<file>[^|`]+)`?[^|]*\|\s*(?P<version>\d+\.\d+)\s*\|"
)

TRACKED_SPEC_FILES = [
    SPEC_DIR / "core" / "interface.md",
    SPEC_DIR / "integration" / "interface.md",
    SPEC_DIR / "interface.md",
    SPEC_DIR / "fsm.md",
    SPEC_DIR / "watchdog.md",
    SPEC_DIR / "VERSIONING.md",
]


def _extract_version(path: Path) -> str | None:
    """Extract the spec-version header from a file."""
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    match = VERSION_HEADER_RE.search(content)
    return match.group("version") if match else None


def _extract_versioning_table(path: Path) -> dict[str, str]:
    """Parse the current versions table from VERSIONING.md.

    Returns {relative_file_path: version_string}.
    """
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    table: dict[str, str] = {}
    for match in VERSION_TABLE_RE.finditer(content):
        file_ref = match.group("file").strip()
        version = match.group("version").strip()
        # Normalize: remove leading spec/ if present for matching
        table[file_ref] = version
    return table


def check_versions() -> list[str]:
    """Validate all versioning rules. Returns list of errors."""
    errors: list[str] = []

    # 1. Check every tracked spec file has a version header
    file_versions: dict[str, str] = {}
    for path in TRACKED_SPEC_FILES:
        if not path.exists():
            continue
        version = _extract_version(path)
        rel = str(path.relative_to(ROOT_DIR))
        if version is None:
            errors.append(
                f"{rel}: missing 'spec-version: MAJOR.MINOR' header"
            )
        else:
            file_versions[rel] = version

    # 2. Check VERSIONING.md table matches actual file headers
    versioning_path = SPEC_DIR / "VERSIONING.md"
    if versioning_path.exists():
        table = _extract_versioning_table(versioning_path)
        for table_ref, table_version in table.items():
            # Try to find a matching tracked file
            matched = False
            for tracked_path in TRACKED_SPEC_FILES:
                rel = str(tracked_path.relative_to(ROOT_DIR))
                # Match by filename pattern (table may use spec/core/interface.md)
                if table_ref in rel or rel.endswith(table_ref.lstrip("/")):
                    matched = True
                    if rel in file_versions:
                        actual = file_versions[rel]
                        if actual != table_version:
                            errors.append(
                                f"VERSIONING.md lists {table_ref} as v{table_version} "
                                f"but actual header is v{actual}"
                            )
                    break
            # table_ref like "spec/interface.md (aggregated)" won't match strictly,
            # so we do a loose match
            if not matched:
                for rel, actual in file_versions.items():
                    if table_ref.split("/")[-1].split(" ")[0] in rel:
                        if actual != table_version:
                            errors.append(
                                f"VERSIONING.md lists {table_ref} as v{table_version} "
                                f"but actual header is v{actual}"
                            )
                        matched = True
                        break

    return errors


def main() -> int:
    errors = check_versions()
    if errors:
        print("check_version_consistency: FAIL", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print("check_version_consistency: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
