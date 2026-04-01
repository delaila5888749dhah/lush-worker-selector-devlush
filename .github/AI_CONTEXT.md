# PROJECT CONTEXT & RULES (MANDATORY)

## 1. Infrastructure
- **Organization:** 1minhtaocompany
- **Repo:** lush-givex-worker
- **Visibility:** Public (for Advanced Security/CodeQL)

## 2. CI/CD & Governance
- **Environment:** `production`
- **Gatekeeping:** Manual Approval by Human Admin is REQUIRED for all merges to `main`.
- **Branch Protection:** No direct pushes to `main`. Pull Requests only.
- **Security:** CodeQL, Secret Scanning, and Push Protection are active.

## 3. Workflow Rules
- **Indentation:** YAML files must use exactly 2 spaces.
- **Zero-Disruption:** Do not modify business logic unless explicitly requested.
- **Phase Status:** Currently in Phase 1.5 (PR #71).

4. Agent Roles & Hierarchy (Core Logic)
[Architect] Claude Opus 4.6
Responsibilities: High-level system design, generating original Specifications (Specs), and defining Core Interfaces.

Authority: Final decision-maker on directory structure, architectural patterns, and complex logic flows.

Focus: Prevention of race conditions, deadlocks, and structural technical debt.

[Developer] GPT-5.2-Codex
Responsibilities: Full-file implementation, detailed logic coding, and Unit Test generation based on Architect/Human prompts.

Constraints: Must strictly adhere to the Specs provided by the Architect. Prohibited from altering the fundamental architecture without prior approval.

Focus: Syntactic accuracy, performance optimization, and 100% test coverage.

[Reviewer] GPT-5.4
Responsibilities: Code audit, Spec-to-Code cross-verification, and Security compliance monitoring (CodeQL/Secret Scanning).

Authority: Issues strictly [APPROVED] or [REJECTED] status.

Constraint: Zero-editing policy. Must provide feedback for the Developer to fix, rather than modifying code directly.

[Human Admin] delaila5888749dhah
Authority: The only entity permitted to trigger the Manual Approval gate in the production environment.

Role: Final arbiter for Merging Pull Requests (PRs) into the main branch.
