#!/usr/bin/env python3
"""Meta-audit CI governance rules.

Outputs:
  PASS: "META_AUDIT_PASS"
  FAIL: "[META-AUDIT] <rule> | <file> | <reason>"
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT_DIR = Path(__file__).resolve().parent.parent
CI_DIR = ROOT_DIR / "ci"
MODULES_DIR = ROOT_DIR / "modules"
SPEC_DIR = ROOT_DIR / "spec"

REQUIRED_ENV_VARS = ("PR_TITLE", "CHANGE_CLASS")
DETECTION_TOKENS = ("[emergency]", "[spec-sync]", "[infra]")

REF_PATTERN = re.compile(r"^[A-Za-z0-9._/~-]+$")
SPEC_VERSION_RE = re.compile(
	r"^spec-version\s*:\s*(?P<major>\d+)\.(?P<minor>\d+)\s*$",
	re.MULTILINE,
)

FUNCTION_RE = re.compile(r"^Function\s*:\s*(?P<name>[A-Za-z_]\w*)\s*$", re.I)
INPUT_RE = re.compile(r"^Input\s*:\s*(?P<input>.*)$", re.I)
OUTPUT_RE = re.compile(r"^Output\s*:\s*(?P<output>.*)$", re.I)
MODULE_RE = re.compile(r"^##\s*Module\s*:\s*(?P<name>\w+)", re.I)


@dataclass(frozen=True)
class AuditError:
	rule: str
	file: str
	reason: str


RULES = {
	1: "RULE 1 SINGLE_SOURCE_OF_TRUTH",
	2: "RULE 2 AUTHORIZATION_SECURITY",
	3: "RULE 3 SPEC_RUNTIME_ISOLATION",
	4: "RULE 4 SPEC_LOCK_ENFORCEMENT",
	5: "RULE 5 CONTRACT_SEGMENTATION",
	6: "RULE 6 VERSIONING_ENFORCEMENT",
	7: "RULE 7 CI_FAIL_FAST",
	8: "RULE 8 AUDIT_LOG_FORMAT",
	9: "RULE 9 NO_DUPLICATE_LOGIC",
	10: "RULE 10 ENV_INPUT_VALIDATION",
}


def _normalize_path(path: str) -> str:
	normalized = path.replace("\\", "/")
	return normalized[2:] if normalized.startswith("./") else normalized


def _sanitize_ref(ref: str) -> str:
	return ref.replace("\n", " ").replace("\r", " ").strip()


def _validate_ref(ref: str) -> tuple[str | None, str]:
	if not ref or ref.startswith("-"):
		return None, f"invalid git ref '{_sanitize_ref(ref)}'"
	if not REF_PATTERN.fullmatch(ref):
		return None, f"invalid git ref '{_sanitize_ref(ref)}'"
	if ".." in ref or "/." in ref or "./" in ref:
		return None, f"invalid git ref '{_sanitize_ref(ref)}'"
	if ref.startswith("/") or ref.endswith("/"):
		return None, f"invalid git ref '{_sanitize_ref(ref)}'"
	return ref, ""


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
	result = subprocess.run(
		["git", *args],
		capture_output=True,
		text=True,
		timeout=30,
	)
	return result


def _verify_ref(ref: str) -> tuple[str | None, str]:
	safe, err = _validate_ref(ref)
	if safe is None:
		return None, err
	result = _run_git(["rev-parse", "--verify", safe])
	if result.returncode != 0:
		detail = result.stderr.strip() or result.stdout.strip()
		msg = f"git rev-parse --verify {safe} failed"
		return None, f"{msg}: {detail}" if detail else msg
	return result.stdout.strip(), ""


def _resolve_base_ref(base_ref: str) -> tuple[str | None, str]:
	base_sha, base_error = _verify_ref(base_ref)
	if base_sha is not None:
		return base_ref, ""
	origin = f"origin/{base_ref}"
	origin_sha, origin_error = _verify_ref(origin)
	if origin_sha is not None:
		return origin, ""
	details = []
	if base_error:
		details.append(base_error)
	if origin_error:
		details.append(origin_error)
	return None, "\n".join(details)


def resolve_diff_refs() -> tuple[str, str, str]:
	base_raw = (os.getenv("GITHUB_BASE_REF") or "").strip()
	head_raw = (
		os.getenv("GITHUB_HEAD_SHA")
		or os.getenv("GITHUB_SHA")
		or ""
	).strip()
	is_ci = os.getenv("GITHUB_ACTIONS") == "true"

	if base_raw and head_raw:
		base, base_error = _resolve_base_ref(base_raw)
		if base is None:
			raise RuntimeError(
				"unable to resolve base ref "
				f"'{_sanitize_ref(base_raw)}'\n{base_error}"
			)
		head_sha, head_error = _verify_ref(head_raw)
		if head_sha is None:
			raise RuntimeError(
				"head SHA could not be resolved "
				f"'{_sanitize_ref(head_raw)}'\n{head_error}"
			)
		return base, head_raw, f"{base}...{head_raw}"

	if is_ci:
		raise RuntimeError(
			"missing GITHUB_BASE_REF or GITHUB_HEAD_SHA/GITHUB_SHA"
		)

	for candidate in ("origin/main", "main", "origin/develop", "develop"):
		sha, _ = _verify_ref(candidate)
		if sha is not None:
			return candidate, "HEAD", f"{candidate}...HEAD"

	parent, _ = _verify_ref("HEAD~1")
	if parent is not None:
		return "HEAD~1", "HEAD", "HEAD~1...HEAD"

	raise RuntimeError("unable to determine diff range")


def _load_changed_files(diff_range: str) -> list[str]:
	result = _run_git(["diff", "--name-only", diff_range])
	if result.returncode != 0:
		detail = result.stderr.strip()
		raise RuntimeError(
			f"git diff --name-only failed{': ' + detail if detail else ''}"
		)
	files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
	if not files:
		raise RuntimeError("no changed files detected")
	return files


def _is_spec_path(path: str) -> bool:
	normalized = _normalize_path(path)
	return normalized == "spec" or normalized.startswith("spec/")


def _parse_labels(raw: str) -> set[str]:
	return {label.strip().lower() for label in raw.split(",") if label.strip()}


def _is_authorized() -> bool:
	labels = _parse_labels(os.environ.get("PR_LABELS", ""))
	admin_approved = os.environ.get("CHANGE_CLASS_APPROVED", "").strip().lower()
	return "approved-override" in labels or admin_approved == "true"


def _read_text(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _normalize_spec_line(line: str) -> str:
	line = line.strip()
	line = re.sub(r"^[#>]+\s*", "", line)
	line = re.sub(r"^[-*]\s+", "", line)
	return line.strip("`").strip()


def _extract_functions_from_text(text: str) -> dict[str, dict[str, object]]:
	functions: dict[str, dict[str, object]] = {}
	current_module = ""
	current_func: str | None = None
	current_params: list[str] = []
	current_output: str | None = None
	in_input = False

	def _finalize() -> None:
		nonlocal current_func, current_params, current_output, in_input
		if current_func:
			functions[current_func] = {
				"params": list(current_params),
				"output": current_output,
				"module": current_module,
			}
		current_func = None
		current_params = []
		current_output = None
		in_input = False

	for raw_line in text.splitlines():
		normalized = _normalize_spec_line(raw_line)
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

		if in_input:
			bullet = raw_line.strip()
			if not bullet.startswith(("-", "*")):
				continue
			param = bullet.lstrip("-* ").split(":")[0].strip()
			if param and (param[0].isalpha() or param[0] == "_"):
				current_params.append(param)

	_finalize()
	return functions


def _extract_spec_version(text: str) -> tuple[int, int] | None:
	match = SPEC_VERSION_RE.search(text)
	if not match:
		return None
	return int(match.group("major")), int(match.group("minor"))


def _iter_python_files(root: Path) -> Iterable[Path]:
	return sorted(root.rglob("*.py"))


def _function_contains_tokens(
	lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef
) -> bool:
	start = max(node.lineno - 1, 0)
	end = node.end_lineno or node.lineno
	body = "\n".join(lines[start:end]).lower()
	return any(token in body for token in DETECTION_TOKENS)


def _detect_change_class_functions() -> tuple[list[str], list[AuditError]]:
	detectors: list[str] = []
	errors: list[AuditError] = []
	for path in sorted(CI_DIR.glob("*.py")):
		if path.name == "meta_audit.py":
			continue
		try:
			text = _read_text(path)
		except OSError as exc:
			errors.append(
				AuditError(
					RULES[1],
					str(path.relative_to(ROOT_DIR)),
					f"read error: {exc}",
				)
			)
			continue
		lines = text.splitlines()
		try:
			tree = ast.parse(text)
		except SyntaxError as exc:
			errors.append(
				AuditError(
					RULES[1],
					str(path.relative_to(ROOT_DIR)),
					f"syntax error: {exc.msg}",
				)
			)
			continue
		for node in tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				if _function_contains_tokens(lines, node):
					detectors.append(
						f"{path.relative_to(ROOT_DIR)}:{node.name}"
					)
	return detectors, errors


def rule_single_source_of_truth() -> list[AuditError]:
	errors: list[AuditError] = []
	detect_path = CI_DIR / "detect_change_class.py"
	if detect_path.exists():
		errors.append(
			AuditError(
				RULES[1],
				str(detect_path.relative_to(ROOT_DIR)),
				"detect_change_class.py must not exist",
			)
		)

	detectors, parse_errors = _detect_change_class_functions()
	errors.extend(parse_errors)
	if len(detectors) > 1:
		errors.append(
			AuditError(
				RULES[1],
				str(CI_DIR.relative_to(ROOT_DIR)),
				"multiple CHANGE_CLASS detectors: " + ", ".join(detectors),
			)
		)
	return errors


def rule_authorization_security() -> list[AuditError]:
	errors: list[AuditError] = []
	pattern = re.compile(
		r"['\"]approved-override['\"]\s+in\s+PR_LABELS\b"
	)
	env_pattern = re.compile(
		r"['\"]approved-override['\"]\s+in\s+os\.environ\.get\(\s*['\"]PR_LABELS['\"]"
	)
	for path in sorted(CI_DIR.glob("*.py")):
		text = _read_text(path)
		for lineno, line in enumerate(text.splitlines(), start=1):
			if pattern.search(line) or env_pattern.search(line):
				errors.append(
					AuditError(
						RULES[2],
						str(path.relative_to(ROOT_DIR)),
						f"line {lineno}: string match against PR_LABELS",
					)
				)
	return errors


def rule_spec_runtime_isolation() -> list[AuditError]:
	errors: list[AuditError] = []
	for path in _iter_python_files(MODULES_DIR):
		try:
			text = _read_text(path)
		except OSError as exc:
			errors.append(
				AuditError(
					RULES[3],
					str(path.relative_to(ROOT_DIR)),
					f"read error: {exc}",
				)
			)
			continue
		try:
			tree = ast.parse(text)
		except SyntaxError as exc:
			errors.append(
				AuditError(
					RULES[3],
					str(path.relative_to(ROOT_DIR)),
					f"syntax error: {exc.msg}",
				)
			)
			continue
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				for alias in node.names:
					if alias.name == "spec" or alias.name.startswith("spec."):
						errors.append(
							AuditError(
								RULES[3],
								str(path.relative_to(ROOT_DIR)),
								f"line {node.lineno}: imports {alias.name}",
							)
						)
			elif isinstance(node, ast.ImportFrom):
				if node.module and (
					node.module == "spec" or node.module.startswith("spec.")
				):
					errors.append(
						AuditError(
							RULES[3],
							str(path.relative_to(ROOT_DIR)),
							f"line {node.lineno}: imports from {node.module}",
						)
					)
	return errors


def rule_spec_lock_enforcement(changed_files: list[str]) -> list[AuditError]:
	errors: list[AuditError] = []
	spec_files = [f for f in changed_files if _is_spec_path(f)]
	if not spec_files:
		return errors
	change_class = os.environ.get("CHANGE_CLASS", "").strip().lower()
	if change_class != "spec_sync":
		errors.append(
			AuditError(
				RULES[4],
				"spec/",
				"spec changes require CHANGE_CLASS=spec_sync",
			)
		)
	if not _is_authorized():
		errors.append(
			AuditError(
				RULES[4],
				"spec/",
				"spec changes require authorization",
			)
		)
	return errors


def rule_contract_segmentation() -> list[AuditError]:
	errors: list[AuditError] = []
	aggregated_path = SPEC_DIR / "interface.md"
	core_path = SPEC_DIR / "core" / "interface.md"
	integration_path = SPEC_DIR / "integration" / "interface.md"

	missing: list[Path] = [
		p for p in (aggregated_path, core_path, integration_path) if not p.exists()
	]
	for path in missing:
		errors.append(
			AuditError(
				RULES[5],
				str(path.relative_to(ROOT_DIR)),
				"required spec file is missing",
			)
		)
	if missing:
		return errors

	aggregated_funcs = _extract_functions_from_text(_read_text(aggregated_path))
	segmented_funcs: dict[str, dict[str, object]] = {}
	for sp in (core_path, integration_path):
		segmented_funcs.update(_extract_functions_from_text(_read_text(sp)))

	for name in sorted(aggregated_funcs):
		if name not in segmented_funcs:
			errors.append(
				AuditError(
					RULES[5],
					str(aggregated_path.relative_to(ROOT_DIR)),
					f"function '{name}' missing from segmented specs",
				)
			)
		else:
			agg = aggregated_funcs[name]
			seg = segmented_funcs[name]
			if agg["params"] != seg["params"]:
				errors.append(
					AuditError(
						RULES[5],
						str(aggregated_path.relative_to(ROOT_DIR)),
						f"function '{name}' params mismatch",
					)
				)
			if agg["output"] != seg["output"]:
				errors.append(
					AuditError(
						RULES[5],
						str(aggregated_path.relative_to(ROOT_DIR)),
						f"function '{name}' output mismatch",
					)
				)

	for name in sorted(segmented_funcs):
		if name not in aggregated_funcs:
			errors.append(
				AuditError(
					RULES[5],
					str(aggregated_path.relative_to(ROOT_DIR)),
					f"function '{name}' missing from aggregated spec",
				)
			)

	return errors


def _file_exists_at_ref(ref: str, rel_path: str) -> bool:
	result = _run_git(["cat-file", "-e", f"{ref}:{rel_path}"])
	return result.returncode == 0


def _read_text_at_ref(ref: str, rel_path: str) -> str | None:
	result = _run_git(["show", f"{ref}:{rel_path}"])
	if result.returncode != 0:
		return None
	return result.stdout


def _parse_exception_types(text: str) -> set[str]:
	types: set[str] = set()
	try:
		tree = ast.parse(text)
	except SyntaxError:
		return types
	for node in tree.body:
		if isinstance(node, ast.ClassDef):
			types.add(node.name)
	return types


def _spec_paths_at_ref(ref: str) -> list[str]:
	core_rel = "spec/core/interface.md"
	integration_rel = "spec/integration/interface.md"
	aggregated_rel = "spec/interface.md"

	has_core = _file_exists_at_ref(ref, core_rel)
	has_integration = _file_exists_at_ref(ref, integration_rel)
	if has_core or has_integration:
		paths = []
		if has_core:
			paths.append(core_rel)
		if has_integration:
			paths.append(integration_rel)
		return paths
	if _file_exists_at_ref(ref, aggregated_rel):
		return [aggregated_rel]
	return []


def _load_spec_functions_at_ref(ref: str) -> dict[str, dict[str, object]]:
	functions: dict[str, dict[str, object]] = {}
	for rel_path in _spec_paths_at_ref(ref):
		text = _read_text_at_ref(ref, rel_path)
		if text is None:
			continue
		functions.update(_extract_functions_from_text(text))
	return functions


def _detect_breaking_signature_changes(
	base_funcs: dict[str, dict[str, object]],
	head_funcs: dict[str, dict[str, object]],
) -> list[str]:
	reasons: list[str] = []
	for name, base in base_funcs.items():
		if name not in head_funcs:
			reasons.append(f"function removed: {name}")
			continue
		head = head_funcs[name]
		if base.get("params") != head.get("params"):
			reasons.append(f"params changed: {name}")
		if base.get("output") != head.get("output"):
			reasons.append(f"output changed: {name}")
	return reasons


def rule_versioning_enforcement(base_ref: str, head_ref: str) -> list[AuditError]:
	errors: list[AuditError] = []
	exception_rel = "modules/common/exceptions.py"

	base_exceptions_text = _read_text_at_ref(base_ref, exception_rel)
	head_exceptions_text = _read_text_at_ref(head_ref, exception_rel)

	base_exceptions = (
		_parse_exception_types(base_exceptions_text)
		if base_exceptions_text is not None
		else set()
	)
	head_exceptions = (
		_parse_exception_types(head_exceptions_text)
		if head_exceptions_text is not None
		else set()
	)
	exceptions_changed = base_exceptions != head_exceptions

	base_funcs = _load_spec_functions_at_ref(base_ref)
	head_funcs = _load_spec_functions_at_ref(head_ref)
	breaking_reasons = _detect_breaking_signature_changes(base_funcs, head_funcs)

	if not exceptions_changed and not breaking_reasons:
		return errors

	interface_files = [
		"spec/core/interface.md",
		"spec/integration/interface.md",
		"spec/interface.md",
	]
	for rel_path in interface_files:
		if not (
			_file_exists_at_ref(base_ref, rel_path)
			and _file_exists_at_ref(head_ref, rel_path)
		):
			continue
		base_text = _read_text_at_ref(base_ref, rel_path)
		head_text = _read_text_at_ref(head_ref, rel_path)
		if base_text is None or head_text is None:
			continue
		base_version = _extract_spec_version(base_text)
		head_version = _extract_spec_version(head_text)
		if base_version is None or head_version is None:
			errors.append(
				AuditError(
					RULES[6],
					rel_path,
					"missing spec-version header for major bump check",
				)
			)
			continue
		if head_version[0] <= base_version[0]:
			reason = "major version not incremented"
			if exceptions_changed:
				reason += " (exception types changed)"
			if breaking_reasons:
				reason += "; " + ", ".join(breaking_reasons)
			errors.append(
				AuditError(
					RULES[6],
					rel_path,
					reason,
				)
			)

	return errors


def _is_empty_check(test: ast.expr) -> bool:
	if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
		return isinstance(test.operand, ast.Name)
	if isinstance(test, ast.Compare) and len(test.ops) == 1:
		left = test.left
		right = test.comparators[0]
		if isinstance(test.ops[0], ast.Eq):
			if isinstance(left, ast.Call) and isinstance(left.func, ast.Name):
				if left.func.id == "len" and isinstance(right, ast.Constant):
					return right.value == 0
			if isinstance(left, ast.Name) and isinstance(right, (ast.List, ast.Tuple)):
				return len(right.elts) == 0
	return False


def _has_exit_call(nodes: list[ast.stmt]) -> bool:
	for stmt in nodes:
		for sub in ast.walk(stmt):
			if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
				if (
					isinstance(sub.func.value, ast.Name)
					and sub.func.value.id == "sys"
					and sub.func.attr == "exit"
				):
					if sub.args and isinstance(sub.args[0], ast.Constant):
						return sub.args[0].value == 1
					return True
			if isinstance(sub, ast.Raise):
				if isinstance(sub.exc, ast.Call) and isinstance(sub.exc.func, ast.Name):
					if sub.exc.func.id == "SystemExit":
						return True
	return False


def rule_fail_fast_definition() -> list[AuditError]:
	errors: list[AuditError] = []
	for path in sorted(CI_DIR.glob("*.py")):
		text = _read_text(path)
		try:
			tree = ast.parse(text)
		except SyntaxError as exc:
			errors.append(
				AuditError(
					RULES[7],
					str(path.relative_to(ROOT_DIR)),
					f"syntax error: {exc.msg}",
				)
			)
			continue
		for node in tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				if node.name != "_get_changed_files":
					continue
				has_guard = False
				for stmt in node.body:
					if isinstance(stmt, ast.If) and _is_empty_check(stmt.test):
						if _has_exit_call(stmt.body):
							has_guard = True
							break
				if not has_guard:
					errors.append(
						AuditError(
							RULES[7],
							str(path.relative_to(ROOT_DIR)),
							"_get_changed_files must exit on empty result",
						)
					)
	return errors


def rule_audit_log_format() -> list[AuditError]:
	errors: list[AuditError] = []
	for path in sorted(CI_DIR.glob("*.py")):
		if path.name == "meta_audit.py":
			continue
		text = _read_text(path)
		for lineno, line in enumerate(text.splitlines(), start=1):
			if "AUDIT_LOG:" in line:
				errors.append(
					AuditError(
						RULES[8],
						str(path.relative_to(ROOT_DIR)),
						f"line {lineno}: AUDIT_LOG prefix found",
					)
				)
	return errors


def rule_no_duplicate_logic() -> list[AuditError]:
	errors: list[AuditError] = []
	for path in sorted(CI_DIR.glob("*.py")):
		text = _read_text(path)
		try:
			tree = ast.parse(text)
		except SyntaxError as exc:
			errors.append(
				AuditError(
					RULES[9],
					str(path.relative_to(ROOT_DIR)),
					f"syntax error: {exc.msg}",
				)
			)
			continue
		seen: dict[str, int] = {}
		for node in tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				if node.name in seen:
					errors.append(
						AuditError(
							RULES[9],
							str(path.relative_to(ROOT_DIR)),
							f"duplicate function '{node.name}'",
						)
					)
				else:
					seen[node.name] = node.lineno
	return errors


def rule_env_validation() -> list[AuditError]:
	errors: list[AuditError] = []
	for name in REQUIRED_ENV_VARS:
		value = os.environ.get(name, "").strip()
		if not value:
			errors.append(
				AuditError(
					RULES[10],
					"ENV",
					f"missing {name}",
				)
			)
	return errors


def run_checks() -> list[AuditError]:
	errors: list[AuditError] = []

	errors.extend(rule_env_validation())
	errors.extend(rule_single_source_of_truth())
	errors.extend(rule_authorization_security())
	errors.extend(rule_spec_runtime_isolation())
	errors.extend(rule_contract_segmentation())
	errors.extend(rule_audit_log_format())
	errors.extend(rule_no_duplicate_logic())
	errors.extend(rule_fail_fast_definition())

	diff_range = None
	base_ref = ""
	head_ref = ""
	changed_files: list[str] = []
	try:
		base_ref, head_ref, diff_range = resolve_diff_refs()
	except RuntimeError as exc:
		errors.append(
			AuditError(
				RULES[7],
				"git",
				str(exc),
			)
		)

	if diff_range:
		try:
			changed_files = _load_changed_files(diff_range)
		except RuntimeError as exc:
			errors.append(
				AuditError(
					RULES[7],
					"git",
					str(exc),
				)
			)
		else:
			errors.extend(rule_spec_lock_enforcement(changed_files))

	if base_ref and head_ref:
		errors.extend(rule_versioning_enforcement(base_ref, head_ref))

	return errors


def main() -> int:
	errors = run_checks()
	if errors:
		for err in errors:
			print(f"[META-AUDIT] {err.rule} | {err.file} | {err.reason}")
		return 1
	print("META_AUDIT_PASS")
	return 0


if __name__ == "__main__":
	sys.exit(main())
