# AI_CONTEXT.md — AI Debug, Fix, and Review Protocol

This file is the standing operating protocol for AI agents (Copilot Coding Agent, primary reviewer, secondary reviewer) and human maintainers. It is not runtime code. Issues, PRs, latest human instructions, smoke logs, tests, and blueprint/spec files are the source of truth for each task. This file does not hard-code any current bug, active issue, or temporary incident as permanent truth. Detailed templates, checklists, and the cross-review workflow live in `.github/AI_CONTEXT_DETAIL.md`.

## 1. TL;DR — Critical Rules (read first, always apply)

1. NEVER log raw PII, cookies, storage values, secrets, credentials, API keys, or real browser profile IDs. No justification overrides this.
2. NEVER modify this file, blueprint, or spec/contracts unless the human explicitly requests a docs-only PR for that scope.
3. ALWAYS classify task risk (trivial / non-trivial / high-risk) before choosing review depth.
4. ALWAYS build a Debug Packet for non-trivial fixes; do not proceed if it is unclear.
5. ALWAYS distinguish symptom / observation / hypothesis / confirmed fact / fix / verification. Do not treat hypotheses as facts.
6. NEVER approve a PR by reading only the diff. Inspect the review cone (issue, callers, tests, spec, logs).
7. Controlled exceptions (synthetic JS events, raw send_keys, raw click fallback, delay accumulator reset, global timeout bumps, cross-domain payment edits, broad refactors) require explicit PR justification under §5.
8. For high-risk debugging, two independent AI reviewers (currently GPT-5.5 + Claude Opus 4.7) review in parallel and cross-critique each other before the human decides. See §9.

## 2. Risk Classification

TRIVIAL:      docs-only, comments, typos, tests not changing production assertions.
NON-TRIVIAL:  any production code / config / env / CI / diagnostics / logging / exception change. Default if uncertain.
HIGH-RISK:    browser automation, CDP, selectors, checkout, payment, CVV, VBV, 3DS, session, cookie, fingerprint, anti-detect, DelayEngine, timing, orchestrator, retry, exception hierarchy, PII handling, billing/proxy pool, BitBrowser profiles, blueprint/spec/contracts.

## 3. Context Precedence

1) Latest human instruction → 2) Issue body/comments → 3) PR description/reviews → 4) Latest smoke logs/screenshots/diagnostics → 5) Current code → 6) Blueprint/spec/contracts → 7) This file.

Blueprint/spec/contracts are binding. If a request conflicts with them, report the conflict instead of silently violating design. This file must not override a newer issue, PR, instruction, or log.

## 4. Absolutely Forbidden (no exception, no override)

- logging raw PII (card, CVV, email, name, address, phone, raw page text, raw validation messages)
- logging raw cookie / localStorage / sessionStorage values
- committing secrets, credentials, API keys, real browser profile IDs
- AI self-editing this file, blueprint, or spec/contracts without explicit human authorization
- bypassing PII or self-modification rules via any "controlled exception"

## 5. Controlled Exceptions (require explicit PR justification)

- synthetic JS input/change/blur events to bypass validators
- raw Selenium send_keys on production hot paths
- raw Selenium click fallback in strict CDP paths
- resetting DelayEngine accumulator to bypass MAX_STEP_DELAY
- increasing global timeouts without scoped env/config
- changing payment/card submission inside an unrelated pre-card issue
- broad refactors unrelated to the linked issue


PR description must answer all four: (a) why standard path failed, (b) why this is minimum-risk option, (c) which test covers regression risk, (d) why it does not violate blueprint/spec.

## 6. Critical Thinking Rule

Symptom        = what failed.
Observation    = what logs / screenshots / diagnostics / tests show.
Hypothesis     = possible explanation, not yet proven.
Confirmed fact = proven by logs, tests, code, or diagnostics.
Inferred risk  = plausible risk needing test/diagnostic confirmation.
Fix            = minimal change for confirmed or strongly-supported cause.
Verification   = test, CI, smoke, or diagnostic proving the fix.

If new evidence disproves an old hypothesis, stop fixing the old hypothesis and update the plan. Silent flip-flopping between rounds is not allowed; state new evidence, retracted claim, and remaining uncertainty.

## 7. Debug Packet Requirement

For every non-trivial task, the AI's reasoning must reflect a Debug Packet covering: task, risk class, symptom, latest evidence, confirmed facts, active hypotheses, disproved hypotheses, relevant code/tests/spec, proposed fix scope, explicit out-of-scope. Full YAML template in `AI_CONTEXT_DETAIL.md §A`. Two reviewers must be able to compare packets and identify the exact disagreement.

## 8. No-Lazy-Review Rule (review cone)

For any non-trivial PR, inspect: linked issue + acceptance criteria, PR description, all changed files, surrounding code, callers/callees, related tests, blueprint/spec, runtime/orchestrator paths, CI status, latest smoke logs. For high-risk PRs, also repo-wide search for: selectors, env vars, exception classes, config names, helper functions, contracts, related tests. Explicitly state any area not checked and why. "Diff looks good" without runtime/test/spec verification is incomplete. Pre-approval checklist in `AI_CONTEXT_DETAIL.md §C`.

