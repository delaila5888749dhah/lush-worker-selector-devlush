# Skill: Cross-Review Prompt

Use this skill when running the parallel + cross-review protocol for high-risk debugging (`AI_CONTEXT.md §9`, `AI_CONTEXT_DETAIL.md §G`). This file contains copy-paste prompts the human uses to relay output between two AI reviewers (currently GPT-5.5 + Claude Opus 4.7). The goal is to force genuine critique, not polite agreement, while preventing PII leaks during relay.

## When to use

```text
Use for HIGH-RISK tasks per AI_CONTEXT.md §2:
  - browser automation / CDP / selectors / checkout / payment
  - session / cookie / fingerprint / anti-detect
  - DelayEngine / timing / orchestrator / exception hierarchy
  - PII handling / blueprint changes

Do NOT use for:
  - trivial tasks (docs, typos)
  - non-trivial low-risk tasks where one careful review suffices
  - hotfixes where human accepts single-review risk (document in PR)
```

## Round 1 — Parallel independent review (paste to BOTH A and B separately)

The human prepares ONE input bundle and pastes it to each reviewer in separate sessions. Reviewers do not see each other's output in this round.

```text
=== ROUND 1 PROMPT (paste to GPT-5.5 AND Opus 4.7 separately) ===

You are an independent AI reviewer for a high-risk debugging task in this
repository. Follow .github/AI_CONTEXT.md §1-§15 strictly.

TASK_TYPE: <analyze-log | review-issue | review-pr | propose-fix>
RISK_CLASS: high-risk

INPUT BUNDLE:
  - Issue: <link or pasted body>
  - PR (if any): <link or pasted description + diff>
  - Smoke logs: <pasted, PII-redacted per §10>
  - Relevant code refs: <file paths or pasted snippets>
  - Blueprint/spec refs: <paths or contract IDs>

REQUIRED OUTPUT (do not skip any block):
  1. Debug Packet per AI_CONTEXT_DETAIL.md §A
  2. If logs included: 6-block analysis per skills/analyze-smoke-logs.md
  3. If diagnostic needed: proposal per skills/propose-diagnostic.md
  4. Recommendation: APPROVED | REQUEST_CHANGES | NEEDS_DIAGNOSTIC
  5. Blockers list (if any), with §4 / §5 / §13 references
  6. Inferred risks not yet proven
  7. Areas you did NOT check, with reason

CONSTRAINTS:
  - Do not see or assume what the other reviewer will say.
  - Cite line numbers / timestamps / file paths for every claim.
  - Mark hypothesis vs confirmed fact per §6.
  - PII-safe per §10 — redact before quoting.
  - If you suspect §4 absolute violations, flag immediately.

=== END ROUND 1 PROMPT ===
```

## Round 2 — Cross-review (paste each reviewer's output into the OTHER)

Human relays A's full output into B's session, and B's full output into A's session. Use this prompt verbatim:

```text
=== ROUND 2 CROSS-REVIEW PROMPT ===

Below is the OTHER reviewer's analysis of the same task you reviewed in
Round 1. Cross-review it. Do NOT simply agree. Do NOT simply reject.

OTHER REVIEWER (<A or B>):
<<<
[paste full Round-1 output of the other reviewer here]
>>>

REQUIRED OUTPUT (all 7 blocks, in order):

1. AGREEMENTS
   - List points where you and the other reviewer agree.
   - Cite shared evidence.

2. DISAGREEMENTS
   - List points where you disagree.
   - For each: state your claim, the other's claim, the evidence each cites,
     and the crux (what fact would resolve it).

3. MISSED_HYPOTHESES
   - List hypotheses the other reviewer did not consider.
   - State why each is plausible and what evidence would test it.

4. MISSED_VIOLATIONS
   - List any §4 (absolute forbidden) or §5 (controlled exception) patterns
     the other reviewer overlooked.
   - For §4: this is a hard blocker regardless of any other agreement.

5. SELF_RETRACTIONS
   - List any of YOUR OWN Round-1 claims you now retract or update.
   - For each: state the new evidence (from the other reviewer or re-reading)
     that caused the change, and what your prior claim was.
   - Silent flip-flopping is not acceptable. Be explicit.

6. UPDATED_RECOMMENDATION
   - APPROVED | REQUEST_CHANGES | NEEDS_DIAGNOSTIC | NEEDS_HUMAN_DECISION
   - Justify in one paragraph.

7. CONFIDENCE_DELTA
   - State whether your confidence increased, decreased, or stayed the same
     after seeing the other reviewer's output, and why.

CONSTRAINTS:
  - You may NOT change your position without naming the new evidence.
  - You may NOT agree with the other reviewer just because they sound confident.
  - Cite line numbers / file paths for every disagreement.
  - PII-safe per §10.

=== END ROUND 2 PROMPT ===
```

