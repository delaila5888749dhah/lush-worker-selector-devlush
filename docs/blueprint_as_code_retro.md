# Blueprint-as-Code — Retrospective

**Initiative:** Make Blueprint rules enforceable at CI level.
**Repository:** `lush-givex-worker`
**Outcome:** Shipped. 97 contracts live on `main`. Required status check.
 Gate runs in under one minute. Handed off to maintenance mode.

---

## Timeline

| Phase | Goal | Status |
|-------|------|--------|
| **Phase 1** | Bootstrap schema, scaffold `ci/check_blueprint_contracts.py`, author §1–§6 | ✅ Merged |
| **Phase 2** | Author §7–§14 + `section_meta`, wire `blueprint_contracts` workflow (informational) | ✅ Merged |
| **Phase 3** | Promote to required status check; add `--check-change-policy` gate, coverage badge, PR template | ✅ Merged (#223) |
| **Phase 4** | Drift-challenge validation, runner perf, docs finalization, cleanup | ✅ This PR |

**Actual duration:** ≈ 4 weeks of wall time across four PR-sized phases. The
original rough estimate was "one phase per week" — Phase 3 needed extra time
for branch-protection configuration, Phase 4 came in on schedule.

---

## What We Built

### Contract inventory

- **97 contracts** across **14 YAML files** (13 blueprint sections +
  `section_meta`).
- **374 unique `enforced_by` test nodes** across **66 test files** — every
  contract points at at least one concrete pytest nodeid that fails if the
  rule is broken.
- **21 contract-ID domains** in the canonical set (see
  `spec/contracts/README.md` §"Contract ID Domain Catalog"):
  `GATEKEEPER, PAYMENT, RUNTIME, INTEGRATION, AUDIT, BEHAVIOR, ANTIDETECT,
  DAYNIGHT, PERSONA, FORM, SESSION, TEARDOWN, SCALE, ARCH, META, FSM,
  WATCHDOG, DELAY, ORCHESTRATOR, CDP, REDIS`.

### Test-reuse ratio

- Of the 374 referenced test nodes, **all 374** pointed at pre-existing
  tests. Phase 1–3 authored **zero** new tests explicitly for contract
  enforcement — every contract found a test that already covered the rule.
- This was a deliberate choice: contracts are meant to *pin* what we already
  believe is true, not to replace TDD. When a rule had no existing test
  coverage, we left it out of contracts and filed a follow-up issue instead
  of synthesizing a new test just to satisfy the contract shape.
- **Reuse ratio: 100 %.** If this had not been achievable, the initiative
  would have been much larger in scope.

### Tooling delivered

- `ci/check_blueprint_contracts.py` — the gate. Validates schema, checks ID
  uniqueness, verifies source files and `enforced_by` symbols exist, runs
  pytest per nodeid, writes `docs/blueprint_coverage.md`, and (when
  `--strict`) fails on any `block_merge` contract.
- `ci/contracts/contract_schema.json` — JSON Schema for the YAML shape.
- `ci/generate_coverage_badge.py` — writes `docs/badge.json` for the README.
- `.github/workflows/blueprint_contracts.yml` — required status check on PRs
  to `main`.
- **Phase 4 additions:**
  - Batched-pytest + `pytest-xdist` fast path (3 m 35 s → 58 s).
  - `docs/blueprint_contracts_analysis.md` — real-data audit report.
  - `docs/blueprint_as_code_retro.md` — this document.
  - `spec/contracts/README.md` "Best Practices" section.
  - Removed empty `tests/blueprint/section6/` scaffolding.

---

## ROI

The audit-lock workflow prior to Blueprint-as-Code required a human
reviewer (typically the tech lead) to manually walk
`spec/audit-lock.md`'s 90+ invariants against the PR diff on every
`modules/fsm/`, `modules/cdp/`, or `integration/orchestrator.py` change.

| | Before | After |
|-|--------|-------|
| Manual audit on touching a protected file | ≈ 30 min / PR | 0 min (CI enforces) |
| Protected-file PRs per week (rough avg) | 3–5 | 3–5 |
| Reviewer hours/week on audit-lock walk | ≈ 2 h | 0 h |
| Gate runtime (CI cost) | — | ≈ 1 min/run |
| Phase 1–4 engineering invest | ≈ 4 PR cycles | — |

Assuming 2 h / week reviewer time saved, the initiative pays back its
authoring cost within **~4–6 weeks** of `main` enforcement. Past that point
the ROI compounds: every new rule we pin is saved review time forever.

Intangibles:

- Any PR author now gets **immediate, specific feedback** when they touch a
  protected file. No round-trip to a reviewer.
- `docs/blueprint_coverage.md` is a single pane of glass for "does the
  repo still honour the blueprint?" — useful in incident post-mortems.
- `INV-META-01` alone has caught several audit-lock update misses during
  Phase 3/4 review that would previously have slipped through.

---

## What Went Well

1. **Starting with the schema.** `contract_schema.json` gave us a tight
   feedback loop in Phase 1 — YAMLs either validated or didn't, no bikeshedding
   about field names.
2. **Per-nodeid pytest invocation.** Early in Phase 1 we considered running
   pytest once per test file. Sticking to per-nodeid isolation meant unrelated
   drift in the same file never leaked into contract verdicts, and the
   batched fast path in Phase 4 preserved that property via
   `--dist=loadfile`.
3. **`CHANGE POLICY` driven from `spec/audit-lock.md`.** Parsing the
   protected-file list at import time from the lock file (with a
   hard-coded fallback) means the list cannot silently diverge from the
   policy document.
4. **Severity split (`block_merge` vs `warn` vs `info`).** The Phase 4
   drift challenges validated the split works: MAJOR/CRITICAL violations
   block merge, MINOR violations surface in the report without gating
   velocity.

## What We'd Do Differently

1. **Start the coverage badge in Phase 1, not Phase 3.** The badge is a
   zero-cost motivator and it took us until Phase 3 to wire it.
2. **Write the README best-practices section alongside the schema.** We
   reconstructed authoring conventions in Phase 4 that should have been
   captured as they emerged. Early contributors asked the same questions
   (severity choice, domain naming) multiple times.
3. **Pin `pytest-xdist` from day one.** The batched fast path was always
   possible — we just ran serial until Phase 4. Six minutes of CI budget
   per PR adds up; this should have been an early optimization.

---

## Team Feedback

No structured survey was run; the following is paraphrased from PR review
threads and stand-up discussion during Phase 1–4. Quotes are redacted for
privacy and lightly edited for brevity.

- "I used to forget `spec/audit-lock.md` half the time. Now CI yells at
  me before I even ask for review." — reviewer, Phase 3
- "The `enforced_by` field is the killer feature. I can jump from a rule
  to the test that proves it in one click." — on-call engineer, Phase 2
- "Please pin more rules. The §6 coverage is great; can we do §13 the
  same way?" — author of a §13 runtime PR, Phase 3 → fed directly into
  Phase 3 scope.

---

## Recommendations for Adoption Elsewhere

For other repos that want to replicate this pattern:

1. **Start from `ci/contracts/contract_schema.json`.** Copy it, rename
   domains, keep the structure. The required fields have been through four
   phases of field-testing.
2. **Don't pre-write tests.** If a rule has no existing test, file the test
   as a separate issue. Contracts should pin existing truth; mixing test
   authoring with contract authoring triples the PR size and dilutes review.
3. **Promote to required status check only after ≥ 1 week of
   informational-mode runs.** Phase 2 was "workflow runs but not required"
   for exactly this reason; it caught two schema bugs that would have been
   painful as required-check failures.
4. **Parse your `CHANGE POLICY` list from a single source of truth.** Two
   places = guaranteed drift.
5. **Budget for runner performance work in the final phase.** Batching +
   xdist is simple but needs a final phase to land safely.
6. **Ship a coverage badge in the README early.** Visible progress
   motivates contributors to add contracts in their own PRs.

---

## Handoff

- **Gate:** `blueprint_contracts` is a required status check on PRs to
  `main`. Branch protection is configured; no further action required.
- **Adding contracts:** follow `spec/contracts/README.md` §"Best Practices".
  CI auto-discovers any new `section*.yaml`.
- **Deprecating contracts:** follow the three-step process in the
  README §"Deprecating a Contract".
- **Running locally:** `python ci/check_blueprint_contracts.py --strict`
  (needs `pip install pyyaml jsonschema pytest pytest-xdist`).
- **On-call:** if the gate flakes, the fastest diagnosis is the
  `Contract-test runtime: Ns` line at the tail of the Actions log plus
  `docs/blueprint_coverage.md` in the run artifact.

Initiative closed. Thanks to everyone who reviewed 90+ YAML fragments.
