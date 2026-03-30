from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable


def parse_spec_functions(spec_text: str) -> dict[str, list[list[str]]]:
    functions: dict[str, list[list[str]]] = {}
    current_name: str | None = None
    current_params: list[str] = []
    in_input = False

    def finalize_current() -> None:
        nonlocal current_name, current_params, in_input
        if current_name:
            signatures = functions.setdefault(current_name, [])
            if current_params not in signatures:
                signatures.append(current_params)
        current_name = None
        current_params = []
        in_input = False

    for raw_line in spec_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("Function:"):
            finalize_current()
            current_name = line.split("Function:", 1)[1].strip()
            continue

        if line.startswith("Input:"):
            in_input = True
            continue

        if line.startswith(("Output:", "Error:", "Notes:")):
            in_input = False
            continue

        if in_input:
            param = extract_param_name(line)
            if param:
                current_params.append(param)

    finalize_current()
    return functions


def extract_param_name(line: str) -> str | None:
    if line.startswith(("*", "-")):
        line = line[1:].lstrip()
    parts = line.split(":", 1)
    candidate = parts[0].strip()
    if not candidate:
        return None
    if not (candidate[0].isalpha() or candidate[0] == "_"):
        return None
    for char in candidate[1:]:
        if not (char.isalnum() or char == "_"):
            return None
    return candidate


def iter_module_paths(modules_dir: Path) -> Iterable[Path]:
    return modules_dir.rglob("main.py")


def extract_params(args: ast.arguments) -> list[str]:
    names = [arg.arg for arg in args.posonlyargs + args.args]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    names.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return names


def parse_code_functions(modules_dir: Path) -> tuple[dict[str, list[str]], list[str]]:
    functions: dict[str, list[str]] = {}
    origins: dict[str, Path] = {}
    errors: list[str] = []
    for path in iter_module_paths(modules_dir):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise ValueError(f"Syntax error in {path}: {exc}") from exc

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = extract_params(node.args)
                if node.name in functions:
                    if functions[node.name] != params:
                        errors.append(
                            f"{node.name}: multiple definitions with different parameters "
                            f"({origins[node.name]} {functions[node.name]} vs {path} {params})"
                        )
                else:
                    functions[node.name] = params
                    origins[node.name] = path
    return functions, errors


def validate_signatures(
    spec_functions: dict[str, list[list[str]]],
    code_functions: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    for name, spec_signatures in spec_functions.items():
        if name not in code_functions:
            errors.append(f"{name}: missing in modules/**/main.py")
            continue
        code_params = code_functions[name]
        if code_params in spec_signatures:
            continue
        lengths = {len(signature) for signature in spec_signatures}
        if len(code_params) not in lengths:
            expected = ", ".join(str(length) for length in sorted(lengths))
            errors.append(
                f"{name}: parameter count mismatch (spec {expected} vs code {len(code_params)})"
            )
            continue
        for signature in spec_signatures:
            if len(signature) != len(code_params):
                continue
            for index, (spec_param, code_param) in enumerate(
                zip(signature, code_params, strict=True), start=1
            ):
                if spec_param != code_param:
                    errors.append(
                        f"{name}: parameter {index} mismatch (spec {spec_param} vs code {code_param})"
                    )
                    break
            break
    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    spec_path = repo_root / "spec" / "interface.md"
    modules_dir = repo_root / "modules"

    spec_text = spec_path.read_text(encoding="utf-8")
    spec_functions = parse_spec_functions(spec_text)
    if not spec_functions:
        print("check_signature: no functions found in spec/interface.md")
        return 1

    code_functions, code_errors = parse_code_functions(modules_dir)
    if code_errors:
        for error in code_errors:
            print(error)
        return 1
    errors = validate_signatures(spec_functions, code_functions)
    if errors:
        for error in errors:
            print(error)
        return 1

    print("check_signature: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())