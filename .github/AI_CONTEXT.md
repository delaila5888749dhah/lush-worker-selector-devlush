# AI_CONTEXT.md — AI Debug, Fix, and Review Protocol

## 0. Purpose

This file defines the standing operating protocol for AI agents working in this repository.

It is for:

- GitHub Copilot Coding Agent
- Primary AI reviewer/debugger
- Independent secondary AI reviewer/debugger
- Human maintainers

This file is not runtime code. It does not control the bot directly.

It controls how AI agents must:

```text
- understand new issues
- analyze logs
- form and challenge hypotheses
- implement fixes
- review PRs
- stay aligned with blueprint/spec/contracts
- avoid lazy or unsafe reviews
```

GitHub Issues, Pull Requests, latest human instructions, smoke logs, tests, and blueprint/spec files are the source of truth for each specific task.

This file intentionally avoids hard-coding one current bug, one active issue, one model version, or one temporary incident as permanent repository truth.

---

## 0.1 Risk Classification

Agents must classify every task before choosing review depth.

```text
TRIVIAL:
  - docs-only changes
  - comment-only changes
  - typo / formatting only
  - test-only changes that do not alter production behavior expectations

NON-TRIVIAL (default):
  - any production code change
  - any test that changes assertions about production behavior
  - any config/env/CI change
  - any diagnostics/logging change
  - any exception/message behavior change

HIGH-RISK:
  - browser automation / CDP / Selenium / selectors
  - checkout / cart / payment / card / CVV / VBV / 3DS
  - session / cookie / storage / fingerprint / anti-detect
  - DelayEngine / timing / pacing / scrolling / typing / blur / click behavior
  - orchestrator / worker runtime / retry / watchdog / exception hierarchy
  - PII handling / logging / screenshots / diagnostics
  - billing pool / proxy pool / BitBrowser profiles
  - blueprint/spec/contracts
```

If uncertain, classify as `NON-TRIVIAL`.

If the task touches runtime automation, checkout, payment, anti-detect, PII, or exception handling, classify as `HIGH-RISK`.

---

## 1. Context Precedence

For every task, AI agents must use this precedence order:

```text
1. Latest explicit human instruction in the current conversation
2. Current GitHub Issue body and comments
3. Current PR description and review comments
4. Latest smoke logs / screenshots / diagnostics
5. Current repository code
6. Blueprint/spec/contracts
7. This AI_CONTEXT.md standing protocol
```

Blueprint/spec/contracts are binding constraints. If a human request or proposed fix conflicts with blueprint/spec/contracts, the agent must report the conflict instead of silently violating the design.

This file must not be used to override a newer issue, PR, human instruction, or smoke log.

---

## 2. Current Debug Packet Requirement

Before implementing or reviewing any non-trivial fix, the AI must build a “Current Debug Packet”.

The packet must identify:

```text
- Task / Issue / PR being handled
- User-reported symptom
- Latest logs or diagnostics
- Confirmed facts
- Active hypotheses
- Disproved hypotheses
- Relevant code paths
- Relevant blueprint/spec/contracts
- Expected fix scope
- Explicit out-of-scope areas
```

The packet does not need to be a separate file, but the AI’s reasoning, implementation plan, or review must clearly reflect it.

For bot-debugging tasks, do not proceed with implementation or approval if the current debug packet is unclear.

---

## 2.1 Debug Packet Template

Recommended format:

```yaml
task: "<issue/PR number or human request>"
risk_classification: "<trivial|non-trivial|high-risk>"

symptom: "<one-line failure description>"

latest_evidence:
  - id: E1
    source: "<log|screenshot|diagnostic|test|code>"
    detail: "<evidence summary or exact relevant line>"

confirmed_facts:
  - fact: "<confirmed statement>"
    evidence: ["E1"]

active_hypotheses:
  - id: H1
    claim: "<hypothesis>"
    supports: ["E1"]
    contradicts: []

disproved_hypotheses:
  - id: H0
    claim: "<old hypothesis>"
    disproved_by: ["E1"]
    note: "<why it is no longer valid>"

relevant_code:
  - "<path>"
  - "<path#function-or-class>"

relevant_tests:
  - "<path>"
  - "<test class/function>"

relevant_blueprint_or_spec:
  - "<path or contract id>"

proposed_fix_scope: |
  <one paragraph describing minimal intended fix>

out_of_scope:
  - "<explicitly excluded area>"
  - "<explicitly excluded issue>"
```

Two independent reviewers should be able to compare their Debug Packets and identify the exact point of agreement or disagreement.

---

## 3. Critical Thinking Rule

AI agents must distinguish:

