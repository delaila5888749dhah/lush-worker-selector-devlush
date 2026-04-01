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

## 4. Agent Roles
- **Coordinator:** Oversees PRs and notifies Human for approvals.
- **Architect:** Analyzes CodeQL reports and ensures Spec compliance.
