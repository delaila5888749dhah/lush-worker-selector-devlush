#!/usr/bin/env python3
# Check for cross-module imports in changed files
import ast
import os
import re
import subprocess
import sys
from typing import NamedTuple, Optional


def find_module_names(modules_dir):
    return sorted(
        name
        for name in os.listdir(modules_dir)
        if os.path.isdir(os.path.join(modules_dir, name))
    )


def normalize_path(path):
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


REF_PATTERN = re.compile(r"^[A-Za-z0-9._/~-]+$")
DIFF_RANGE_PATTERN = re.compile(r"^[A-Za-z0-9._/~-]+\.{3}[A-Za-z0-9._/~-]+$")


def sanitize_ref(ref):
    return ref.replace("\n", " ").replace("\r", " ").strip()


def validate_ref_format(ref):
    if not ref:
        return None, "invalid git ref"
    if ref.startswith("-"):
        return None, f"invalid git ref '{sanitize_ref(ref)}'"
    if not REF_PATTERN.fullmatch(ref):
        return None, f"invalid git ref '{sanitize_ref(ref)}'"
    if ".." in ref or "/." in ref or "./" in ref or ref.startswith("/") or ref.endswith("/"):
        return None, f"invalid git ref '{sanitize_ref(ref)}'"
    return ref, ""


