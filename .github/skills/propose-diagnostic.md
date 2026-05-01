# Skill: Propose Diagnostic

Use this skill when smoke logs are insufficient and a new diagnostic is needed to confirm or disprove a hypothesis. This skill extends `AI_CONTEXT.md §10` (PII-safe diagnostics) and `§11` (diagnostics design rule). Output is a diagnostic proposal — not a code fix. The proposal must be small, scoped, PII-safe, and answer exactly one decisive question per proposal.

## When to use

```text
Trigger conditions:
  - analyze-smoke-logs.md Block 6 returned MISSING_DIAGNOSTIC_*
  - Human asks "thêm log gì?", "cần diagnostic nào?", "log này thiếu gì?"
  - Cross-review (§G) reveals reviewers disagree on a hypothesis that
    a single new log line could resolve
  - Issue/PR explicitly asks for instrumentation before a fix
```

If the human is asking for a code fix (not a diagnostic), do not use this skill.

## Required output format

Produce one block per proposed diagnostic. Maximum 3 proposals per response — if more are needed, prioritize the 3 most decisive and note the rest as deferred.

### Block per diagnostic

```text
DIAG_ID: D1
QUESTION: <the single decisive question this diagnostic answers>
HYPOTHESIS_LINK: <H1/H2/... from analyze-smoke-logs Block 4, or "new">

INSTRUMENTATION_TYPE: <log | dom_read | counter | timing | state_snapshot>
LOCATION: <file path + function/class + symbolic step name>
TRIGGER: <when this fires — symbolic state, not raw timing>

OUTPUT_SHAPE:
  - field: <name>
    type:  <bool | int | len | enum | symbolic_name>
    pii_safe: yes
  - field: <name>
    type:  <bool | int | len | enum | symbolic_name>
    pii_safe: yes

EXPECTED_VALUES:
  - if hypothesis CONFIRMED: <what the diagnostic should show>
  - if hypothesis DISPROVED: <what the diagnostic should show>

SCOPE_GUARDS:
  - production_safe: <yes | no — and why>
  - delay_impact:    <none | bounded by §12 | requires DelayEngine guard>
  - retry_safe:      <yes | no — does it survive retries cleanly>
  - log_volume:      <one-shot | per-step | per-iteration — justify if not one-shot>

REJECTED_ALTERNATIVES:
  - <alt 1>: <why rejected — usually PII, scope, or non-decisive>
  - <alt 2>: <why rejected>
```

If multiple diagnostics share a code path, group them as `D1`, `D2`, `D3` in one location to minimize churn.

## Hard rules (must satisfy ALL)

```text
1. PII-safe per §10. No raw values. Use lengths/booleans/symbolic names.
   Whitelist of allowed shapes:
     - bool         (present, disabled, valid)
     - int          (count, retry_n)
     - len          (value_len, text_len, class_len)
     - enum         (state name from a fixed set)
     - symbolic     (selector key like SEL_X, never raw selector string with PII)
     - css_value    (display, visibility, pointer-events — these are not PII)
     - rect         (rect_w, rect_h)
     - validity     (validity.valueMissing etc, booleans only)

2. One question per diagnostic. If you need 2 questions, propose 2 diagnostics.

3. Decisive: the diagnostic must change the hypothesis status from ACTIVE
   to CONFIRMED or DISPROVED. If it cannot, do not propose it.

4. Minimal scope: prefer one-shot over per-step, per-step over per-iteration.
   High-volume logging requires explicit justification under SCOPE_GUARDS.

5. No production behavior change. Diagnostics may READ DOM/state; they must NOT
   mutate user-visible form state, dispatch synthetic events, or alter timing
   beyond §12 budget.

6. No raw send_keys, no synthetic JS events, no click fallback added "for
   diagnostics". Those are §5 controlled exceptions, not diagnostics.

7. If the diagnostic touches DelayEngine, critical/VBV/POST_ACTION states, or
   the orchestrator, it must specify how it stays within §12 and §13 constraints.

8. Stable across runs. Do not propose diagnostics whose output depends on
   wallclock time, RNG seed, or external network latency unless that variance
   IS the answer to the question.
```

