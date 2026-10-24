<#
.SYNOPSIS
  Install the Discord Capture sync as a Windows scheduled task.

.DESCRIPTION
  Saves the bot token (prompting for it if needed) to ~/.config/discord-capture/token,
  readable only by you, checks it against Discord, then registers a task that runs
  `discord_capture sync` every -Minutes minutes while you're logged on (a locked
  screen counts). Rerunning it replaces the task, so it's also how you change the interval.

.EXAMPLE
  .\scripts\install-windows.ps1
  .\scripts\install-windows.ps1 -Minutes 30
  .\scripts\install-windows.ps1 -NewToken
#>
param(
    [ValidateRange(5, 1440)][int]$Minutes = 60,
    [switch]$NewToken,
    [string]$TaskName = "Discord Capture sync"
)
$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot
$src = Join-Path $project "src"
$data = Join-Path $project "data"
$db = Join-Path $data "discord.sqlite3"
$log = Join-Path $data "sync.log"
$tokenDir = Join-Path $HOME ".config\discord-capture"
$tokenFile = Join-Path $tokenDir "token"

# Python 3.10+, and not the Microsoft Store stub that only opens the Store.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python -or $python -like "*\WindowsApps\*") {
    throw "Python 3.10+ not found on PATH. Install it from python.org (tick 'Add to PATH')."
}
& $python -c "import sys; sys.exit(sys.version_info < (3, 10))"
if ($LASTEXITCODE) { throw "$python is older than 3.10." }
# pythonw runs without opening a console window every hour.
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

New-Item -ItemType Directory -Force $data, $tokenDir | Out-Null

if ($NewToken -or -not (Test-Path $tokenFile)) {
    $secure = Read-Host "Paste the bot token (Developer Portal > your app > Bot > Reset Token)" -AsSecureString
    $token = [Net.NetworkCredential]::new("", $secure).Password.Trim()
    if (-not $token) { throw "No token entered." }
    [IO.File]::WriteAllText($tokenFile, $token)  # UTF-8, no BOM, in Windows PowerShell 5.1 and 7
    # Only you (and SYSTEM, which can read everything anyway) can read the token file.
    icacls $tokenFile /inheritance:r /grant:r "${env:USERNAME}:F" "SYSTEM:F" | Out-Null
    Write-Host "Token saved to $tokenFile"
}

Write-Host "Checking the token against Discord..."
Push-Location $src
try {
    & $python -m discord_capture channels --db $db
    if ($LASTEXITCODE) { throw "The check failed (exit $LASTEXITCODE); not installing the task. Fix the token with -NewToken." }
} finally { Pop-Location }

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m discord_capture sync --db `"$db`" --log-file `"$log`"" -WorkingDirectory $src
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Fetch new Discord messages into $db. Installed by $PSCommandPath." -Force | Out-Null

Write-Host ""
Write-Host "Installed '$TaskName': every $Minutes min, first run in 2 min."
Write-Host "  Log:      $log"
Write-Host "  Database: $db"
Write-Host "  Run now:  Start-ScheduledTask '$TaskName'"
Write-Host "  Remove:   .\scripts\uninstall-windows.ps1"
Write-Host ""
Write-Host "For the historic catch-up, run this once (resumable; the scheduled sync waits while it runs):"
Write-Host "  cd `"$src`"; python -m discord_capture backfill --db `"$db`" -v"
