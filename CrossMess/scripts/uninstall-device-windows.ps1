<#
.SYNOPSIS
  Stop and remove the Courier Device task. device.toml, the token and data\ (inbox, logs) are kept.
#>
param([string]$TaskName = "Courier Device")
$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
} else {
    Write-Host "No task named '$TaskName'."
}
Write-Host "Kept: $HOME\.config\courier (device.toml, token) and data\ (inbox, logs)."