```text
Symptom:
  What failed.

Observation:
  What logs, screenshots, diagnostics, or tests show.

Hypothesis:
  Possible explanation, not yet proven.

Confirmed fact:
  Proven by logs, tests, code, or diagnostics.

Inferred risk:
  A plausible risk that needs test or diagnostic confirmation.

Fix:
  Minimal change that addresses confirmed or strongly supported cause.

Verification:
  Test, CI, smoke, or diagnostic proving the fix.
```

Agents must not treat a hypothesis as a confirmed fact.

If new diagnostics disprove an old hypothesis, stop fixing the old hypothesis and update the plan.

---

## 4. Parallel AI Debug Protocol

For high-risk bot-debugging work, especially browser automation, checkout, payment, session, anti-detect, or timing behavior, review/debugging should use two independent AI reviewer roles:

```text
Reviewer A: Primary reasoning model
Reviewer B: Independent secondary reasoning model
```

The human operator is responsible for invoking both reviewers.

This protocol is advisory and process-level. It is not automatically enforced by CI unless a future workflow explicitly implements it.

Both reviewers must independently inspect:

```text
- latest human request
- issue body
- logs / diagnostics
- relevant code path
- related tests
- blueprint/spec/contracts
- possible regressions
- implementation scope
```

Decision rules:

```text
APPROVED:
  Both reviewers agree there is no blocker.

REQUEST_CHANGES:
  Either reviewer identifies a valid blocker.

NEEDS_HUMAN_DECISION:
  Reviewers disagree on a material architectural, runtime, security, payment, anti-detect, or blueprint issue.
```

No high-risk PR should be merged while a material reviewer disagreement remains unresolved.

---

## 4.1 Disagreement Report Format

When reviewers disagree, use this format:

```yaml
disagreement_type: "<architecture|runtime|security|payment|spec|pii|delay|other>"

reviewer_a_position:
  claim: "<position>"
  evidence:
    - "<code/log/spec/test evidence>"

reviewer_b_position:
  claim: "<position>"
  evidence:
    - "<code/log/spec/test evidence>"

crux: "<one sentence describing what fact would resolve the disagreement>"

recommended_diagnostic:
  - "<log/test/experiment/code inspection needed>"

recommended_decision: "<merge|hold|split-pr|request-changes|escalate-to-human>"
```

The final response must include:

```text
- both positions
- exact disagreement
- evidence
- recommended decision
```

---

## 5. No-Lazy-Review Rule

AI reviewers must not approve a PR by reading only the diff.

For any non-trivial PR, the reviewer must inspect the “review cone”:

```text
1. Linked Issue and acceptance criteria
2. PR description
3. All changed files
4. Surrounding code around changed functions/classes
5. Callers and callees of changed functions
6. Related tests
7. Related blueprint/spec/contracts
8. Related runtime/orchestrator paths
9. CI/check status
10. Latest smoke logs or diagnostics.
    If none are available for a bot-debugging PR, the reviewer must explicitly
    request them or declare the review incomplete for smoke-level confidence.
```

For high-risk bot-debugging PRs, the reviewer must also perform repository-wide searches for relevant:

```text
- selectors
- env vars
- exception classes
- config names
- helper functions
- contracts/invariants
- related tests
```

The reviewer must explicitly state if any relevant area was not checked and why.

A review that only says:

```text
diff looks good
```

without verifying runtime flow, tests, and blueprint/spec alignment is incomplete.

---

## 6. Blueprint-First Fixing Rule

All fixes must stay aligned with:

```text
- blueprint behavior
- spec/contracts
- GitHub Issue acceptance criteria
- runtime safety
- PII policy
- CI rules
- orchestrator/error compatibility
```

Agents must not implement quick fixes that make smoke pass while violating architecture.

### 6.1 Absolutely forbidden

The following are never allowed:

```text
- logging raw PII
- logging raw cookie/storage values
- committing secrets, credentials, API keys, or real browser profile IDs
```

No PR description justification can override this. See Section 7.

### 6.2 Controlled exceptions requiring explicit PR justification

The following patterns require explicit justification in the PR description:

```text
- synthetic JS input/change/blur events to bypass validators
- raw Selenium send_keys on production hot paths
- raw Selenium click fallback in strict CDP paths
- resetting DelayEngine accumulator to bypass MAX_STEP_DELAY
- increasing global timeouts without scoped env/config
- changing payment/card submission behavior inside unrelated pre-card issues
- broad refactors unrelated to the issue
```

For any controlled exception above, the PR must explain:

```text
1. why the standard path failed
2. why the alternative is the minimum-risk option
3. which test covers the regression risk
4. why the change does not violate blueprint/spec/contracts
```

If a requested fix conflicts with blueprint constraints, the agent must stop and report the conflict.

---

## 7. PII Safety

Never log raw:

```text
- card number
- CVV
- email
- name
- address
- phone
- cookie values
- localStorage/sessionStorage values
- raw page text that may contain user data
- raw validation messages that may contain user data
```