## 9. Parallel + Cross-Review Protocol (high-risk; human-orchestrated)

For high-risk debugging, two independent AI reviewers analyze the same evidence in parallel, then **cross-critique each other's output** before the human decides. Current setup: **Reviewer A = GPT-5.5**, **Reviewer B = Claude Opus 4.7** (model names are illustrative; replace as the human chooses). Workflow:

Round 1 — Parallel independent review:
  Human gives identical inputs (issue, logs, code refs, spec) to A and B.
  Each produces its own Debug Packet (§A) and recommendation, blind to the other.

Round 2 — Cross-review (human relays outputs):
  Human pastes A's output into B, and B's output into A.
  Each reviewer must: (i) state agreements, (ii) state disagreements with evidence,
  (iii) flag any hypothesis the other missed, (iv) flag any forbidden/controlled-exception
  pattern the other overlooked, (v) update or retract its own claims per §6 and §20-style
  position-stability rules.

Round 3 — Convergence or escalation:
  APPROVED:               both agree, no blocker after cross-review.
  REQUEST_CHANGES:        either still finds a valid blocker.
  NEEDS_HUMAN_DECISION:   material unresolved disagreement (architecture, runtime,
                          security, payment, anti-detect, PII, delay, spec).

This protocol is advisory and human-driven; it is not CI-enforced. No high-risk PR merges while a material disagreement is unresolved. Disagreement YAML format in `AI_CONTEXT_DETAIL.md §B`. Full cross-review workflow detail in `AI_CONTEXT_DETAIL.md §G`.

## 10. PII-Safe Diagnostics

Allowed: booleans, counts, lengths (`value_len=24`), symbolic selector names (`SEL_RECIPIENT_EMAIL`), CSS display/visibility/pointer-events, rect width/height, validity flags, error categories. Forbidden: raw values (`recipient@example.com`, raw cookies, raw page text). Applies to app logs, tests, diagnostics, screenshots, PR/issue comments, AI review output. Diagnostics must answer the next root-cause question, be stable across runs, and minimal but decisive.

## 11. Native Browser Interaction

Prefer CDP key/mouse events, native focus via click, natural blur via Tab. Forbidden by default: `dispatchEvent("input"|"change"|"blur")`, synthetic form submission shortcuts, JS mutation of user-entered field values. Diagnostics may read DOM but must not mutate user-visible form state unless the issue explicitly asks.

## 12. Delay & Human-Behavior Constraints

Use `_engine_aware_sleep(low, high, reason)` (or equivalent) that respects persona RNG, `engine.is_delay_permitted()`, `engine.accumulate_delay()`, MAX_STEP_DELAY headroom scaling, and logs only symbolic reason + numeric delay. No casual accumulator reset. No delay injection in critical / VBV / POST_ACTION states. No raw `time.sleep(...)` on hot paths unless the issue allows it and it is non-behavioral.

## 13. Error Semantics

Distinguish: absent / present-but-hidden / present-but-disabled / zero-size / pointer-events-none / blocked-by-overlay / click-failed. Do not report "selector not found" when diagnostics show present-but-disabled. New exception classes must preserve catch hierarchy and not break SessionFlaggedError, SelectorTimeoutError, alerting classification, or retry/failure accounting.

## 14. PR Scope, Tests, Review Output

Each PR: one issue, tight scope, includes/updates tests, no unrelated cleanup, preserves PII safety, respects blueprint. Tests cover new behavior, the failure that motivated the issue, PII safety on new logging, error semantics on changed exceptions, and DelayEngine/critical-section behavior on timing changes. Every AI review reports: scope coverage, runtime impact, blueprint alignment, PII safety, delay/error impact if relevant, tests added, CI status, merge readiness, blockers vs non-blocking. For bot-debugging PRs, also: root cause being addressed, log support for the fix, avoidance of disproven hypotheses, diagnostics sufficiency for next smoke.

## 15. Smoke Logs, Staleness, Out-of-Scope

When smoke logs are provided, cite decisive lines: last successful step, first failing step, exact error, relevant diagnostics, hypothesis confirmed/disproved, next minimal fix. If insufficient, state which diagnostic is missing — do not jump to a fix. Before reusing prior context, verify the issue is still open, no newer PR/log contradicts it, and the human has not given newer instructions. This file does not contain: current bug status as permanent truth, per-issue acceptance criteria, blueprint details, secrets, vendor model settings, CI configuration, or temporary smoke results. Such content belongs in Issues / PRs / smoke comments / release notes.
For smoke log analysis, follow .github/skills/analyze-smoke-logs.md.
For smoke log analysis, follow .github/skills/analyze-smoke-logs.md.
For task-specific skills and trigger phrases, see .github/skills/README.md.
