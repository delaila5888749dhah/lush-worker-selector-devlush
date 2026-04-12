#!/usr/bin/env python3
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT_DIR = Path(__file__).resolve().parent.parent
SPEC_INTERFACE_PATHS = [
    ROOT_DIR / "spec" / "core" / "interface.md",
    ROOT_DIR / "spec" / "integration" / "interface.md",
]
SPEC_FALLBACK_PATH = ROOT_DIR / "spec" / "interface.md"
MODULES_DIR = ROOT_DIR / "modules"

INLINE_FUNCTION_DEF_RE = re.compile(r"^(?:async\s+def|def)\s+\w+")
INLINE_SIGNATURE_CALL_RE = re.compile(r"^[A-Za-z_]\w*\s*\(")
FUNCTION_RE = re.compile(r"^Function\s*:\s*(?P<name>[A-Za-z_]\w*)\s*$", re.I)
INPUT_RE = re.compile(r"^Input\s*:\s*(?P<input>.*)$", re.I)
OUTPUT_RE = re.compile(r"^Output\s*:\s*(?P<output>.*)$", re.I)

EMPTY_PARAM_VALUES = frozenset({"none", "n/a", "na"})


@dataclass
class SignatureRecord:
    name: str
    params: list[str]
    output: str | None = None
    file: Path | None = None
    line: int | None = None


class SpecParseError(RuntimeError):
    pass


class ModuleParseError(RuntimeError):
    pass


def normalize_line(raw_line: str) -> str:
    line = raw_line.strip()
    if not line:
        return ""
    line = re.sub(r"^[#>]+\s*", "", line)
    line = re.sub(r"^[-*]\s+", "", line)
    return line.strip("`").strip()


def extract_params_from_args(args: ast.arguments) -> list[str]:
    params: list[str] = []
    if args.posonlyargs:
        params.extend([arg.arg for arg in args.posonlyargs])
        params.append("/")
    params.extend([arg.arg for arg in args.args])
    if args.vararg:
        params.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        params.append("*")
    params.extend([arg.arg for arg in args.kwonlyargs])
    if args.kwarg:
        params.append(f"**{args.kwarg.arg}")
    return params


def parse_params_text(params_text: str, line_no: int) -> list[str]:
    params_text = params_text.strip()
    if not params_text:
        return []
    if params_text.lower() in EMPTY_PARAM_VALUES:
        return []
    if params_text.startswith("(") and params_text.endswith(")"):
        params_text = params_text[1:-1].strip()
    try:
        module = ast.parse(f"def _f({params_text}):\n    pass")
    except SyntaxError as exc:
        raise SpecParseError(f"Invalid parameters at line {line_no}: {params_text}") from exc
    func = module.body[0]
    if not isinstance(func, ast.FunctionDef):
        raise SpecParseError(f"Invalid parameters at line {line_no}: {params_text}")
    return extract_params_from_args(func.args)


