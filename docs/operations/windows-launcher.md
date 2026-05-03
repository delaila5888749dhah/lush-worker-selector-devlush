# Windows Launcher (`start_bot.ps1`)

A one-click PowerShell launcher for operators running lush-worker-selector on
Windows 10/11.

## Quick start

Double-click one of:

| Wrapper                          | Equivalent flags                |
| -------------------------------- | ------------------------------- |
| `start_bot_safe_1worker.bat`     | `-Safe -WorkerCount 1`          |
| `start_bot_prod_1worker.bat`     | `-Production -WorkerCount 1`    |

Or run directly from a PowerShell prompt at the repo root:

```powershell
.\start_bot.ps1 -Safe -WorkerCount 1
```

## What it does

1. `Set-Location -Path $PSScriptRoot` — always runs from the repo root.
2. `git pull origin main --ff-only` — unless `-SkipGitPull` is passed.
3. Verifies `.env` exists (does **not** create or modify it).
4. Creates `.venv` if missing.
5. Activates `.venv` and runs `pip install -r requirements.txt`.
6. Reads `BITBROWSER_ENDPOINT` from `.env` (default `http://127.0.0.1:54345`)
   and probes the TCP port with `Test-NetConnection`.
7. Applies session-only env overrides based on parameters
   (never written to `.env`).
8. Prints a redacted env summary — values for keys containing
   `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, or `PROXY` are masked.
9. Prints `git rev-parse HEAD` so log output can be correlated to a commit.
10. Creates `logs/` if missing and runs
    `python -m app 2>&1 | Tee-Object -FilePath logs\bot_<yyyyMMdd_HHmmss>.log`.

## Parameters

| Parameter         | Effect                                                                                          |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| `-WorkerCount N`  | Sets `WORKER_COUNT=N` and `MAX_WORKER_COUNT=N` for the launcher session only.                   |
| `-Safe`           | Sets `ENABLE_PRODUCTION_TASK_FN=0` (no-op stub task_fn) for the session.                        |
| `-Production`     | Sets `ENABLE_PRODUCTION_TASK_FN=1` after an interactive `YES` confirmation prompt.              |
| `-SkipGitPull`    | Skips `git pull origin main --ff-only`.                                                         |
| `-NoDomFallback`  | Leaves `ALLOW_DOM_ONLY_WATCHDOG` unset. Default behaviour sets it to `1` for the session only.  |

`-Safe` and `-Production` are mutually exclusive.

## What it will NOT do

- Modify `.env` or `.env.example` (session env overrides are process-scoped).
- Print API keys, tokens, or proxy credentials in plain text.
- Force-install dependencies if no virtual environment is active (warns instead).
- Run `git pull` when `-SkipGitPull` is passed.

## Examples

```powershell
# Safe / no-op stub mode, single worker
.\start_bot.ps1 -Safe -WorkerCount 1

# Production mode (will prompt for "YES")
.\start_bot.ps1 -Production -WorkerCount 1

# Skip git pull (offline / detached HEAD)
.\start_bot.ps1 -Safe -WorkerCount 1 -SkipGitPull

# Disable DOM-only watchdog fallback
.\start_bot.ps1 -Safe -WorkerCount 1 -NoDomFallback
```

## Logs

Each run writes to `logs/bot_<yyyyMMdd_HHmmss>.log`. The launcher tees stdout
and stderr, so the file mirrors what is shown in the console.
