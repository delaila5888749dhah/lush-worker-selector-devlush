<#
.SYNOPSIS
    Windows launcher for lush-worker-selector.

.DESCRIPTION
    One-click launcher that:
      - Sets working directory to the repo root
      - Optionally pulls latest main (unless -SkipGitPull)
      - Verifies .env exists
      - Creates and activates .venv
      - Installs requirements.txt
      - Probes the BitBrowser endpoint
      - Sets per-session env overrides for WorkerCount / Safe / Production / DOM fallback
      - Prints a redacted env summary + current git commit
      - Runs `python -m app` and tees output to logs/bot_<timestamp>.log

    This launcher MUST NOT modify .env or .env.example.

.PARAMETER WorkerCount
    Overrides WORKER_COUNT and MAX_WORKER_COUNT for this launcher session only.

.PARAMETER Safe
    Sets ENABLE_PRODUCTION_TASK_FN=0 (no-op stub) for the session.

.PARAMETER Production
    Sets ENABLE_PRODUCTION_TASK_FN=1 for the session and prompts for confirmation.

.PARAMETER SkipGitPull
    Skip `git pull origin main --ff-only`.

.PARAMETER NoDomFallback
    Do NOT set ALLOW_DOM_ONLY_WATCHDOG=1 for the session (default: set it).

.EXAMPLE
    .\start_bot.ps1 -Safe -WorkerCount 1

.EXAMPLE
    .\start_bot.ps1 -Production -WorkerCount 1
#>
[CmdletBinding()]
param(
    [int]$WorkerCount,
    [switch]$Safe,
    [switch]$Production,
    [switch]$SkipGitPull,
    [switch]$NoDomFallback
)

$ErrorActionPreference = "Stop"

# 1. Always run from repo root
Set-Location -Path $PSScriptRoot

Write-Host "==> lush-worker-selector launcher" -ForegroundColor Cyan
Write-Host "    Repo root: $PSScriptRoot"

# 2. Optional git pull
if (-not $SkipGitPull) {
    Write-Host "==> git pull origin main --ff-only" -ForegroundColor Cyan
    try {
        git pull origin main --ff-only
    } catch {
        Write-Host "    git pull failed: $_" -ForegroundColor Yellow
        Write-Host "    Continuing with local checkout." -ForegroundColor Yellow
    }
} else {
    Write-Host "==> Skipping git pull (-SkipGitPull)" -ForegroundColor Yellow
}

# 3. Verify .env exists
if (-not (Test-Path ".env")) {
    Write-Host "ERROR: .env not found in $PSScriptRoot" -ForegroundColor Red
    Write-Host "       Copy .env.example to .env and fill in operator values." -ForegroundColor Red
    exit 1
}

# 4. Create venv if missing
if (-not (Test-Path ".venv")) {
    Write-Host "==> Creating virtual environment .venv" -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to create .venv" -ForegroundColor Red
        exit 1
    }
}

# 5. Activate venv
$activate = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Host "ERROR: venv activation script not found at $activate" -ForegroundColor Red
    exit 1
}
Write-Host "==> Activating .venv" -ForegroundColor Cyan
. $activate

# Sanity: do not force-install if not actually in a venv
if (-not $env:VIRTUAL_ENV) {
    Write-Host "WARNING: VIRTUAL_ENV not set after activation; skipping pip install." -ForegroundColor Yellow
} else {
    Write-Host "==> Installing requirements.txt" -ForegroundColor Cyan
    python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: pip install failed" -ForegroundColor Red
        exit 1
    }
}

# 6. Read BITBROWSER_ENDPOINT from .env (default http://127.0.0.1:54345)
function Get-EnvValue {
    param([string]$Name, [string]$Default = "")
    $line = Select-String -Path ".env" -Pattern "^\s*$([regex]::Escape($Name))\s*=" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $line) { return $Default }
    $value = $line.Line -replace "^\s*$([regex]::Escape($Name))\s*=\s*", ""
    # Strip surrounding quotes
    $value = $value.Trim()
    if ($value.StartsWith('"') -and $value.EndsWith('"')) { $value = $value.Substring(1, $value.Length - 2) }
    elseif ($value.StartsWith("'") -and $value.EndsWith("'")) { $value = $value.Substring(1, $value.Length - 2) }
    return $value
}

$bbEndpoint = Get-EnvValue -Name "BITBROWSER_ENDPOINT" -Default "http://127.0.0.1:54345"
Write-Host "==> BitBrowser endpoint: $bbEndpoint" -ForegroundColor Cyan

# Parse host/port from endpoint
$bbHost = "127.0.0.1"
$bbPort = 54345
try {
    $uri = [System.Uri]$bbEndpoint
    if ($uri.Host) { $bbHost = $uri.Host }
    if ($uri.Port -gt 0) { $bbPort = $uri.Port }
} catch {
    Write-Host "    Could not parse endpoint URI; using defaults $bbHost`:$bbPort" -ForegroundColor Yellow
}

