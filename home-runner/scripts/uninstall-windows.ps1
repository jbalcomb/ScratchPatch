<#
.SYNOPSIS
  Stop and remove the Home Runner task and its `tailscale serve` entry. Run history and the key are kept.
#>
param([int]$Port = 8765, [string]$TaskName = "Home Runner")
$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
} else {
    Write-Host "No task named '$TaskName'."
}
$tailscale = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if ($tailscale) {
    & $tailscale serve --https=$Port off 2>$null
    if (-not $LASTEXITCODE) { Write-Host "Removed the tailscale serve entry for port $Port." }
}
Write-Host "Kept: data\ (run history, logs) and $HOME\.config\home-runner\key."
