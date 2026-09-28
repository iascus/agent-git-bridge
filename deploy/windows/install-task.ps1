<#
.SYNOPSIS
  Registers agent-git-bridge as a hidden per-user scheduled task that starts
  at logon and restarts on failure. Needs no administrator rights.

.DESCRIPTION
  The service listens on 127.0.0.1 only (enforced by configuration).
  Tailscale Serve provides the private HTTPS endpoint; see docs/TAILSCALE_SETUP.md.
#>
param(
    [string]$Config = (Join-Path $env:USERPROFILE ".config\agent-git-bridge\config.yaml"),
    [string]$LogFile = (Join-Path $env:LOCALAPPDATA "agent-git-bridge\service.log"),
    [string]$TaskName = "agent-git-bridge"
)
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Virtual environment not found: $pythonw (run: py -3 -m venv .venv; .venv\Scripts\pip install -e .)" }
if (-not (Test-Path $Config)) { throw "Configuration not found: $Config" }

$arguments = "-m git_bridge --config `"$Config`" --log-file `"$LogFile`" serve"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "agent-git-bridge on http://127.0.0.1 (exposed privately via Tailscale Serve)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Registered and started scheduled task '$TaskName'. Logs: $LogFile"
