#requires -Version 5.1
<#
.SYNOPSIS
  Reproducible reinstall of Hermes into the live pypi-venv from local repo main.

.DESCRIPTION
  Builds a Hermes wheel from the local repo in an isolated build venv (offline,
  using build tooling fetched into build/wheelhouse via SOCKS curl), then installs
  it into the live pypi-venv fully offline. Wraps the live phases with backup,
  watchdog pause/restore, smoke verification, and automatic rollback.

  Default invocation is a DRY-RUN: it runs read-only checks (egress, wheel-URL
  resolution, smoke) and prints the exact plan WITHOUT downloading, building,
  stopping the gateway, or modifying the live runtime. Pass -Execute to perform
  the real reinstall. Pass -Rollback <path> to restore a venv snapshot (preview
  only unless combined with -Execute).

.PARAMETER Execute
  Perform the real build + install (and, in -Rollback mode, the real restore).
  Without it, every side-effecting step is only previewed ("would: ...").

.PARAMETER Rollback
  Path to a pypi-venv.bak-<timestamp> snapshot to restore. Selects rollback mode;
  preview-only unless -Execute is also passed. Must be inside HERMES_HOME.

.PARAMETER SkipWheelhouse
  Reuse an existing build/wheelhouse instead of re-resolving/downloading the
  build wheels (setuptools, wheel).

.PARAMETER Proxy
  SOCKS proxy for curl when fetching the wheelhouse.
  Default: socks5h://127.0.0.1:10808.

.EXAMPLE
  scripts\reinstall-from-repo.ps1
  Dry-run: read-only checks + full plan, no side effects.

.EXAMPLE
  scripts\reinstall-from-repo.ps1 -Execute
  Real reinstall: build -> backup -> stop -> install -> smoke -> restart
  (auto-rollback on smoke failure).

.EXAMPLE
  scripts\reinstall-from-repo.ps1 -Execute -SkipWheelhouse
  Real reinstall reusing the cached build/wheelhouse.

.EXAMPLE
  scripts\reinstall-from-repo.ps1 -Rollback "$env:LOCALAPPDATA\hermes\pypi-venv.bak-20260616_063721"
  Preview a rollback. Add -Execute to actually restore the snapshot.

.NOTES
  Windows PowerShell 5.1. Requires curl.exe (SOCKS5) and system Python 3.11.
  The live runtime is touched only under -Execute, and only after a successful build.
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

# --- Path guards (run always, incl. dry-run; throw before any destructive op) ---
function Assert-SafePaths {
    if ([string]::IsNullOrWhiteSpace($HermesHome)) { throw "guard: HERMES_HOME is empty" }
    if (-not (Test-Path $HermesHome)) { throw "guard: HERMES_HOME not found: $HermesHome" }
    if ((Split-Path $LiveVenv -Leaf) -ne "pypi-venv") { throw "guard: LiveVenv must end with 'pypi-venv', got: $LiveVenv" }
    $hh = [System.IO.Path]::GetFullPath($HermesHome)
    $lv = [System.IO.Path]::GetFullPath($LiveVenv)
    if (-not $lv.StartsWith($hh, [System.StringComparison]::OrdinalIgnoreCase)) { throw "guard: LiveVenv not inside HERMES_HOME ($LiveVenv)" }
}

function Assert-PathInsideHermesHome {
    param([string]$Path)
    $hh = [System.IO.Path]::GetFullPath($HermesHome)
    $p  = [System.IO.Path]::GetFullPath($Path)
    if (-not $p.StartsWith($hh, [System.StringComparison]::OrdinalIgnoreCase)) { throw "guard: path not inside HERMES_HOME: $Path" }
}

# --- Phase: live-runtime helpers (Task 4) ---
function Backup-LiveVenv {
    Assert-SafePaths
    $bak = "$LiveVenv.bak-$Stamp"
    Assert-PathInsideHermesHome -Path $bak
    Write-Log "backup plan: '$LiveVenv' -> '$bak' (+ config.yaml/.env copies)" "INFO"
    Invoke-Action "snapshot live venv to '$bak' and back up config.yaml/.env" {
        if (Test-Path $bak) { throw "backup already exists: $bak" }
        Copy-Item $LiveVenv $bak -Recurse
        if (-not (Test-Path (Join-Path $bak "Scripts\python.exe"))) { throw "venv backup incomplete: $bak" }
        Copy-Item (Join-Path $HermesHome "config.yaml") (Join-Path $HermesHome "config.yaml.bak-reinstall-$Stamp") -Force
        Copy-Item (Join-Path $HermesHome ".env")        (Join-Path $HermesHome ".env.bak-reinstall-$Stamp") -Force
        Write-Log "backup ready: $bak" "INFO"
    }
    $script:BackupPath = $bak
    return $bak
}

function Set-Watchdog {
    param([ValidateSet("Disable","Enable")] [string]$Action)
    Invoke-Action "$Action scheduled task '$TaskName'" {
        if ($Action -eq "Disable") { Disable-ScheduledTask -TaskName $TaskName | Out-Null }
        else { Enable-ScheduledTask -TaskName $TaskName | Out-Null }
        Write-Log "watchdog $($Action.ToLower())d" "INFO"
    }
}

