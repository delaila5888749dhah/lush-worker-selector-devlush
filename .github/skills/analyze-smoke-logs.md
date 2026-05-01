# Skill: Analyze Smoke Logs

Use this skill when the human pastes smoke logs, runtime logs, or worker output and asks "why did this fail?" or "what's the root cause?". This skill extends `AI_CONTEXT.md §6` (critical thinking), `§10` (PII), and `§15` (smoke log reasoning). Do not use this skill to propose code fixes — produce evidence first; fixes belong in a separate step.

## When to use

```text
Trigger phrases from the human:
  - "đây là log smoke", "phân tích log", "tại sao fail"
  - "smoke log", "worker log", "runtime log"
  - paste of multi-line log content with timestamps or stack traces
```

If the input is not a log (e.g., it's a code snippet or a screenshot description), do not use this skill — fall back to the relevant section of `AI_CONTEXT.md`.

## Required output format

Always produce these 6 blocks in order. Do not skip any.

### Block 1 — Last successful step

```text
LAST_OK_STEP: <step name or symbolic action>
LAST_OK_LINE: "<exact log line, redact PII per §10>"
EVIDENCE_AT: <line number or timestamp>
```

### Block 2 — First failing step

```text
FIRST_FAIL_STEP: <step name>
FIRST_FAIL_LINE: "<exact log line, redact PII>"
EVIDENCE_AT: <line number or timestamp>
EXCEPTION_CLASS: <class name if any, else "none">
```

### Block 3 — Decisive diagnostics between OK and FAIL

List every PII-safe diagnostic that helps decide root cause. Cite each with line/timestamp.

```text
- "<diagnostic line>" @<line/ts>  → interpretation
- "<diagnostic line>" @<line/ts>  → interpretation
```

If no decisive diagnostic exists between OK and FAIL, say so explicitly:

```text
DECISIVE_DIAGNOSTICS: insufficient — see Block 6.
```

### Block 4 — Hypothesis status

For each plausible hypothesis, mark its status using §6 vocabulary:

```text
H1: <claim>
   status: <CONFIRMED | DISPROVED | ACTIVE | INFERRED_RISK>
   evidence: [<line/ts refs>]
   contradicts: [<line/ts refs>] (if any)
```

Distinguish UI readiness categories explicitly when relevant (per `AI_CONTEXT.md §13`):

```text
absent | present-but-hidden | present-but-disabled | zero-size |
pointer-events-none | blocked-by-overlay | click-failed
```

Never collapse these into "selector not found".

### Block 5 — Root cause statement

Exactly one of:

```text
ROOT_CAUSE: CONFIRMED — <one-line statement>
            evidence: [<line/ts refs>]

ROOT_CAUSE: STRONGLY_SUPPORTED — <one-line statement>
            evidence: [<line/ts refs>]
            remaining_uncertainty: <what would fully confirm>

ROOT_CAUSE: UNDETERMINED — see Block 6 for missing diagnostics.
```

Do not write "ROOT_CAUSE: CONFIRMED" unless an evidence line directly proves it. Hypothesis ≠ confirmed fact.

### Block 6 — What is missing (if anything)

If logs are insufficient, list the smallest set of additional diagnostics needed. Each must be PII-safe (§10) and answer the next decisive question.

```text
MISSING_DIAGNOSTIC_1:
  question: <what we need to know>
  proposal:  <symbolic name + value type, e.g., "log SEL_X.disabled bool">
  pii_safe:  yes
```

If logs are sufficient, write:

```text
MISSING_DIAGNOSTIC: none — evidence is sufficient.
```

## Hard rules

```text
1. Cite line numbers or timestamps for every claim. No claim without citation.
2. Redact PII per AI_CONTEXT.md §10 before quoting any log line.
   Replace raw values with lengths/booleans/symbolic names.
3. Do NOT propose code fixes in this skill's output. Root cause + evidence only.
4. Do NOT mark a hypothesis CONFIRMED based on plausibility. Require a log line.
5. If two AI reviewers (GPT-5.5 / Opus 4.7) analyze the same log, each must
   produce this 6-block output independently before cross-review (see §G).
6. If the log shows raw PII, flag it as a §4 violation BEFORE any analysis,
   and request a redacted re-paste. Do not analyze raw-PII logs.
```

## Anti-patterns to avoid

```text
❌ "Có vẻ như session bị flag" without citing a log line
❌ "Selector not found" when log shows present-but-disabled
❌ Jumping to fix proposal in Block 5 instead of root cause statement
❌ "Likely a timing issue" with no diagnostic to back it
❌ Quoting raw email/cookie/page-text from log
❌ Skipping Block 6 when evidence is actually insufficient
```

## Minimal example (synthetic, PII-safe)

```text
Block 1 — LAST_OK_STEP: cart_ready
          LAST_OK_LINE: "cart_count=3 readiness=true" @line 142

Block 2 — FIRST_FAIL_STEP: click_checkout
          FIRST_FAIL_LINE: "SelectorTimeoutError: SEL_CHECKOUT_BTN" @line 198
          EXCEPTION_CLASS: SelectorTimeoutError

Block 3 — "SEL_CHECKOUT_BTN present=true disabled=true display=block" @line 195
          → element exists but disabled, not absent.

Block 4 — H1: button absent → DISPROVED, contradicts: [line 195]
          H2: button disabled by client validator → ACTIVE, evidence: [line 195]
          H3: overlay blocking click → INFERRED_RISK, no diagnostic yet

Block 5 — ROOT_CAUSE: STRONGLY_SUPPORTED — checkout button present but disabled,
          not absent. Error message "SelectorTimeoutError" is misleading per §13.
          remaining_uncertainty: which validator disabled it.

Block 6 — MISSING_DIAGNOSTIC_1:
            question: which form field's validity flag failed
            proposal: log validity.{valueMissing,typeMismatch,patternMismatch}
                      for each form input as booleans
            pii_safe: yes
```