## Anti-patterns to reject

```text
❌ "Log toàn bộ page HTML" — PII leak, not decisive, huge volume
❌ "Log raw cookie để debug session" — §4 absolute violation
❌ "Log validationMessage string" — may contain user data, use len + bool flags
❌ "Thêm sleep 5s rồi check lại" — not a diagnostic, breaks §12
❌ "Thử dispatchEvent('input') xem có pass không" — §11 forbidden, not a diagnostic
❌ "Log mọi thứ trong scope để xem" — non-decisive, violates rule 2 + 4
❌ Diagnostic không gắn với hypothesis nào — purposeless
❌ "Log raw email để xác nhận đúng user" — §4 violation, never acceptable
```

## Output checklist (self-verify before responding)

```text
[ ] Each diagnostic answers exactly one question
[ ] Every output field has pii_safe: yes
[ ] No raw values, no full strings of user content
[ ] EXPECTED_VALUES distinguishes CONFIRMED vs DISPROVED clearly
[ ] SCOPE_GUARDS addresses production_safe, delay_impact, retry_safe, log_volume
[ ] REJECTED_ALTERNATIVES shows you considered cheaper/safer options first
[ ] Diagnostic is decisive (not "more info would help")
[ ] No code fix is being proposed in disguise
[ ] If touching delay/critical-section, §12 compliance is explicit
[ ] Total proposals ≤ 3
```

## Cross-review note (when used with §G)

When two reviewers (GPT-5.5 / Opus 4.7) propose diagnostics independently:

```text
Round 1: Each produces its own diagnostic blocks per this skill.
Round 2: Cross-review focuses on:
  - Does the other reviewer's diagnostic actually answer its stated question?
  - Is there overlap that can be merged into fewer diagnostics?
  - Did one reviewer miss a SCOPE_GUARD violation the other caught?
  - Is one reviewer's diagnostic non-decisive (rule 3 violation)?
Round 3: Converge on the smallest decisive set, prioritized by:
  1. Resolves a Round-2 disagreement (highest priority)
  2. Confirms/disproves the most active hypothesis
  3. Lowest production risk and log volume
```

## Minimal example (synthetic, PII-safe)

Context: `analyze-smoke-logs` Block 6 said:
> question: which form field's validity flag failed

Proposal:

```text
DIAG_ID: D1
QUESTION: which form input's HTML5 validity check is rejecting the form?
HYPOTHESIS_LINK: H2 (button disabled by client validator)

INSTRUMENTATION_TYPE: dom_read
LOCATION: workers/checkout/form_submit.py :: _pre_submit_check()
TRIGGER: just before SEL_CHECKOUT_BTN click attempt

OUTPUT_SHAPE:
  - field: input_key
    type:  symbolic        # e.g. SEL_RECIPIENT_EMAIL
    pii_safe: yes
  - field: validity_valueMissing
    type:  bool
    pii_safe: yes
  - field: validity_typeMismatch
    type:  bool
    pii_safe: yes
  - field: validity_patternMismatch
    type:  bool
    pii_safe: yes
  - field: value_len
    type:  int
    pii_safe: yes

EXPECTED_VALUES:
  - if H2 CONFIRMED: at least one input shows a true validity flag
  - if H2 DISPROVED: all inputs show all-false validity, value_len > 0

SCOPE_GUARDS:
  - production_safe: yes — read-only DOM query, no event dispatch
  - delay_impact:    none
  - retry_safe:      yes — one-shot per click attempt
  - log_volume:      one-shot per checkout attempt

REJECTED_ALTERNATIVES:
  - "log validationMessage string": may contain user data; use bool flags + len instead
  - "screenshot the form": not decisive (cannot read validity API from image)
  - "log full form HTML": PII leak risk + huge volume + non-decisive
```