## Round 3 — Convergence decision (human reads both Round-2 outputs)

The human compares both updated outputs and applies §G.4:

```text
APPROVED:             both reviewers, after cross-review, agree no blocker.
REQUEST_CHANGES:      either still finds a valid blocker.
NEEDS_HUMAN_DECISION: material disagreement remains on architecture, runtime,
                      security, payment, anti-detect, PII, delay, spec, blueprint.
```

If `NEEDS_HUMAN_DECISION`, optionally trigger Round 4.

## Round 4 (optional) — Targeted diagnostic loop

If both reviewers in Round 2 agree evidence is insufficient, ask each (separately) to propose a diagnostic per `skills/propose-diagnostic.md`. Then:

```text
1. Human runs the diagnostic (or asks Copilot Coding Agent to instrument it).
2. New smoke produces new log.
3. Human re-runs Round 1 with the updated input bundle.
4. Repeat until convergence or hard-stop.
```

## Relay hygiene rules (human responsibility)

```text
1. Paste the FULL reviewer output, not a summary. Summaries lose nuance.
2. Do not editorialize ("A says X but I think Y"). Stay neutral.
3. Mark clearly which output came from A vs B.
4. Strip raw PII before relaying. If a reviewer accidentally included PII:
   - Redact it (replace with <REDACTED:type>)
   - Flag the PII leak as a §4 violation against that reviewer's output
   - Note: the leak itself is a Round-2 disagreement input
5. Do not skip rounds. Do not merge Round 1 + Round 2 into one prompt.
6. If a reviewer refuses to cross-review or only agrees, push back:
   "Per skill cross-review-prompt.md Round 2, you must produce all 7 blocks
    including DISAGREEMENTS or SELF_RETRACTIONS. Re-do."
```

## Anti-patterns to reject in reviewer output

```text
❌ "I agree with the other reviewer." — block 2 (DISAGREEMENTS) cannot be empty
   without explicit "no disagreements found" + evidence of having looked.
❌ Round 2 output identical to Round 1 — means cross-review did not happen.
❌ Position change without SELF_RETRACTIONS block — silent flip-flop, §6/§20.
❌ "The other reviewer is correct" without citing what evidence convinced you.
❌ MISSED_VIOLATIONS empty when §4/§5 patterns exist in the other's output.
❌ Polite hedging ("both perspectives have merit") without naming the crux.
❌ Confidence delta = "same" when material new evidence was presented.
```

## Convergence quality checklist (human applies before merging)

```text
[ ] Round 2 outputs from BOTH reviewers exist
[ ] Each Round 2 output has all 7 blocks filled
[ ] DISAGREEMENTS block names a clear crux per item
[ ] Any §4 violation flagged in MISSED_VIOLATIONS is a hard blocker
[ ] If positions changed, SELF_RETRACTIONS explains why
[ ] No raw PII in either output (relay hygiene rule 4)
[ ] If NEEDS_HUMAN_DECISION, the human's decision is recorded in PR/issue
[ ] If approved, both updated recommendations are APPROVED
```

## Minimal example (synthetic, abbreviated)

Round 1 — A says: "ROOT_CAUSE: STRONGLY_SUPPORTED — selector disabled by validator H2."
Round 1 — B says: "ROOT_CAUSE: UNDETERMINED — could be H2 or H3 (overlay)."

Round 2 (B reviewing A) excerpt:

```text
1. AGREEMENTS:
   - SEL_CHECKOUT_BTN is present-but-disabled (line 195).

2. DISAGREEMENTS:
   - Claim: A says H2 is the cause.
     B's view: evidence at line 195 only proves "disabled", not "by validator".
     Crux: which validator (or overlay) disabled it.

3. MISSED_HYPOTHESES:
   - A did not consider H4: button disabled because cart total = 0
     (cart_count=3 but readiness=true could still mean total stuck).

5. SELF_RETRACTIONS:
   - My Round-1 H3 (overlay) is now WEAKENED — A correctly noted no
     elementFromPoint diagnostic exists. Reclassifying H3 from ACTIVE
     to INFERRED_RISK pending diagnostic.

6. UPDATED_RECOMMENDATION: NEEDS_DIAGNOSTIC
   - Trigger skills/propose-diagnostic.md for H2 vs H4.

7. CONFIDENCE_DELTA: decreased — A's analysis revealed I was overweighting H3.
```

This is a healthy cross-review: agreements named, disagreements with crux, missed hypothesis surfaced, self-retraction explicit, recommendation updated, confidence delta honest.
