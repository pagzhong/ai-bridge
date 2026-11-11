# Install bridge-sentinel on Windows (Task Scheduler, per-user task).
# Usage:  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Member codex
# The task runs sentinel.py every minute while you are logged on.
# NOTE: written 2026-11-11; not yet verified on a real Windows machine —
# the first Windows member to install should report any errors back.
param(
    [Parameter(Mandatory = $true)][string]$Member,
    [int]$IntervalMinutes = 1
)
$ErrorActionPreference = 'Stop'

# --- find a real Python -------------------------------------------------
function Resolve-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($exe -and (Test-Path $exe)) { return $exe.Trim() }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        # The Microsoft Store stub in WindowsApps is 0 bytes — skip it.
        if ($cmd.Source -notlike '*\WindowsApps\*' -or (Get-Item $cmd.Source).Length -gt 0) {
            return $cmd.Source
        }
    }
    return $null
}
$python = Resolve-Python
if (-not $python) {
    Write-Host "Python 3 not found. Install it from https://www.python.org/downloads/"
    Write-Host "(tick 'Add python.exe to PATH'), then re-run this installer."
    exit 1
}
Write-Host "python: $python"
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# --- OpenSSH client check ------------------------------------------------
if (-not (Get-Command sftp -ErrorAction SilentlyContinue)) {
    Write-Host "WARNING: sftp.exe not found. Enable the Windows optional feature"
    Write-Host "'OpenSSH Client' (Settings > Apps > Optional features), then continue."
    Write-Host "Also confirm 'sftp aibridge' connects before relying on the sentinel."
}

# --- install files -------------------------------------------------------
$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$homeDir = Join-Path $env:USERPROFILE '.bridge-sentinel'
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
Copy-Item (Join-Path $srcDir 'sentinel.py') $homeDir -Force
Copy-Item (Join-Path $srcDir 'doorbell.py') $homeDir -Force
$adapterSrc = Join-Path $srcDir 'codex-adapter'
if (Test-Path $adapterSrc) {
    $adapterDst = Join-Path $homeDir 'codex-adapter'
    New-Item -ItemType Directory -Force -Path $adapterDst | Out-Null
    Copy-Item (Join-Path $adapterSrc 'codex_adapter.py') $adapterDst -Force
    Copy-Item (Join-Path $adapterSrc 'README.md') $adapterDst -Force
}
$configPath = Join-Path $homeDir 'config'
if (-not (Test-Path $configPath)) {
    (Get-Content (Join-Path $srcDir 'config.example')) -replace '^MEMBER=.*', "MEMBER=$Member" |
        Set-Content $configPath -Encoding UTF8
    Write-Host "wrote $configPath (MEMBER=$Member) - edit HANDOFF there before relying on delivery"
} else {
    Write-Host "kept existing $configPath"
}

# --- register the scheduled task -----------------------------------------
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$(Join-Path $homeDir 'sentinel.py')`"" -WorkingDirectory $homeDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'BridgeSentinel' -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "installed: scheduled task 'BridgeSentinel' runs sentinel.py every $IntervalMinutes minute(s)"

# --- doorbell task (only when the config has your ntfy account) ------------
if (Select-String -Path $configPath -Pattern '^NTFY_USER=.' -Quiet) {
    $dbAction = New-ScheduledTaskAction -Execute $pythonw `
        -Argument "`"$(Join-Path $homeDir 'doorbell.py')`"" -WorkingDirectory $homeDir
    $dbTrigger = New-ScheduledTaskTrigger -AtLogon -User $env:USERNAME
    $dbSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    Register-ScheduledTask -TaskName 'BridgeDoorbell' -Action $dbAction -Trigger $dbTrigger `
        -Settings $dbSettings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName 'BridgeDoorbell'
    Write-Host "installed: scheduled task 'BridgeDoorbell' (group-notice listener, ~2-5s delivery)"
} else {
    Write-Host "doorbell NOT enabled: fill NTFY_USER/NTFY_PASS (your own ntfy account) in $configPath, then re-run this installer"
}

# --- self-check: one cycle now --------------------------------------------
Write-Host "running one cycle now..."
& $python (Join-Path $homeDir 'sentinel.py')
Write-Host "done. Log: $(Join-Path $homeDir 'sentinel.log')"
Write-Host "Manage with: Get-ScheduledTask BridgeSentinel / Unregister-ScheduledTask BridgeSentinel"