def parse_inline_signature(line: str, line_no: int) -> SignatureRecord:
    signature_line = line
    if not INLINE_FUNCTION_DEF_RE.match(signature_line):
        signature_line = f"def {signature_line}"
    if not signature_line.rstrip().endswith(":"):
        signature_line = f"{signature_line}:"
    source = f"{signature_line}\n    pass"
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise SpecParseError(f"Invalid function signature at line {line_no}: {line}") from exc
    if not module.body or not isinstance(
        module.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        raise SpecParseError(f"Invalid function signature at line {line_no}: {line}")
    func = module.body[0]
    output = ast.unparse(func.returns) if func.returns else None
    return SignatureRecord(
        name=func.name,
        params=extract_params_from_args(func.args),
        output=output,
        line=line_no,
    )


def parse_spec_signatures(path: Path | str) -> list[SignatureRecord]:
    path = Path(path)
    if not path.exists():
        raise SpecParseError(f"Spec file not found: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SpecParseError(f"Unable to read spec file: {exc}") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecParseError(f"Spec file is not valid UTF-8: {exc}") from exc

    signatures: list[SignatureRecord] = []
    seen_names: dict[str, int] = {}
    pending_index: int | None = None
    in_observability_section = False

    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        stripped = raw_line.strip()
        # Observability module sections use bullet-list documentation format
        # (not Function:/Input:/Output:) — skip them to avoid false duplicates.
        if stripped.startswith("## Module: modules.observability."):
            in_observability_section = True
            continue
        if in_observability_section and stripped.startswith("## "):
            in_observability_section = False
        if in_observability_section:
            continue

        normalized = normalize_line(raw_line)
        if not normalized:
            continue
        if INLINE_FUNCTION_DEF_RE.match(normalized) or INLINE_SIGNATURE_CALL_RE.match(normalized):
            record = parse_inline_signature(normalized, line_no)
            if record.name in seen_names:
                raise SpecParseError(
                    f"Duplicate function '{record.name}' in spec at line {line_no} "
                    f"(first defined at line {seen_names[record.name]})"
                )
            signatures.append(record)
            seen_names[record.name] = line_no
            pending_index = None
            continue

        func_match = FUNCTION_RE.match(normalized)
        if func_match:
            name = func_match.group("name")
            if name in seen_names:
                raise SpecParseError(
                    f"Duplicate function '{name}' in spec at line {line_no} "
                    f"(first defined at line {seen_names[name]})"
                )
            signatures.append(SignatureRecord(name=name, params=[], output=None, line=line_no))
            seen_names[name] = line_no
            pending_index = len(signatures) - 1
            continue

        input_match = INPUT_RE.match(normalized)
        if input_match:
            if pending_index is None:
                raise SpecParseError(f"Input without Function at line {line_no}: {normalized}")
            params = parse_params_text(input_match.group("input") or "", line_no)
            signatures[pending_index].params = params
            continue

        output_match = OUTPUT_RE.match(normalized)
        if output_match:
            if pending_index is None:
                raise SpecParseError(f"Output without Function at line {line_no}: {normalized}")
            signatures[pending_index].output = output_match.group("output").strip() or None
            continue

        if normalized.startswith("def ") or normalized.startswith("async def "):
            raise SpecParseError(f"Invalid function signature at line {line_no}: {normalized}")
        if normalized.lower().startswith("function") and not func_match:
            raise SpecParseError(
                f"Function line missing name or invalid format at line {line_no}: {normalized}"
            )
        if normalized.lower().startswith("input") and not input_match:
            raise SpecParseError(
                f"Input line missing parameters or invalid format at line {line_no}: {normalized}"
            )
        if normalized.lower().startswith("output") and not output_match:
            raise SpecParseError(
                f"Output line missing return info or invalid format at line {line_no}: {normalized}"
            )

    return signatures


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self.functions: list[SignatureRecord] = []

    def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        output = ast.unparse(node.returns) if node.returns else None
        self.functions.append(
            SignatureRecord(
                name=node.name,
                params=extract_params_from_args(node.args),
                output=output,
                file=self._file_path,
                line=node.lineno,
            )
        )

    def _visit_body(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            self.visit(stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record(node)
        self._visit_body(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record(node)
        self._visit_body(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_body(node.body)


def collect_module_functions(modules_dir: Path | str) -> list[SignatureRecord]:
    modules_dir = Path(modules_dir)
    functions: list[SignatureRecord] = []
    if not modules_dir.exists():
        return functions
    for file_path in sorted(modules_dir.rglob("*.py")):
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            raise ModuleParseError(f"Unable to read {file_path}: {exc}") from exc
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            source = raw.decode(encoding)
        except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
            raise ModuleParseError(f"Unable to read {file_path}: {exc}") from exc
        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:
            raise ModuleParseError(f"Unable to parse {file_path}: {exc}") from exc
        collector = _FunctionCollector(file_path)
        collector.visit(tree)
        functions.extend(collector.functions)
    return functions


def format_signature(name: str, params: list[str], output: str | None = None) -> str:
    display_params = params
    if display_params and all(param in {"*", "/"} for param in display_params):
        display_params = []
    signature = f"{name}({', '.join(display_params)})"
    if output:
        signature = f"{signature} -> {output}"
    return signature


def format_location(record: SignatureRecord) -> str:
    if record.file and record.line:
        return f"{record.file}:{record.line}"
    if record.file:
        return f"{record.file}"
    if record.line:
        return f"spec:{record.line}"
    return "spec"


def compare_signatures(
    spec_records: Iterable[SignatureRecord],
    module_records: Iterable[SignatureRecord],
) -> list[str]:
    spec_list = list(spec_records)
    module_list = list(module_records)
    errors: list[str] = []
    max_len = max(len(spec_list), len(module_list))

    for index in range(max_len):
        spec = spec_list[index] if index < len(spec_list) else None
        actual = module_list[index] if index < len(module_list) else None

        if spec is None:
            if actual is not None:
                actual_signature = format_signature(actual.name, actual.params, actual.output)
                errors.append(
                    f"{format_location(actual)}: {actual.name}\n"
                    f"  expected: no specification found; add entry to spec/interface.md\n"
                    f"  actual:   {actual_signature}"
                )
            continue
        if actual is None:
            expected_signature = format_signature(spec.name, spec.params, spec.output)
            errors.append(
                f"{format_location(spec)}: {spec.name}\n"
                f"  expected: {expected_signature}\n"
                f"  actual:   <missing implementation>"
            )
            continue

        expected_signature = format_signature(spec.name, spec.params, spec.output)
        actual_signature = format_signature(actual.name, actual.params, actual.output)

        if spec.name != actual.name:
            errors.append(
                f"{format_location(actual)}: {actual.name}\n"
                f"  expected: {expected_signature}\n"
                f"  actual:   {actual_signature}"
            )
            continue
        if spec.params != actual.params:
            errors.append(
                f"{format_location(actual)}: {actual.name}\n"
                f"  expected: {expected_signature}\n"
                f"  actual:   {actual_signature}"
            )
            continue
        if spec.output is not None and spec.output != actual.output:
            errors.append(
                f"{format_location(actual)}: {actual.name}\n"
                f"  expected: {expected_signature}\n"
                f"  actual:   {actual_signature}"
            )
            continue
    return errors


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


def resolve_spec_paths() -> list[Path]:
    """Return the list of interface spec files to validate.

    Prefers the segmented files under spec/core/ and spec/integration/.
    Falls back to the aggregated spec/interface.md if segmented files are
    missing.
    """
    existing = [p for p in SPEC_INTERFACE_PATHS if p.exists()]
    if existing:
        return existing
    if SPEC_FALLBACK_PATH.exists():
        return [SPEC_FALLBACK_PATH]
    return []


def _check_duplicate_across_files(
    all_signatures: list[SignatureRecord],
    spec_paths: list[Path],
) -> list[str]:
    """Detect duplicate function names across different spec files.

    Returns list of error strings.
    """
    errors: list[str] = []
    seen: dict[str, Path] = {}
    for sig in all_signatures:
        source_file = sig.file
        if sig.name in seen and seen[sig.name] != source_file:
            errors.append(
                f"Duplicate function '{sig.name}' defined in both "
                f"{seen[sig.name].relative_to(ROOT_DIR)} and "
                f"{source_file.relative_to(ROOT_DIR) if source_file else 'unknown'}"
            )
        elif sig.name not in seen:
            seen[sig.name] = source_file
    return errors
def _check_aggregated_consistency(
    segmented_funcs: dict[str, list[list[str]]],
) -> list[str]:
    """Verify the aggregated file does not diverge from segmented sources.

    Returns a list of warning messages (non-fatal).  An empty list means
    the aggregated file is consistent or absent.
    """
    if not SPEC_FALLBACK_PATH.exists():
        return []
    # Only check when segmented files are the primary source
    if not any(p.exists() for p in SPEC_INTERFACE_PATHS):
        return []
    agg_text = SPEC_FALLBACK_PATH.read_text(encoding="utf-8")
    agg_funcs = parse_spec_functions(agg_text)

    warnings: list[str] = []
    seg_names = set(segmented_funcs.keys())
    agg_names = set(agg_funcs.keys())

    missing_in_agg = seg_names - agg_names
    extra_in_agg = agg_names - seg_names

    if missing_in_agg:
        warnings.append(
            f"spec/interface.md is MISSING functions present in segmented "
            f"files: {', '.join(sorted(missing_in_agg))}. "
            f"Update spec/interface.md to match."
        )
    if extra_in_agg:
        warnings.append(
            f"spec/interface.md has EXTRA functions not in segmented "
            f"files: {', '.join(sorted(extra_in_agg))}. "
            f"Update spec/interface.md to match."
        )
    return warnings


def main() -> int:
    spec_paths = resolve_spec_paths()
    if not spec_paths:
        print("check_signature: no interface spec files found", file=sys.stderr)
        return 1

    print(f"check_signature: reading specs from: "
          f"{', '.join(str(p.relative_to(ROOT_DIR)) for p in spec_paths)}",
          file=sys.stderr)

    all_spec_signatures: list[SignatureRecord] = []
    spec_texts: list[str] = []
    for sp in spec_paths:
        try:
            sigs = parse_spec_signatures(sp)
            # Tag each signature with its source file for diagnostics
            for s in sigs:
                s.file = sp
        except SpecParseError as exc:
            print(f"check_signature: error in {sp.relative_to(ROOT_DIR)}: "
                  f"{exc}", file=sys.stderr)
            return 1
        all_spec_signatures.extend(sigs)
        spec_texts.append(sp.read_text(encoding="utf-8"))

    # Cross-file duplicate detection
    dup_errors = _check_duplicate_across_files(all_spec_signatures, spec_paths)
    if dup_errors:
        print("check_signature: FAIL — duplicate functions across spec files",
              file=sys.stderr)
        for err in dup_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    all_spec_text = "\n".join(spec_texts) + "\n"

    try:
        functions = collect_module_functions(MODULES_DIR)
    except ModuleParseError as exc:
        print(f"check_signature: {exc}", file=sys.stderr)
        return 1

    compare_signatures(all_spec_signatures, functions)

    spec_functions = parse_spec_functions(all_spec_text)
    if not spec_functions:
        print("check_signature: no functions found in interface spec files")
        return 1

    # Guard 3.12: check aggregated file consistency
    divergence_warnings = _check_aggregated_consistency(spec_functions)
    for warning in divergence_warnings:
        print(f"check_signature: WARNING: {warning}", file=sys.stderr)

    modules_dir = ROOT_DIR / "modules"
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