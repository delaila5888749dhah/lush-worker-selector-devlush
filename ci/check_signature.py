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
SPEC_PATH = ROOT_DIR / "spec" / "interface.md"
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
    seen_names: set[str] = set()
    pending_index: int | None = None
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        normalized = normalize_line(raw_line)
        if not normalized:
            continue
        if INLINE_FUNCTION_DEF_RE.match(normalized) or INLINE_SIGNATURE_CALL_RE.match(normalized):
            record = parse_inline_signature(normalized, line_no)
            if record.name in seen_names:
                raise SpecParseError(f"Duplicate function '{record.name}' in spec at line {line_no}")
            signatures.append(record)
            seen_names.add(record.name)
            pending_index = None
            continue
        func_match = FUNCTION_RE.match(normalized)
        if func_match:
            name = func_match.group("name")
            if name in seen_names:
                raise SpecParseError(f"Duplicate function '{name}' in spec at line {line_no}")
            signatures.append(SignatureRecord(name=name, params=[], output=None, line=line_no))
            seen_names.add(name)
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
        return f"{SPEC_PATH}:{record.line}"
    return str(SPEC_PATH)
def compare_signatures(
    spec_records: Iterable[SignatureRecord],
    module_records: Iterable[SignatureRecord],
) -> list[str]:
    spec_list = list(spec_records)
    module_list = list(module_records)
    errors: list[str] = []
    max_len = max(len(spec_list), len(module_list)) if spec_list or module_list else 0
    for index in range(max_len):
        spec: SignatureRecord | None = spec_list[index] if index < len(spec_list) else None
        actual: SignatureRecord | None = module_list[index] if index < len(module_list) else None
        if spec is None and actual is not None:
            actual_signature = format_signature(actual.name, actual.params, actual.output)
            errors.append(
                f"{format_location(actual)}: {actual.name}\n"
                f"  expected: no specification found; add entry to spec/interface.md\n"
                f"  actual:   {actual_signature}"
            )
            continue
        if actual is None and spec is not None:
            expected_signature = format_signature(spec.name, spec.params, spec.output)
            errors.append(
                f"{format_location(spec)}: {spec.name}\n"
                f"  expected: {expected_signature}\n"
                f"  actual:   <missing implementation>"
            )
            continue
        if spec is None or actual is None:
            # Both are None — nothing to compare (unreachable given max_len
            # logic, kept as a narrowing guard for the type checker).
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
def main() -> int:
    try:
        spec_signatures = parse_spec_signatures(SPEC_PATH)
        functions = collect_module_functions(MODULES_DIR)
    except (SpecParseError, ModuleParseError) as exc:
        print(f"check_signature: {exc}", file=sys.stderr)
        return 1
    errors = compare_signatures(spec_signatures, functions)
    if errors:
        print("check_signature: signature mismatch detected:", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0
if __name__ == "__main__":
    sys.exit(main())