Allowed diagnostics:

```text
- booleans
- counts
- lengths
- selector symbolic names
- CSS display/visibility/pointer-events
- rect width/height
- validity flags
- error categories
```

Examples:

```text
OK:
  value_len=24
  cookie_count=5
  validationMessage_len=18
  selector=SEL_RECIPIENT_EMAIL

NOT OK:
  recipient@example.com
  Jane Doe
  raw cookie/session token
```

PII safety applies to:

```text
- app logs
- tests
- diagnostics
- screenshots
- PR comments
- issue comments
- AI review output
```

---

## 8. Native Browser Interaction Rule

For browser/form automation, prefer native or CDP-style interactions matching real user behavior.

Allowed patterns:

```text
- CDP key events
- CDP mouse events
- native focus via click
- natural blur via Tab or safe non-interactive click
- DOM reads for diagnostics
```

Controlled exceptions require explicit PR justification under Section 6.

Forbidden by default:

```text
- JS dispatchEvent("input")
- JS dispatchEvent("change")
- JS dispatchEvent("blur")
- synthetic form submission shortcuts
- JS mutation of user-entered field values to bypass UI flow
```

Diagnostics may read DOM state but must not mutate user-visible form state unless the issue explicitly asks for it.

---

## 9. Delay and Human-Behavior Constraints

When adding delay, pacing, scroll, typing, blur, click, or human-like behavior, agents must respect existing delay architecture.

New behavioral delays should use or create a shared helper such as:

```text
_engine_aware_sleep(low, high, reason)
```

Required behavior:

```text
- uses persona RNG when available
- checks engine.is_delay_permitted()
- uses engine.accumulate_delay()
- scales down when remaining MAX_STEP_DELAY headroom is low
- returns actual slept delay
- logs only symbolic reason and numeric delay
- does not reset delay accumulator casually
- does not inject delay in critical/VBV/POST_ACTION states
```

Do not scatter raw `time.sleep(...)` calls unless the issue explicitly allows it or the sleep is non-behavioral and safe.

---

## 10. Error Semantics Rule

Errors must describe the real failure state.

Do not report:

```text
selector not found
```

when diagnostics show:

```text
selector present but disabled
selector present but hidden
selector present but zero-size
selector present but blocked by overlay
selector present but pointer-events none
```

For UI readiness failures, distinguish:

```text
- absent
- present but hidden
- present but disabled
- present but zero-size
- present but pointer-events none
- present but click failed
```

If adding new exception classes, preserve existing catch hierarchy and orchestrator compatibility.

Do not break:

```text
- SessionFlaggedError flow
- SelectorTimeoutError compatibility
- alerting classification
- retry/failure accounting
```

---

## 11. Diagnostics Design Rule

Diagnostics must answer the next root-cause question.

Good diagnostics help decide:

```text
- Is the element present?
- Is it visible?
- Is it disabled?
- Did storage/cookies survive?
- Did form validity pass?
- Did cart/readiness state update?
- Was the failure caused by selector miss, disabled state, overlay, timeout, or click dispatch?
```

Diagnostics must be:

```text
- PII-safe
- structured enough for logs
- stable across runs
- minimal but decisive
```

Prefer logging:

```text
present
disabled
aria_disabled
display
visibility
pointer_events
rect_w
rect_h
text_len
class_len
value_len
validity flags
counts
```

Do not log raw values.

---

## 12. Smoke Log Reasoning Rule

When smoke logs are provided, AI agents must parse and cite the decisive log lines.

For each failure, identify:

```text
- last successful step
- first failing step
- exact error
- relevant diagnostics
- hypothesis confirmed or disproved
- next minimal fix
```

Agents must not skip log analysis and jump directly to a fix.

If logs are insufficient, state what diagnostic is missing.

---

## 13. Testing Expectations

Every non-trivial PR must include or update tests.

Tests should cover:

```text
- new behavior
- failure mode that motivated the issue
- PII safety when diagnostics/logging are added
- error semantics when exceptions/messages change
- compatibility with orchestrator/runtime handling
- relevant blueprint/spec invariants
```

When a PR changes timing, scroll, click, blur, or form behavior, tests should cover:

```text
- DelayEngine budget behavior
- critical-section delay gating
- no synthetic form events
- strict CDP path compatibility
- absent vs present-but-disabled semantics when relevant
```

---

## 14. PR Scope Rule

Each PR must:

```text
- target one issue
- keep scope tight
- include tests
- avoid unrelated cleanup
- preserve PII safety
- respect blueprint/spec/contracts
- avoid changing unrelated runtime paths
```

If a proposed fix requires touching multiple domains, the agent should recommend splitting into multiple PRs unless the issue explicitly authorizes a larger change.

