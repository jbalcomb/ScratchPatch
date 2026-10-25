<#
.SYNOPSIS
  Install Home Runner as a Windows scheduled task and publish it on your tailnet.

.DESCRIPTION
  Creates the access key (~/.config/home-runner/key, readable only by you), registers a
  task that starts the runner at logon and checks every 5 minutes that it's still up
  (restarting it if not), starts it now, and, if Tailscale is installed, runs
  `tailscale serve` so the phone reaches it at https://<this machine>.<tailnet>.ts.net:<Port>.
  Rerunning it is safe; the key is kept.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#>
param(
    [int]$Port = 8765,
    [switch]$NoTailscale,
    [string]$TaskName = "Home Runner"
)
$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot
$src = Join-Path $project "src"
$data = Join-Path $project "data"
$log = Join-Path $data "runner.log"
$keyFile = Join-Path $HOME ".config\home-runner\key"

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python -or $python -like "*\WindowsApps\*") {
    throw "Python 3.11+ not found on PATH. Install it from python.org (tick 'Add to PATH')."
}
& $python -c "import sys; sys.exit(sys.version_info < (3, 11))"
if ($LASTEXITCODE) { throw "$python is older than 3.11." }
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
New-Item -ItemType Directory -Force $data | Out-Null

Push-Location $src
try { $key = & $python -m home_runner key } finally { Pop-Location }
icacls $keyFile /inheritance:r /grant:r "${env:USERNAME}:F" "SYSTEM:F" | Out-Null

# Stop a running copy first so the new settings (port, paths) take effect.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
}
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m home_runner serve --port $Port --log-file `"$log`"" -WorkingDirectory $src
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    # The watchdog: if the runner is up this start is ignored (IgnoreNew); if it died, it comes back.
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Runs the jobs in $project\jobs.toml on request. Installed by $PSCommandPath." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$up = $false
foreach ($i in 1..15) {
    Start-Sleep -Seconds 1
    try { $up = (Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2).ok; if ($up) { break } } catch {}
}
if (-not $up) { throw "The runner didn't come up on port $Port. See $log." }
Write-Host "Home Runner is up on http://127.0.0.1:$Port (task '$TaskName')."

$url = $null
$tailscale = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if ($NoTailscale) {
    Write-Host "Skipped Tailscale. The runner only listens on this machine until something proxies it."
} elseif (-not $tailscale) {
    Write-Host "Tailscale isn't on PATH. Once it is: tailscale serve --bg --https=$Port http://127.0.0.1:$Port"
} else {
    & $tailscale serve --bg --https=$Port "http://127.0.0.1:$Port"
    if ($LASTEXITCODE) {
        Write-Host "tailscale serve failed (above). It may ask you to enable HTTPS for the tailnet first;"
        Write-Host "follow its link, then rerun this script."
    } else {
        $name = ((& $tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".")
        $url = "https://${name}:$Port"
    }
}

Write-Host ""
Write-Host "In TaskDeck, open Home Jobs > settings (tune icon) and enter:"
Write-Host "  Runner URL:  $(if ($url) { $url } else { "https://<this machine>.<tailnet>.ts.net:$Port" })"
Write-Host "  Runner key:  $key"
Write-Host ""
Write-Host "Key file: $keyFile (new key: python -m home_runner key --new, then restart the task)"
Write-Host "Log:      $log"
Write-Host "Remove:   .\scripts\uninstall-windows.ps1"
