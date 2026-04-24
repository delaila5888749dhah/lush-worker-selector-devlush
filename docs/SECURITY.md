# Security Guide

This document covers environment configuration and secret hygiene for the
`lush-worker-selector-devlush` worker.  It is part of the issue #226
hardening effort.

## 1. `.env` setup

1. Copy the committed template to a local, **uncommitted** file:

   ```bash
   cp .env.example .env
   ```

2. Fill in the required secrets (API keys, Redis credentials, BitBrowser
   tokens, etc.).  The repository's `.gitignore` blocks `.env`, `.env.local`,
   `*.pem` and `*.key` from accidentally being committed — do **not** weaken
   this rule.

3. Load the file only in local development or when running the container:

   ```bash
   docker run --env-file .env lush-worker:latest
   ```

4. In CI, provide values through `${{ secrets.* }}` (GitHub Actions).  Never
   `echo` a secret to the job log; use `::add-mask::` if a value must be
   propagated between steps.

## 2. Key rotation lifecycle

| Kind of secret          | Rotation cadence | Owner              |
|-------------------------|------------------|--------------------|
| BitBrowser API key      | 90 days          | Platform team      |
| Redis password          | 90 days          | Infra team         |
| Givex merchant tokens   | 30 days          | Billing team       |
| Notification webhooks   | 180 days         | Observability team |

Rotation steps:

1. Provision the new secret in the identity provider / vault.
2. Update the GitHub Actions secret value (`Settings → Secrets`).
3. Roll the running deployment (pull the new image, `docker compose up -d`).
4. **Revoke** the previous secret at the provider once the new one is
   confirmed working — do not leave both active.
5. Record the rotation in the on-call journal with timestamp + operator.

## 3. Leak response

If a secret is suspected to be in a commit:

1. Revoke the secret immediately at the source.
2. Rotate it (section 2).
3. Purge it from git history (`git filter-repo` / BFG) on a protected branch
   and force-push under change management.
4. Open an incident ticket referencing this guide.

## 4. Dependency security

CI runs `pip-audit -r requirements.txt --strict` on every PR.  If it flags
a vulnerable dependency:

1. Bump the affected entry in `requirements.txt`.
2. Regenerate the lockfile:

   ```bash
   pip-compile --generate-hashes --no-strip-extras \
       --output-file=requirements-lock.txt requirements.txt
   ```

3. Re-run `pip-audit` locally before pushing.

## 5. Pre-commit hooks

`.pre-commit-config.yaml` runs `detect-secrets` and `ruff` before every
commit.  Install once per clone:

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```
