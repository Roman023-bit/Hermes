#requires -Version 5.1
<#
.SYNOPSIS
  Reproducible reinstall of Hermes into the live pypi-venv from local repo main.
.DESCRIPTION
  Default = dry-run (checks + plan, no side effects). -Execute performs the real
  build+install with backup/rollback. -Rollback <path> restores a venv snapshot.
#>
[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$Rollback,
    [switch]$SkipWheelhouse,
    [string]$Proxy = "socks5h://127.0.0.1:10808"
)

$ErrorActionPreference = "Stop"   # fail-fast
Set-StrictMode -Version Latest

# --- Paths (HERMES_HOME-aware) ---
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$LiveVenv   = Join-Path $HermesHome "pypi-venv"
$LivePy     = Join-Path $LiveVenv "Scripts\python.exe"
$HermesExe  = Join-Path $LiveVenv "Scripts\hermes.exe"
$SysPy      = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
$BuildDir   = Join-Path $RepoRoot "build"
$Wheelhouse = Join-Path $BuildDir "wheelhouse"
$BuildVenv  = Join-Path $BuildDir "buildenv"
$DistDir    = Join-Path $BuildDir "dist"
$TaskName   = "Hermes Gateway Watchdog"
$Stamp      = Get-Date -Format "yyyyMMdd_HHmmss"
$LogDir     = Join-Path $HermesHome "logs"
$LogFile    = Join-Path $LogDir ("reinstall-from-repo-{0}.log" -f $Stamp)
$EgressUrls = @("https://pypi.org/simple/", "https://files.pythonhosted.org/", "https://github.com/")

# --- Logging ---
function Write-Log {
    param([string]$Message, [ValidateSet("INFO","STEP","WARN","ERROR","DRY")] [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "HH:mm:ss"), $Level, $Message
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    $color = @{ INFO="Gray"; STEP="Cyan"; WARN="Yellow"; ERROR="Red"; DRY="DarkGray" }[$Level]
    Write-Host $line -ForegroundColor $color
}

# --- Dry-run wrapper: in dry-run, log intent; in execute, run the action ---
function Invoke-Action {
    param([string]$What, [scriptblock]$Action)
    if ($Execute) { Write-Log "DO: $What" "STEP"; & $Action }
    else { Write-Log "would: $What" "DRY" }
}

function Resolve-Mode {
    if ($Rollback) { return "rollback" }
    if ($Execute)  { return "execute" }
    return "dryrun"
}

# --- Phase functions are added in later tasks ---

# --- Main dispatch ---
$mode = Resolve-Mode
Write-Log "reinstall-from-repo starting | mode=$mode | repo=$RepoRoot | live=$LiveVenv | log=$LogFile" "STEP"
switch ($mode) {
    "rollback" { Write-Log "rollback mode (stub - implemented in Task 6)" "WARN" }
    "execute"  { Write-Log "execute mode (stub - implemented in Task 6)" "WARN" }
    "dryrun"   { Write-Log "DRY-RUN: no side effects. (full preview wired in later tasks)" "INFO" }
}
Write-Log "done (mode=$mode)" "STEP"
