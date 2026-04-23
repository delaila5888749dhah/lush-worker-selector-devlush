# Blueprint Contracts

This directory contains **Blueprint-as-Code** contracts — machine-readable YAML files
that map Blueprint rules to production source files and CI-enforced tests.

## Purpose

`spec/contracts/` makes blueprint rules enforceable at CI level. Each contract YAML:

- Pins a single rule from `spec/blueprint.md` to the exact source file(s) that
  implement it.
- References existing tests that prove the rule holds at runtime.
- Is validated automatically on every PR and push to `main` by the
  `blueprint_contracts` GitHub Actions workflow.

## Contract ID Convention

Contract IDs follow the pattern used in `spec/audit-lock.md`:

```
INV-<DOMAIN>-<NN>
```

- `DOMAIN` is an uppercase identifier for the rule category (e.g. `FSM`,
  `GATEKEEPER`, `DELAY`, `ORCHESTRATOR`, `RUNTIME`).
- `NN` is a zero-padded two- or three-digit sequence number within the domain.

Examples: `INV-FSM-01`, `INV-GATEKEEPER-03`, `INV-DELAY-02`.

**Rule:** Contract IDs must be globally unique across all YAML files in this
directory. The CI runner enforces this at every run.

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Contract ID matching `^INV-[A-Z]+(-[A-Z]+)?-\d{2,3}$` |
| `priority` | enum | `CRITICAL`, `MAJOR`, or `MINOR` |
| `rule` | string | 1–2 sentence prose rule extracted verbatim from the blueprint |
| `blueprint_ref` | string | Path + anchor, e.g. `spec/blueprint.md#section-6` |
| `source_files` | `list[str]` | Production code paths (relative to repo root), min 1 |
| `enforced_by` | `list[str]` | Test node paths (see format below), min 1 |
| `fail_severity` | enum | `block_merge`, `warn`, or `info` |

## Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `audit_lock_ref` | string | Pointer to matching entry in `spec/audit-lock.md`, e.g. `spec/audit-lock.md#INV-FSM-01` |
| `rationale` | string | Additional context for why the rule exists |
| `notes` | string | Implementation notes, known gaps, or clarifications |
| `spec_version` | string | Spec version this contract targets |
| `last_updated` | string | ISO date of last update (YYYY-MM-DD) |

## `enforced_by` Format

Each entry in `enforced_by` must be one of:

```
tests/path/to/test.py                          # entire file covers the rule
tests/path/to/test.py::ClassName               # specific class
tests/path/to/test.py::ClassName::method_name  # specific test method
```

The CI runner verifies that every referenced file exists on disk. If a class or
method name is provided, it also checks (via AST) that the symbol exists in the
file.

## `fail_severity` Semantics

| Value | Effect when contract fails |
|-------|---------------------------|
| `block_merge` | Blocks merge when `--strict` flag is passed |
| `warn` | Prints warning; never blocks merge |
| `info` | Informational only |

**Guideline:** Use `block_merge` for `CRITICAL` and `MAJOR` contracts;
`warn` for `MINOR`.

## File Structure

Each contract file is named `section<N>_<topic>.yaml` and has this structure:

```yaml
section: 6
title: "Gatekeeper & Xử Lý Ngoại Lệ"
blueprint_ref: "spec/blueprint.md#section-6"
spec_version: "1.0"
last_updated: "2026-04-23"
contracts:
  - id: INV-GATEKEEPER-01
    priority: CRITICAL
    rule: "Stuck submit >3s must map to ui_lock (no raise)."
    blueprint_ref: "spec/blueprint.md#section-6"
    source_files:
      - modules/cdp/driver.py
    enforced_by:
      - tests/test_stuck_submit_guard.py
    fail_severity: block_merge
```

## Adding a New Contract

1. Pick an ID in the `INV-<DOMAIN>-<NN>` convention.
2. Add a YAML entry to the appropriate `section*.yaml` file (or create a new
   section file).
3. CI auto-discovers all `section*.yaml` files via glob — no registration needed.
4. Open a PR with label `blueprint-contracts`.

## Modifying a Contract

If the **rule semantics change** (not just wording), you must also update
`spec/blueprint.md` in the same PR with `CHANGE_CLASS=spec_sync`. If only the
`enforced_by` test paths change (e.g. a test was renamed), no blueprint update
is needed.

## Schema

Contracts are validated against `ci/contracts/contract_schema.json` on every CI
run. See that file for the full JSON Schema definition.

## CI Workflow

The `blueprint_contracts` workflow (`.github/workflows/blueprint_contracts.yml`)
runs `ci/check_blueprint_contracts.py --strict` on every PR and push to `main`.

**Note:** This workflow is informational only in Phase 1 — it is NOT a required
status check. Required-check gate will be enabled in Phase 3.
