<#
.SYNOPSIS
  Remove the Discord Capture scheduled task. The database, log and token are left alone.
#>
param([string]$TaskName = "Discord Capture sync")
$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
} else {
    Write-Host "No task named '$TaskName'."
}
Write-Host "Kept: data\ (database, log) and $HOME\.config\discord-capture\token. Delete them by hand if you want them gone."
