# Uninstall bridge-sentinel on Windows.
# Usage:  powershell -ExecutionPolicy Bypass -File .\uninstall-windows.ps1 [-Purge]
#   default: unregister the scheduled task and remove the installed program
#            files. Your state (%USERPROFILE%\.bridge-sentinel) is KEPT.
#   -Purge:  also delete the state directory entirely.
# NOTE: not yet verified on a real Windows machine (2026-11-11).
param([switch]$Purge)
$ErrorActionPreference = 'Stop'

if (Get-ScheduledTask -TaskName 'BridgeSentinel' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'BridgeSentinel' -Confirm:$false
    Unregister-ScheduledTask -TaskName 'BridgeDoorbell' -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed scheduled task 'BridgeSentinel'"
}

$homeDir = Join-Path $env:USERPROFILE '.bridge-sentinel'
Remove-Item (Join-Path $homeDir 'sentinel.py') -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $homeDir 'doorbell.py') -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $homeDir 'codex-adapter') -Recurse -Force -ErrorAction SilentlyContinue

if ($Purge) {
    Remove-Item $homeDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "purged $homeDir (config, seen list, letters, logs)"
} else {
    Write-Host "kept state in $homeDir (config, seen list, letters, logs)"
    Write-Host "re-run with -Purge to delete it too"
}
Write-Host "uninstalled."
