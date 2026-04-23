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
runs `ci/check_blueprint_contracts.py --strict` on every PR and push to `main`,
plus a separate `--check-change-policy` step that enforces `INV-META-01`
against the PR diff.

**Status:** this workflow is a **required status check** on `main` (enabled
in Phase 3). A PR cannot merge if any `block_merge` contract fails or if the
change-policy step reports an audit-lock miss.

The runner uses a batched `pytest` fast path with `pytest-xdist -n auto
--dist=loadfile` that completes in well under one minute on
`ubuntu-latest`. See `docs/blueprint_contracts_analysis.md` §5 for numbers.

---

## Best Practices (Phase 4 Learnings)

The points below distill what the four-phase rollout taught us about
authoring, reviewing, and evolving contracts. Follow them and your PR will
sail through review; skip them and you will be asked to change things.

### Contract ID Domain Catalog

The canonical set of domain identifiers used in `INV-<DOMAIN>-NN` IDs, as of
Phase 4:

```
ANTIDETECT    ARCH        AUDIT       BEHAVIOR    CDP
DAYNIGHT      DELAY       FORM        FSM         GATEKEEPER
INTEGRATION   META        ORCHESTRATOR  PAYMENT   PERSONA
REDIS         RUNTIME     SCALE       SESSION     TEARDOWN
WATCHDOG
```

**Rule:** reuse one of these when authoring a new contract. Do not invent a
new domain without opening a PR that also extends this list — audit-lock
cross-references rely on a stable, well-known set.

### Choosing `fail_severity`

| Priority | Default severity | When to override |
|----------|------------------|------------------|
| `CRITICAL` | `block_merge` | Never override. CRITICAL = production safety. |
| `MAJOR` | `block_merge` | Consider `warn` **only** during the first week a rule ships, to catch false positives without blocking velocity. Promote to `block_merge` once stable. |
| `MINOR` | `warn` | Use `info` for documentation-only rules. Use `block_merge` only when a MINOR rule still guards a user-visible regression. |

Drift PR-B in Phase 4 (`docs/blueprint_contracts_analysis.md` §2) is the
canonical example of the split working correctly: a MAJOR `block_merge`
contract blocks the merge while a MINOR `warn` contract in the same file
is visible in the report but does not gate the PR.

### When to write a new test vs reuse an existing one

**Default: reuse.** A contract's job is to *pin* an existing truth so that
CI can police it. If there is no existing test that proves the rule, that
is a gap in the test suite, not a contract-authoring task.

Workflow:

1. Search `tests/` for an assertion that already proves the rule.
2. If found, reference the exact nodeid in `enforced_by`.
3. If not found, **do not** add the contract. Instead, open a separate PR
   (or a commit in a TDD series) that adds the test first; land it; then
   add the contract referencing the now-existing nodeid.

Mixing test authoring with contract authoring triples PR size and dilutes
review attention away from the invariant itself.

### Common authoring mistakes (and how to avoid them)

1. **Pointing `enforced_by` at a file when a class already exists.** The
   file-level reference runs every test in the file; prefer the tightest
   nodeid (`tests/x.py::TestY::test_z`) so the contract cannot be fooled
   by an unrelated test passing.
2. **Copy-pasting a rule without updating `blueprint_ref`.** The anchor
   must point at the actual section of `spec/blueprint.md` that states
   the rule. CI does not verify the anchor exists — reviewers must.
3. **Over-broad `source_files`.** If the rule lives in one function in one
   module, list that one file. Listing five modules makes the contract
   impossible to review and guarantees it will trip `INV-META-01` on
   unrelated edits.
4. **Forgetting `spec_version` / `last_updated`.** Optional but strongly
   recommended: they make diffs across phases readable and are required
   for the deprecation process below.
5. **Choosing `fail_severity: warn` for a CRITICAL rule.** The priority
   and severity are independent fields, but a CRITICAL rule that doesn't
   block the merge is cosmetic. CI will accept it; reviewers will ask.

### Rolling back a bad contract

If a newly-merged contract is proven to be a false positive on real
traffic:

1. **Do not edit `spec/blueprint.md`.** The blueprint is the source of
   truth and is protected.
2. Demote the contract's `fail_severity` to `warn` in a one-line PR so the
   gate is immediately unblocked on main.
3. File an issue describing the false-positive root cause.
4. Either fix the rule (narrow it) or follow the deprecation process.

### Deprecating a Contract

Contracts occasionally become stale — the rule is subsumed by a stronger
rule elsewhere, or the module it guards is deleted. Remove them
gracefully:

1. **Mark it `fail_severity: info`.** This keeps the contract visible in
   `docs/blueprint_coverage.md` but removes all gating behaviour.
2. **Update the blueprint prose.** In the same PR (or the next one),
   update the corresponding paragraph in `spec/blueprint.md` to match
   the new reality.
3. **Wait one release.** At least one tagged release with the contract in
   `info` state should land on `main` before removing the YAML entry.
   This gives anyone running off a pinned tag a clear deprecation
   signal.
4. **Remove the YAML entry.** Drop the contract block from the section
   YAML. CI auto-discovers removals — no further registration is needed.

Do **not** skip step 1 and delete the YAML directly: a silent removal
breaks the deprecation audit trail and may re-open a gap the contract
was closing.

