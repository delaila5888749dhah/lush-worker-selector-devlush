# AI Skills Index

Task-specific skill files for AI agents working in this repository. These skills extend `.github/AI_CONTEXT.md` (the standing protocol) and `.github/AI_CONTEXT_DETAIL.md` (templates and checklists). Skills are loaded on demand — agents do not need to read them all upfront.

## Available skills

| Skill | Purpose | Trigger | Output |
|---|---|---|---|
| [`analyze-smoke-logs.md`](./analyze-smoke-logs.md) | Parse smoke/runtime logs into evidence | Human pastes logs and asks for root cause | 6 blocks: last-OK, first-fail, diagnostics, hypothesis status, root cause, missing diagnostics |
| [`propose-diagnostic.md`](./propose-diagnostic.md) | Propose PII-safe diagnostics when logs are insufficient | `analyze-smoke-logs` Block 6 returns missing diagnostics, or human asks "what should we log?" | 1–3 diagnostic blocks with scope guards and rejected alternatives |
| [`cross-review-prompt.md`](./cross-review-prompt.md) | Run parallel + cross-review between two AI reviewers | High-risk task per `AI_CONTEXT.md §2` | Round 1/2/3/4 prompts, copy-paste ready |

## When to use which skill

```text
Trivial task (docs, typos):
  → No skill needed. Apply AI_CONTEXT.md §1-§15.

Non-trivial low-risk task:
  → Single reviewer. Use analyze-smoke-logs.md if logs are involved.
  → Use propose-diagnostic.md if logs are insufficient.

High-risk task (browser automation, payment, anti-detect, PII, blueprint):
  → Use cross-review-prompt.md (orchestrates the other two).
  → Round 1: each reviewer applies analyze-smoke-logs.md independently.
  → If gap: each reviewer applies propose-diagnostic.md independently.
  → Round 2: cross-critique. Round 3: converge or escalate.
```

## Skill chaining (typical high-risk flow)

```text
Log paste
  → analyze-smoke-logs.md (each reviewer, Round 1)
  → propose-diagnostic.md (if Block 6 has missing diagnostics)
  → cross-review-prompt.md Round 2 (cross-critique)
  → Round 3 converge → human merges OR Round 4 diagnostic loop
```

## Relationship to other docs

```text
AI_CONTEXT.md           → 15 standing rules. Authoritative. Always applies.
AI_CONTEXT_DETAIL.md    → Templates (§A Debug Packet, §B Disagreement,
                          §C Pre-Approval Checklist, §G Cross-Review Workflow).
                          Reference when AI_CONTEXT.md points to it.
skills/*.md             → Task-specific procedures. Load only when triggered.
                          Each skill explicitly cites which AI_CONTEXT.md
                          sections it extends.
```

Skills must NOT contradict `AI_CONTEXT.md`. If a conflict arises, `AI_CONTEXT.md` wins. Skills are subordinate procedures, not overrides.

## Hard rules for all skills

```text
1. Skills follow §4 (absolutely forbidden) and §10 (PII safety) without exception.
2. Skills do not modify AI_CONTEXT.md, blueprint, or spec/contracts. See §19.
3. Skills must cite the AI_CONTEXT.md sections they extend, at the top of the file.
4. New skills require human-authorized docs-only PR. AI agents do not self-create skills.
```

## Adding a new skill

```text
Before adding a new skill, verify:
  [ ] The task occurs repeatedly (≥5 times) with consistent input/output shape
  [ ] The current AI output is unreliable for this task
  [ ] The skill can be written in ≤100 lines
  [ ] The skill does not duplicate AI_CONTEXT.md or another skill
  [ ] The skill cites which AI_CONTEXT.md sections it extends
  [ ] The human explicitly authorizes the new skill (per §19)

If any item is "no", do not add the skill.
```

## One-line trigger phrases (for human convenience)

When the human types one of these short phrases, the AI must auto-detect intent, fetch required context from GitHub, and apply the matching skill end-to-end without asking for re-paste.

| Trigger phrase | Auto-action |
|---|---|
| `review pr <url>` | Fetch PR + diff + linked issue + CI status + smoke logs (if linked). Classify risk. If high-risk → run `cross-review-prompt.md` Round 1 self-mode (this AI = Reviewer A). Output Round-1 format. |
| `cross-review pr <url>` | Same as above, force high-risk path even if AI thinks it's low-risk. |
| `analyze log` (with logs pasted or linked) | Apply `analyze-smoke-logs.md` 6-block format. |
| `propose diagnostic` (after analyze) | Apply `propose-diagnostic.md`. |
| `round 2: <paste other reviewer output>` | Apply `cross-review-prompt.md` Round 2, 7-block cross-critique. |
| `round 4 diagnostic` | Apply `propose-diagnostic.md` to resolve current Round-2 disagreement. |

### Auto-fetch contract

When the human gives only a PR URL, the AI must fetch (via GitHub tool / MCP):

```text
1. PR title, description, author, base/head branches
2. Full diff (all changed files)
3. Linked issue body + last 20 comments
4. CI/check status (which jobs passed/failed)
5. Last failing job log (if any)
6. Files referenced in diff that are NOT changed but needed for review cone
   (callers/callees, related tests, blueprint/spec)
```

If the AI does NOT have GitHub tool access, it must respond with exactly:

```text
NEED_PASTE: GitHub tool unavailable. Paste PR description, full diff,
linked issue body, and CI status before I can run the skill.
```

Do not silently fall back to a partial review. Either auto-fetch or request paste.

### Output discipline

Auto-fetch mode does not relax skill output format. The AI must still produce the full skill output (6 blocks for log analysis, 7 blocks for cross-review, etc.). One-line trigger only saves typing for the human; it does not save effort for the AI.
