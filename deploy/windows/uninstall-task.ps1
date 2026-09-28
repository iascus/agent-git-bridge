<#
.SYNOPSIS
  Stops and removes the agent-git-bridge scheduled task. Does not touch
  configuration, secrets, clones or the Tailscale Serve configuration.
#>
param([string]$TaskName = "agent-git-bridge")
$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
} else {
    Write-Host "No scheduled task named '$TaskName'."
}