Write-Host "==> Probing BitBrowser TCP port $bbHost`:$bbPort" -ForegroundColor Cyan
$reachable = $false
try {
    $reachable = Test-NetConnection -ComputerName $bbHost -Port $bbPort -InformationLevel Quiet -WarningAction SilentlyContinue
} catch {
    $reachable = $false
}
if ($reachable) {
    Write-Host "    BitBrowser endpoint is reachable." -ForegroundColor Green
} else {
    Write-Host "    WARNING: BitBrowser endpoint $bbHost`:$bbPort is not reachable." -ForegroundColor Yellow
    Write-Host "             Ensure BitBrowser is running before workers attempt to connect." -ForegroundColor Yellow
}

# 7. Apply per-session env overrides (process scope only — never written to .env)
if ($PSBoundParameters.ContainsKey('WorkerCount')) {
    $env:WORKER_COUNT = "$WorkerCount"
    $env:MAX_WORKER_COUNT = "$WorkerCount"
    Write-Host "==> Session WORKER_COUNT=$WorkerCount  MAX_WORKER_COUNT=$WorkerCount" -ForegroundColor Cyan
}

if ($Safe -and $Production) {
    Write-Host "ERROR: -Safe and -Production are mutually exclusive." -ForegroundColor Red
    exit 1
}

if ($Safe) {
    $env:ENABLE_PRODUCTION_TASK_FN = "0"
    Write-Host "==> Session ENABLE_PRODUCTION_TASK_FN=0 (Safe / no-op stub mode)" -ForegroundColor Green
}

if ($Production) {
    $confirm = Read-Host "Production mode will execute REAL purchases. Type 'YES' to continue"
    if ($confirm -ne "YES") {
        Write-Host "Aborted by operator." -ForegroundColor Yellow
        exit 1
    }
    $env:ENABLE_PRODUCTION_TASK_FN = "1"
    Write-Host "==> Session ENABLE_PRODUCTION_TASK_FN=1 (Production mode)" -ForegroundColor Red
}

if (-not $NoDomFallback) {
    $env:ALLOW_DOM_ONLY_WATCHDOG = "1"
    Write-Host "==> Session ALLOW_DOM_ONLY_WATCHDOG=1 (DOM fallback enabled)" -ForegroundColor Cyan
} else {
    # Explicitly override any parent/.env value for this launcher session.
    $env:ALLOW_DOM_ONLY_WATCHDOG = "0"
    Write-Host "==> -NoDomFallback set: Session ALLOW_DOM_ONLY_WATCHDOG=0" -ForegroundColor Yellow
}

# 8. Print redacted env summary
function Test-ShouldMask {
    param([string]$Name)
    foreach ($needle in @("KEY", "TOKEN", "SECRET", "PASSWORD", "PROXY")) {
        if ($Name.ToUpperInvariant().Contains($needle)) { return $true }
    }
    return $false
}

Write-Host ""
Write-Host "==> Effective env summary (redacted)" -ForegroundColor Cyan
$envLines = @()
if (Test-Path ".env") {
    $envLines = Get-Content ".env" | Where-Object {
        ($_ -match "^\s*[A-Za-z_][A-Za-z0-9_]*\s*=") -and ($_ -notmatch "^\s*#")
    }
}
$keysSeen = New-Object System.Collections.Generic.HashSet[string]
foreach ($line in $envLines) {
    $name = ($line -split "=", 2)[0].Trim()
    [void]$keysSeen.Add($name)
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($null -eq $value) {
        $value = (Get-EnvValue -Name $name)
    }
    if (Test-ShouldMask -Name $name) {
        if ([string]::IsNullOrEmpty($value)) {
            Write-Host ("    {0,-32} = <unset>" -f $name)
        } else {
            Write-Host ("    {0,-32} = ***REDACTED*** (len={1})" -f $name, $value.Length)
        }
    } else {
        Write-Host ("    {0,-32} = {1}" -f $name, $value)
    }
}
# Also surface the session-only overrides not in .env
foreach ($name in @("WORKER_COUNT", "MAX_WORKER_COUNT", "ENABLE_PRODUCTION_TASK_FN", "ALLOW_DOM_ONLY_WATCHDOG")) {
    if (-not $keysSeen.Contains($name)) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrEmpty($value)) {
            Write-Host ("    {0,-32} = {1}  (session)" -f $name, $value)
        }
    }
}

# 9. Print git commit
Write-Host ""
try {
    $sha = (git rev-parse HEAD).Trim()
    Write-Host "==> Git commit: $sha" -ForegroundColor Cyan
} catch {
    Write-Host "==> Git commit: <unknown>" -ForegroundColor Yellow
}

# 10. Ensure logs/ exists
if (-not (Test-Path "logs")) {
    New-Item -ItemType Directory -Path "logs" | Out-Null
}

# 11. Run the bot, teeing to a timestamped log file
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path "logs" "bot_$timestamp.log"
Write-Host ""
Write-Host "==> Starting: python -m app" -ForegroundColor Green
Write-Host "    Log: $logFile" -ForegroundColor Green
Write-Host ""

python -m app 2>&1 | Tee-Object -FilePath $logFile
exit $LASTEXITCODE
