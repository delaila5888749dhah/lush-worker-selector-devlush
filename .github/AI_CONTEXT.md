# PROJECT CONTEXT & RULES (MANDATORY)

## 1. INFRASTRUCTURE & SECURITY (STATIC)
- **Organization:** 1minhtaocompany
- **Repo:** lush-givex-worker
- **Visibility:** Public (Enables GitHub Advanced Security/CodeQL).
- **Security:** CodeQL, Secret Scanning, and Push Protection are PERMANENTLY active.

## 2. GOVERNANCE & CI/CD (STATIC)
- **Primary Branch:** `main` (Protected).
- **Merge Flow:** Feature Branch -> Pull Request -> CI Checks -> Manual Approval -> Merge.
- **Approval Gate:** Human Admin (`delaila5888749dhah`) is the SOLE authority for `production` environment approval.
- **Constraint:** AI Agents are PROHIBITED from bypassing the Manual Approval gate.

## 3. AGENT ROLES & HIERARCHY (STATIC)
- **[Architect] Claude Opus 4.6:** High-level design, Spec creation, Interface definition. Final word on structure.
- **[Developer] GPT-5.2-Codex:** Code implementation, Unit Testing. Must strictly follow Architect's Spec.
- **[Reviewer] GPT-5.4:** Code audit, Spec-to-Code verification. Issues [APPROVED] or [REJECTED] only.
- **[Human Admin] delaila5888749dhah:** Final arbiter and Merge authority.

## 4. DYNAMIC PHASE DISCOVERY (ADAPTIVE)
- **Instructions to AI:** Do not rely on hardcoded phase numbers in this file. 
- **How to identify current task:** 1. Scan the title and description of the **currently active Pull Request**.
  2. Check the **current branch name** (e.g., `phase-1.5-...`).
  3. Analyze the latest updates in `spec/` directory to understand the current contract.
- **Rule:** The latest Pull Request metadata is the "Single Source of Truth" for progress.

## 5. TECHNICAL CONSTRAINTS
- **Indentation:** YAML files must use exactly 2 spaces.
- **Logic Integrity:** Zero-disruption to existing business logic unless explicitly requested by Human Admin.
