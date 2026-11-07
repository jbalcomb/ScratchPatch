<#
.SYNOPSIS
  Stop and remove the Courier tasks and their `tailscale serve` entries. Keys, tokens, senders,
  bin\ntfy.exe and data\ (logs, audit log, message cache) are kept.
#>
param([int]$GatewayPort = 8766, [int]$NtfyPort = 2586)
$ErrorActionPreference = "Stop"

foreach ($taskName in "Courier Gateway", "Courier ntfy") {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed '$taskName'."
    } else {
        Write-Host "No task named '$taskName'."
    }
}
# The task's pythonw launcher is gone; stop the ntfy.exe it started.
$ntfy = Join-Path (Split-Path -Parent $PSScriptRoot) "bin\ntfy.exe"
Get-Process ntfy -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $ntfy } | Stop-Process -Force
$tailscale = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if ($tailscale) {
    foreach ($port in $GatewayPort, $NtfyPort) {
        & $tailscale serve --https=$port off 2>$null
        if (-not $LASTEXITCODE) { Write-Host "Removed the tailscale serve entry for port $port." }
    }
}
Write-Host "Kept: $HOME\.config\courier (keys, tokens, senders), bin\ and data\."