---

## 15. Review Output Requirements

Every AI PR review must report:

```text
- Issue scope coverage
- Runtime flow impact
- Blueprint/spec alignment
- PII safety
- Delay budget impact if relevant
- Error semantics compatibility if relevant
- Tests added/updated
- CI/check status
- Merge readiness
- Blockers or non-blocking suggestions
```

For bot-debugging PRs, the review must also state:

```text
- root cause being addressed
- whether latest logs support the fix
- whether old disproven hypotheses are avoided
- whether diagnostics are sufficient for the next smoke
```

---

## 16. Staleness and Context Hygiene

AI agents must not assume old context is still true.

Before using prior context, verify:

```text
- Is the issue still open?
- Has a newer PR merged?
- Has a newer smoke log contradicted the prior hypothesis?
- Has the human provided newer instructions?
- Does the current issue body supersede older context?
```

If context appears stale, say so and rely on the latest issue/PR/log evidence.

This file intentionally avoids permanently hard-coding one active issue as the repository-wide truth.

---

## 17. Task Type Routing

For each new human request, agents must classify the task and apply the relevant sections.

```text
Analyze logs:
  Required: Sections 2, 3, 7, 11, 12, 16

Review issue:
  Required: Sections 1, 2, 3, 5, 6, 16

Review PR:
  Required: Sections 2, 3, 5, 6, 10, 11, 13, 15, 16

Create PR / implement fix:
  Required: Sections 1, 2, 3, 6, 7, 8, 9, 13, 14, 16

Modify issue:
  Required: Sections 1, 2, 3, 16

Explain code:
  Required: Sections 1, 3, 5 as applicable
  Must remain read-only unless the human explicitly asks for a change.

Update AI_CONTEXT.md:
  Required: Sections 18, 19, 21
  Must be explicitly requested by the human.
```

Do not assume the task is related to a previous bug unless the user, issue, PR, or logs clearly connect it.

---

## 18. Human-in-the-Loop Rule

The human operator may provide:

```text
- smoke logs
- screenshots
- visual observations
- issue priorities
- approval to create PRs
- approval to update issues
- approval to update this file
```

AI agents must incorporate human observations as evidence, but still validate them against code, logs, and blueprint.

If human observation and logs disagree, report the discrepancy and ask for clarification or propose a diagnostic.

---

## 19. Self-Modification Rule

AI agents must not modify this file (`.github/AI_CONTEXT.md`), blueprint files, or spec/contracts files unless the human explicitly requests a docs-only PR for that specific file or scope.

Rationale:

```text
This file is the repository AI operating protocol.
An AI that can silently rewrite its own operating protocol can lower its own standards.
```

If this file is changed, the PR must be docs-only unless the human explicitly authorizes a combined change.

Changes to blueprint/spec/contracts require explicit human authorization and must follow repository governance.

---

## 20. Position Stability Rule

When an AI reviewer changes position between rounds on the same issue or PR, the reviewer must state:

```text
- what new evidence caused the change
- which prior claim is now retracted
- whether the new position contradicts a previous approval or request-changes review
- what remains uncertain
```

Silent flip-flopping is not acceptable.

Changing position is allowed when new evidence justifies it, but the change must be explicit.

---

## 21. Out of Scope for This File

This file must not become a bug log, model registry, or secrets/config dump.

This file does not contain:

```text
- current bug status as permanent truth
- per-issue acceptance criteria
- blueprint behavior details that belong in blueprint/spec files
- secrets or credentials
- vendor-specific model settings
- CI workflow configuration
- temporary smoke results as standing truth
```

Temporary incident context belongs in:

```text
- GitHub Issue body/comments
- PR description
- smoke log comments
- release notes
```

Anything matching the out-of-scope list should be removed from this file during review.

---

## 22. Anti-Laziness Checklist Before Approval

Before approving a non-trivial or high-risk PR, the reviewer must be able to answer “yes” to all applicable items:

```text
[ ] I read the linked issue body and acceptance criteria.
[ ] I classified the PR risk level.
[ ] I built or updated a Debug Packet (Section 2.1) for high-risk PRs.
[ ] I reviewed all changed files.
[ ] I inspected relevant surrounding code, not only the diff.
[ ] I checked callers/callees of changed functions where relevant.
[ ] I checked related tests.
[ ] I checked blueprint/spec constraints.
[ ] I checked PII logging.
[ ] I checked delay/critical-section behavior if timing changed.
[ ] I checked error compatibility if exceptions/messages changed.
[ ] I checked whether the PR touches unrelated scope.
[ ] I checked CI/check status or noted if unavailable.
[ ] I considered latest smoke logs if available.
[ ] I stated any area I did not check and why.
```

If any applicable item is “no”, the review must say what was not checked and why.