function Stop-Gateway {
    Assert-SafePaths
    Invoke-Action "stop gateway ('$HermesExe' gateway stop) and ensure no 'gateway run' processes remain" {
        & $HermesExe gateway stop 2>&1 | Out-Null
        Start-Sleep -Seconds 3
        $rem = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='hermes.exe'" | Where-Object { $_.CommandLine -match 'gateway run' }
        if ($rem) { $rem | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 2 }
        Write-Log "gateway stopped" "INFO"
    }
}

function Start-Gateway {
    Assert-SafePaths
    Invoke-Action "start gateway: '$HermesExe' gateway run --replace --accept-hooks -v (detached) and confirm running" {
        $out = Join-Path $LogDir "gateway-run.out.log"
        $err = Join-Path $LogDir "gateway-run.err.log"
        Start-Process -FilePath $HermesExe -ArgumentList 'gateway','run','--replace','--accept-hooks','-v' `
            -WorkingDirectory $HermesHome -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden | Out-Null
        Start-Sleep -Seconds 22
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='hermes.exe'" | Where-Object { $_.CommandLine -match 'gateway run' }
        if (-not $procs) { throw "gateway did not come up after start" }
        Write-Log "gateway running (pids: $(($procs.ProcessId) -join ','))" "INFO"
    }
}

# --- Phase: offline install + smoke (Task 5) ---
function Install-Wheel {
    Assert-SafePaths
    Invoke-Action "offline install into live venv: `"$LivePy`" -m pip install --force-reinstall --no-deps --no-index --find-links `"$DistDir`" hermes-agent" {
        if (-not $script:BuiltWheel -or -not (Test-Path $script:BuiltWheel)) { throw "no built wheel to install (Build-Wheel must run first)" }
        & $LivePy -m pip install --force-reinstall --no-deps --no-index --find-links $DistDir hermes-agent
        if ($LASTEXITCODE -ne 0) { throw "live offline install failed" }
        Write-Log "installed wheel into live venv" "INFO"
    }
}

function Invoke-Smoke {
    # Read-only, no network. Version-tolerant: passes on both the current runtime
    # and a freshly reinstalled (repo-main) runtime. Returns $true if all gates pass.
    Write-Log "running smoke checks (read-only, no network) against '$LivePy'" "STEP"
    if (-not (Test-Path $LivePy)) { Write-Log "smoke: live python not found: $LivePy" "ERROR"; return $false }
    $cfg = Join-Path $HermesHome "config.yaml"
    $checks = [ordered]@{
        imports         = "import tools.agent_profiles, tools.cost_ledger, tools.tool_pricing, gateway.run, gateway.slash_commands, cli; print('OK')"
        profile_schema  = "import tools.delegate_tool as d; assert 'profile' in d.DELEGATE_TASK_SCHEMA['parameters']['properties']; print('OK')"
        profile_resolve = "import yaml, tools.agent_profiles as a; p=a.resolve_profile('researcher', yaml.safe_load(open(r'$cfg',encoding='utf-8'))); assert p and p.get('model'); print('OK', p['model'])"
        spend_report    = "from tools import cost_ledger as c; r=c.render_spend_report(); assert isinstance(r,str) and r.strip(); print('OK')"
        spend_handler   = "import inspect, gateway.run as g; from gateway.slash_commands import GatewaySlashCommandsMixin as M; assert hasattr(M,'_handle_spend_command') or ('_handle_spend_command' in inspect.getsource(g)); print('OK')"
        perplexity      = "import tools.web_tools as w, importlib.util as u; assert hasattr(w,'_perplexity_search') or (u.find_spec('plugins.web.perplexity.provider') is not None); print('OK')"
    }
    $allOk = $true
    foreach ($name in $checks.Keys) {
        $out = & $LivePy -c $checks[$name] 2>&1
        $ok = ($LASTEXITCODE -eq 0) -and ("$out" -match 'OK')
        $detail = ("$out" -replace '\s+', ' ').Trim()
        Write-Log ("  smoke[{0}] = {1} :: {2}" -f $name, $(if ($ok) { 'PASS' } else { 'FAIL' }), $detail) $(if ($ok) { 'INFO' } else { 'ERROR' })
        if (-not $ok) { $allOk = $false }
    }
    # Replicate/image/video plugins live in HERMES_HOME (untouched by install).
    # Per spec: verify the ones present now; absence is informational (not a gate).
    foreach ($rp in @("plugins\image_gen\replicate", "plugins\replicate-video")) {
        $full = Join-Path $HermesHome $rp
        if (Test-Path $full) { Write-Log "  smoke[replicate:$rp] = PASS (present)" "INFO" }
        else { Write-Log "  smoke[replicate:$rp] = absent (not installed; skipped)" "WARN" }
    }
    Write-Log ("smoke result: {0}" -f $(if ($allOk) { 'ALL PASS' } else { 'FAILURES' })) $(if ($allOk) { 'INFO' } else { 'ERROR' })
    return $allOk
}

# --- Phase: rollback + execute orchestration (Task 6) ---
function Restore-FromBackup {
    param([string]$BackupPath)
    Assert-SafePaths
    Assert-PathInsideHermesHome -Path $BackupPath
    if (-not (Test-Path (Join-Path $BackupPath "Scripts\python.exe"))) {
        if ($Execute) { throw "invalid backup (no Scripts\python.exe): $BackupPath" }
        Write-Log "rollback target not validated (dry-run): expects '$BackupPath\Scripts\python.exe'" "WARN"
    }
    Write-Log "rollback plan: restore live venv '$LiveVenv' from backup '$BackupPath'" "INFO"
    Invoke-Action "disable watchdog, stop gateway, remove '$LiveVenv', rename '$BackupPath' -> '$LiveVenv', enable watchdog" {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        & $HermesExe gateway stop 2>&1 | Out-Null
        Start-Sleep -Seconds 3
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='hermes.exe'" |
            Where-Object { $_.CommandLine -match 'gateway run' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        if (Test-Path $LiveVenv) { Remove-Item $LiveVenv -Recurse -Force }
        Rename-Item $BackupPath $LiveVenv
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    }
    Start-Gateway
    Write-Log "rollback complete" "WARN"
}

function Invoke-RollbackMode {
    if ([string]::IsNullOrWhiteSpace($Rollback)) { throw "-Rollback requires a backup path" }
    if ($Execute -and -not (Test-Path $Rollback)) { throw "rollback path not found: $Rollback" }
    if (-not (Test-Path $Rollback)) { Write-Log "rollback path does not exist (dry-run preview only): $Rollback" "WARN" }
    Restore-FromBackup -BackupPath $Rollback
}

function Invoke-Execute {
    Write-Log "EXECUTE: full reinstall sequence" "STEP"
    # Non-live phases first (fail-fast: abort before touching the live runtime).
    Test-Egress
    Get-Wheelhouse
    New-BuildVenv
    Build-Wheel
    # Live phases (auto-rollback on any failure after backup).
    $bak = Backup-LiveVenv
    Set-Watchdog -Action Disable
    Stop-Gateway
    try {
        Install-Wheel
        if (-not (Invoke-Smoke)) { throw "smoke checks failed" }
    } catch {
        Write-Log "live phase failed: $($_.Exception.Message) -- auto-rolling back" "ERROR"
        Restore-FromBackup -BackupPath $bak
        throw "Reinstall FAILED and was rolled back. Backup kept at $bak. Log: $LogFile"
    }
    Set-Watchdog -Action Enable
    Start-Gateway
    Invoke-Action "final gateway status" {
        $st = & $HermesExe gateway status 2>&1
        Write-Log "gateway status: $(("$st" -replace '\s+', ' ').Trim())" "INFO"
    }
    Write-Log "SUCCESS: reinstall complete; backup retained at $bak (delete when satisfied)" "STEP"
}

# --- Main dispatch ---
$mode = Resolve-Mode
Write-Log "reinstall-from-repo starting | mode=$mode | repo=$RepoRoot | live=$LiveVenv | log=$LogFile" "STEP"
switch ($mode) {
    "rollback" { Invoke-RollbackMode }
    "execute"  { Invoke-Execute }
    "dryrun"   {
        Write-Log "DRY-RUN: no side effects (egress is read-only; nothing downloaded/built)." "INFO"
        Write-Log "phase order: egress -> wheelhouse -> build venv -> build wheel -> backup -> disable watchdog -> stop -> install -> smoke -> enable watchdog -> start -> status" "INFO"
        Test-Egress
        Get-Wheelhouse
        New-BuildVenv
        Build-Wheel
        Write-Log "DRY-RUN preview of live phases:" "INFO"
        $null = Backup-LiveVenv
        Set-Watchdog -Action Disable
        Stop-Gateway
        Install-Wheel
        $smokeOk = Invoke-Smoke
        Write-Log ("smoke (dry-run rehearsal, read-only) => {0}; under -Execute a FAIL auto-rolls back" -f $(if ($smokeOk) { 'PASS' } else { 'FAIL' })) $(if ($smokeOk) { 'INFO' } else { 'WARN' })
        Set-Watchdog -Action Enable
        Start-Gateway
        Write-Log "----- DRY-RUN SUMMARY -----" "STEP"
        Write-Log "  mode          : $mode (Execute=$Execute, SkipWheelhouse=$SkipWheelhouse)" "INFO"
        Write-Log "  repo          : $RepoRoot" "INFO"
        Write-Log "  HERMES_HOME   : $HermesHome" "INFO"
        Write-Log "  live venv     : $LiveVenv" "INFO"
        Write-Log "  wheelhouse    : $Wheelhouse" "INFO"
        Write-Log "  build venv    : $BuildVenv" "INFO"
        Write-Log "  dist          : $DistDir" "INFO"
        Write-Log "  watchdog task : $TaskName" "INFO"
        Write-Log "  proxy         : $Proxy" "INFO"
        Write-Log "  log           : $LogFile" "INFO"
        Write-Log "  to apply for real: scripts\reinstall-from-repo.ps1 -Execute" "INFO"
    }
}
Write-Log "done (mode=$mode)" "STEP"
