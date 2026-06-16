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

# --- Phase: egress + wheelhouse (Task 2) ---
function Test-Egress {
    Write-Log "checking SOCKS egress via $Proxy" "STEP"
    foreach ($u in $EgressUrls) {
        $code = & curl.exe -sS --proxy $Proxy --connect-timeout 12 --max-time 25 -o NUL -w "%{http_code}" -I $u 2>$null
        Write-Log "  $u -> HTTP $code"
        if ($code -ne "200") {
            throw "Egress check failed for $u (HTTP '$code'). Populate $Wheelhouse from another machine, then re-run with -SkipWheelhouse."
        }
    }
    Write-Log "egress OK" "INFO"
}

function Get-WheelUrl {
    # Read the PyPI simple index through the proxy; return the newest
    # py3-none-any wheel whose version satisfies $VersionFilter.
    param([string]$Package, [scriptblock]$VersionFilter)
    $html = & curl.exe -sS --proxy $Proxy --max-time 30 "https://pypi.org/simple/$Package/" 2>$null
    $pattern = 'href="(?<url>[^"]+?/(?<file>' + [regex]::Escape($Package) + '-(?<ver>[0-9][^-]*)-py3-none-any\.whl)(#[^"]*)?)"'
    $mm = [regex]::Matches([string]$html, $pattern)
    $cands = foreach ($m in $mm) {
        $verStr = $m.Groups['ver'].Value
        try { $v = [version]($verStr -replace '[^0-9.].*$', '') } catch { continue }
        if (& $VersionFilter $v) {
            [pscustomobject]@{ ver = $v; file = $m.Groups['file'].Value; url = ($m.Groups['url'].Value -replace '#.*$', '') }
        }
    }
    $best = $cands | Sort-Object ver -Descending | Select-Object -First 1
    if (-not $best) { throw "No matching py3-none-any wheel for '$Package' on the PyPI simple index." }
    return $best
}

function Get-Wheelhouse {
    if ($SkipWheelhouse -and (Test-Path $Wheelhouse) -and (Get-ChildItem $Wheelhouse -Filter *.whl -ErrorAction SilentlyContinue)) {
        Write-Log "wheelhouse: reusing cached $Wheelhouse (-SkipWheelhouse)" "INFO"
        return
    }
    $needed = @(
        @{ pkg = "setuptools"; filter = { param($v) ($v -ge [version]"77.0") -and ($v -lt [version]"83.0") } },
        @{ pkg = "wheel";      filter = { param($v) $true } }
    )
    Invoke-Action "create $Wheelhouse and download build wheels via curl ($Proxy)" {
        New-Item -ItemType Directory -Force -Path $Wheelhouse | Out-Null
        foreach ($n in $needed) {
            $w = Get-WheelUrl -Package $n.pkg -VersionFilter $n.filter
            $dest = Join-Path $Wheelhouse $w.file
            if (Test-Path $dest) { Write-Log "  cached: $($w.file)"; continue }
            Write-Log "  fetching $($w.file)"
            & curl.exe -sS --proxy $Proxy --max-time 120 -o $dest $w.url
            if (-not (Test-Path $dest)) { throw "curl failed to download $($w.url)" }
        }
        Write-Log "wheelhouse ready: $((Get-ChildItem $Wheelhouse -Filter *.whl).Name -join ', ')" "INFO"
    }
    if (-not $Execute) {
        foreach ($n in $needed) {
            $w = Get-WheelUrl -Package $n.pkg -VersionFilter $n.filter
            Write-Log "would fetch: $($w.file)  <-  $($w.url)" "DRY"
        }
    }
}

# --- Phase: build venv + build wheel (Task 3) ---
function New-BuildVenv {
    $bpy = Join-Path $BuildVenv "Scripts\python.exe"
    if (-not (Test-Path $SysPy)) {
        if ($Execute) { throw "System Python not found at $SysPy (needed to create the build venv)." }
        Write-Log "system python NOT found at $SysPy (would be required for -Execute)" "WARN"
    } else {
        Write-Log "build venv target: $BuildVenv (from system python $SysPy)" "INFO"
    }
    Invoke-Action "recreate build venv: `"$SysPy`" -m venv `"$BuildVenv`"" {
        if (Test-Path $BuildVenv) { Remove-Item $BuildVenv -Recurse -Force }
        & $SysPy -m venv $BuildVenv
        if (-not (Test-Path $bpy)) { throw "build venv creation failed: $bpy missing" }
    }
    Invoke-Action "`"$bpy`" -m pip install --no-index --find-links `"$Wheelhouse`" `"setuptools>=77,<83`" wheel" {
        & $bpy -m pip install --no-index --find-links $Wheelhouse "setuptools>=77,<83" wheel
        if ($LASTEXITCODE -ne 0) { throw "build venv: offline install of setuptools/wheel from wheelhouse failed" }
        $ver = & $bpy -c "import setuptools; print(setuptools.__version__)"
        Write-Log "build venv setuptools=$ver" "INFO"
    }
}

function Build-Wheel {
    $bpy = Join-Path $BuildVenv "Scripts\python.exe"
    Invoke-Action "clean $DistDir, then build: `"$bpy`" -m pip wheel `"$RepoRoot`" --no-build-isolation --no-deps -w `"$DistDir`"" {
        if (Test-Path $DistDir) { Remove-Item (Join-Path $DistDir "*.whl") -Force -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
        & $bpy -m pip wheel $RepoRoot --no-build-isolation --no-deps -w $DistDir
        if ($LASTEXITCODE -ne 0) { throw "pip wheel build failed" }
        $whl = Get-ChildItem $DistDir -Filter "hermes_agent-*.whl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $whl) { throw "no hermes_agent wheel produced in $DistDir" }
        Write-Log "built wheel: $($whl.Name)" "INFO"
        $script:BuiltWheel = $whl.FullName
    }
}

# --- Main dispatch ---
$mode = Resolve-Mode
Write-Log "reinstall-from-repo starting | mode=$mode | repo=$RepoRoot | live=$LiveVenv | log=$LogFile" "STEP"
switch ($mode) {
    "rollback" { Write-Log "rollback mode (stub - implemented in Task 6)" "WARN" }
    "execute"  { Write-Log "execute mode (stub - implemented in Task 6)" "WARN" }
    "dryrun"   {
        Write-Log "DRY-RUN: no side effects (egress is read-only; nothing downloaded/built)." "INFO"
        Test-Egress
        Get-Wheelhouse
        New-BuildVenv
        Build-Wheel
    }
}
Write-Log "done (mode=$mode)" "STEP"
