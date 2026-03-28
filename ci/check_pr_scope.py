import os
import re
import subprocess
import sys


MODULES = ("fsm", "cdp", "billing", "watchdog")
LINE_LIMIT = 200
MODULE_PATTERNS = {
    module: re.compile(rf"(?:^|[/._-]){re.escape(module)}(?:$|[/._-])")
    for module in MODULES
}


def run_git_command(args, repo_root):
    result = subprocess.run(
        args,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("check_pr_scope: FAIL")
        print(f"FAIL: git command failed ({result.returncode}): {' '.join(args)}")
        if result.stderr.strip():
            print(f"FAIL: stderr: {result.stderr.strip()}")
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def normalize_path(path):
    normalized = path.replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if "=>" in normalized:
        normalized = re.sub(
            r"\{[^{}]* => ([^{}]*)\}",
            lambda match: match.group(1),
            normalized,
        )
        if "=>" in normalized:
            normalized = normalized.split("=>")[-1]
    return normalized.strip()


def is_test_path(path):
    return normalize_path(path).startswith("tests/")


def parse_numstat_line(line):
    parts = line.split("\t")
    if len(parts) < 3:
        return None
    added, deleted = parts[0], parts[1]
    path = normalize_path("\t".join(parts[2:]))
    return added, deleted, path


def count_changed_lines(numstat_lines):
    total = 0
    for line in numstat_lines:
        parsed = parse_numstat_line(line)
        if not parsed:
            continue
        added, deleted, path = parsed
        if is_test_path(path):
            continue
        if added == "-" or deleted == "-":
            continue
        try:
            total += int(added) + int(deleted)
        except ValueError:
            continue
    return total


def parse_name_status_lines(lines):
    paths = []
    for line in lines:
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        path = None
        if status.startswith(("R", "C")) and len(parts) >= 3:
            path = parts[2]
        elif len(parts) >= 2:
            path = parts[1]
        if path:
            paths.append(normalize_path(path))
    return paths


def detect_test_modules(path):
    matches = []
    for module in MODULES:
        if MODULE_PATTERNS[module].search(path):
            matches.append(module)
    return matches


def collect_modules(paths):
    touched = set()
    errors = []
    for path in paths:
        if path.startswith("modules/"):
            parts = path.split("/")
            if len(parts) > 1 and parts[1]:
                module = parts[1]
                if module in MODULES:
                    touched.add(module)
                else:
                    errors.append(f"unknown module in path {path}")
            continue
        if path.startswith("tests/"):
            matches = detect_test_modules(path)
            if len(matches) == 1:
                touched.add(matches[0])
            elif len(matches) > 1:
                errors.append(
                    "tests path "
                    f"{path} matches multiple modules: {', '.join(sorted(matches))}"
                )
    return touched, errors


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    numstat_lines = run_git_command(
        ["git", "diff", "--cached", "--numstat"], repo_root
    )
    name_status_lines = run_git_command(
        ["git", "diff", "--cached", "--name-status"], repo_root
    )
    if numstat_lines is None or name_status_lines is None:
        return 1

    total_lines = count_changed_lines(numstat_lines)
    modules_touched, errors = collect_modules(parse_name_status_lines(name_status_lines))

    if total_lines > LINE_LIMIT:
        errors.append(
            f"total line changes {total_lines} exceed limit {LINE_LIMIT} "
            "(excluding tests/)"
        )

    if len(modules_touched) > 1:
        errors.append(
            f"multiple modules changed: {', '.join(sorted(modules_touched))}"
        )

    if errors:
        print("check_pr_scope: FAIL")
        for message in errors:
            print(f"FAIL: {message}")
        return 1

    print("check_pr_scope: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