def verify_ref(ref):
    safe_ref, safe_error = validate_ref_format(ref)
    if safe_ref is None:
        return None, safe_error
    result = subprocess.run(
        ["git", "rev-parse", "--verify", safe_ref],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        if details:
            return None, f"git rev-parse --verify {safe_ref} failed: {details}"
        return None, f"git rev-parse --verify {safe_ref} failed"
    return result.stdout.strip(), ""


def resolve_base_ref(base_ref):
    base_sha, base_error = verify_ref(base_ref)
    if base_sha is not None:
        return base_ref, ""
    origin_ref = f"origin/{base_ref}"
    origin_sha, origin_error = verify_ref(origin_ref)
    if origin_sha is not None:
        return origin_ref, ""
    details = []
    if base_error:
        details.append(base_error)
    if origin_error:
        details.append(origin_error)
    return None, "\n".join(details)


def resolve_diff_range():
    base_ref_env = os.getenv("GITHUB_BASE_REF") or ""
    head_sha_env = os.getenv("GITHUB_HEAD_SHA") or os.getenv("GITHUB_SHA") or ""
    is_ci = os.getenv("GITHUB_ACTIONS") == "true"

    base_ref = base_ref_env.strip()
    head_sha = head_sha_env.strip()

    if base_ref and head_sha:
        base, base_error = resolve_base_ref(base_ref)
        if base is None:
            print(
                "check_import_scope: unable to resolve base ref "
                f"'{sanitize_ref(base_ref)}'",
                file=sys.stderr,
            )
            if base_error:
                print(base_error, file=sys.stderr)
            sys.exit(1)

        head_sha_resolved, head_sha_error = verify_ref(head_sha)
        if head_sha_resolved is None:
            print(
                "check_import_scope: head SHA "
                f"'{sanitize_ref(head_sha)}' could not be resolved",
                file=sys.stderr,
            )
            if head_sha_error:
                print(head_sha_error, file=sys.stderr)
            sys.exit(1)

        return f"{base}...{head_sha}"

    if is_ci:
        print(
            "check_import_scope: missing GITHUB_BASE_REF or "
            "GITHUB_HEAD_SHA/GITHUB_SHA; cannot determine diff range in CI",
            file=sys.stderr,
        )
        sys.exit(1)

    for candidate in ("origin/develop", "develop"):
        candidate_sha, _ = verify_ref(candidate)
        if candidate_sha is not None:
            return f"{candidate}...HEAD"

    head_parent_sha, _ = verify_ref("HEAD~1")
    if head_parent_sha is not None:
        return "HEAD~1...HEAD"

    print(
        "check_import_scope: unable to determine diff range; set "
        "GITHUB_BASE_REF and GITHUB_HEAD_SHA/GITHUB_SHA",
        file=sys.stderr,
    )
    sys.exit(1)


def validate_diff_range(diff_range):
    # Format-only validation: "<ref>...<ref>" using allowed characters.
    return bool(DIFF_RANGE_PATTERN.fullmatch(diff_range))


class ImportTarget(NamedTuple):
    name: Optional[str]
    is_wildcard: bool


def resolve_relative_base(current_package, level):
    if not current_package or level <= 0:
        return None
    parts = current_package.split(".")
    drop = level - 1
    if drop >= len(parts):
        return None
    if drop == 0:
        return current_package
    return ".".join(parts[:-drop])


def resolve_relative_targets(current_package, node):
    base = resolve_relative_base(current_package, node.level)
    if not base:
        return []
    if node.module:
        return [ImportTarget(name=f"{base}.{node.module}", is_wildcard=False)]
    targets = []
    for alias in node.names:
        if alias.name == "*":
            targets.append(ImportTarget(name=base, is_wildcard=True))
        else:
            targets.append(ImportTarget(name=f"{base}.{alias.name}", is_wildcard=False))
    return targets


def get_changed_files(diff_range):
    if not validate_diff_range(diff_range):
        print(
            f"check_import_scope: invalid diff range '{diff_range}'",
            file=sys.stderr,
        )
        sys.exit(1)
    result = subprocess.run(
        ["git", "diff", "--name-only", diff_range],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print("check_import_scope: git diff failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def module_from_path(path, module_names):
    normalized = normalize_path(path)
    if not normalized.startswith("modules/"):
        return None
    parts = normalized.split("/")
    if len(parts) < 2:
        return None
    module_name = parts[1]
    if module_name in module_names:
        return module_name
    return None


def current_package_from_path(path):
    normalized = normalize_path(path)
    if not normalized.endswith(".py"):
        return ""
    parts = normalized[:-3].split("/")
    if not parts:
        return ""
    parts = parts[:-1]
    if not parts:
        return ""
    return ".".join(parts)


def resolve_import_root(import_name):
    if import_name == "modules":
        return "modules"
    if import_name.startswith("modules."):
        parts = import_name.split(".")
        if len(parts) > 1 and parts[1]:
            return parts[1]
        return None
    return import_name.split(".")[0]


def iter_import_targets(node):
    if not node.module:
        return
    if node.module == "modules":
        for alias in node.names:
            if alias.name == "*":
                yield ImportTarget(name=None, is_wildcard=True)
                continue
            yield ImportTarget(name=f"{node.module}.{alias.name}", is_wildcard=False)
    else:
        yield ImportTarget(name=node.module, is_wildcard=False)


def check_import_statements(
    current_module,
    current_package,
    module_names,
    file_path,
    tree,
    errors,
    repo_root,
):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = resolve_import_root(alias.name)
                if root in module_names and root != current_module:
                    rel_path = os.path.relpath(file_path, repo_root)
                    errors.append((rel_path, node.lineno, f"imports {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                targets = resolve_relative_targets(current_package, node)
                for target in targets:
                    if target.is_wildcard and target.name == "modules":
                        rel_path = os.path.relpath(file_path, repo_root)
                        errors.append(
                            (
                                rel_path,
                                node.lineno,
                                "wildcard import from modules package is not allowed",
                            )
                        )
                        continue
                    if not target.name:
                        continue
                    root = resolve_import_root(target.name)
                    if root in module_names and root != current_module:
                        rel_path = os.path.relpath(file_path, repo_root)
                        errors.append((rel_path, node.lineno, f"imports {target.name}"))
                continue
            if not node.module:
                continue
            for target in iter_import_targets(node):
                if target.is_wildcard:
                    rel_path = os.path.relpath(file_path, repo_root)
                    errors.append(
                        (
                            rel_path,
                            node.lineno,
                            "wildcard import from modules package is not allowed",
                        )
                    )
                    continue
                if not target.name:
                    continue
                root = resolve_import_root(target.name)
                if root in module_names and root != current_module:
                    rel_path = os.path.relpath(file_path, repo_root)
                    errors.append((rel_path, node.lineno, f"imports {target.name}"))


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    modules_dir = os.path.join(repo_root, "modules")
    if not os.path.isdir(modules_dir):
        print("check_import_scope: PASS")
        return 0

    module_names = find_module_names(modules_dir)
    diff_range = resolve_diff_range()
    changed_files = get_changed_files(diff_range)

    errors = []

    for path in changed_files:
        normalized = normalize_path(path)
        if not normalized.endswith(".py"):
            continue
        module_name = module_from_path(normalized, module_names)
        if not module_name:
            continue
        current_package = current_package_from_path(normalized)
        file_path = os.path.join(repo_root, normalized)
        if not os.path.isfile(file_path):
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                content = file.read()
            tree = ast.parse(content, filename=file_path)
        except SyntaxError as exc:
            rel_path = os.path.relpath(file_path, repo_root)
            errors.append((rel_path, exc.lineno or 0, f"syntax error: {exc.msg}"))
            continue
        except (OSError, UnicodeError) as exc:
            rel_path = os.path.relpath(file_path, repo_root)
            errors.append((rel_path, 0, f"read error: {exc}"))
            continue

        check_import_statements(
            module_name,
            current_package,
            module_names,
            file_path,
            tree,
            errors,
            repo_root,
        )

    if errors:
        print("check_import_scope: FAIL")
        for file_path, line, message in errors:
            print(f"FAIL: {file_path}:{line} {message}")
        return 1

    print("check_import_scope: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())