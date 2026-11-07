<#
.SYNOPSIS
  Install the Courier device agent as a Windows scheduled task.

.DESCRIPTION
  Creates .venv with Courier's one dependency, writes ~/.config/courier/device.toml if there is
  none (subscriptions, the ntfy URL and the gateway key it trusts), and registers "Courier Device",
  which starts at logon (it needs your session to reach the clipboard) and is checked every
  5 minutes (restarted if it died).

  On the machine that runs the gateway, -Ntfy and -GatewayKey default to this machine's. On any
  other device, copy ntfy-device.token from the gateway machine's ~/.config/courier first.
  Rerunning it is safe; an existing device.toml is kept.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-device-windows.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-device-windows.ps1 -Ntfy https://box.tailnet.ts.net:2586 -GatewayKey <key>
#>
param(
    [string]$Ntfy,
    [string]$GatewayKey,
    [string]$TaskName = "Courier Device"
)
$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot
$src = Join-Path $project "src"
$data = Join-Path $project "data"
$config = Join-Path $HOME ".config\courier"

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python -or $python -like "*\WindowsApps\*") {
    throw "Python 3.11+ not found on PATH. Install it from python.org (tick 'Add to PATH')."
}
& $python -c "import sys; sys.exit(sys.version_info < (3, 11))"
if ($LASTEXITCODE) { throw "$python is older than 3.11." }

$venvPython = Join-Path $project ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { & $python -m venv (Join-Path $project ".venv") }
& $venvPython -m pip install --quiet --disable-pip-version-check "cryptography>=42"
if ($LASTEXITCODE) { throw "Installing cryptography into .venv failed (above)." }
$pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"

New-Item -ItemType Directory -Force $config, $data | Out-Null
icacls $config /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
if (-not (Test-Path (Join-Path $config "ntfy-device.token"))) {
    throw "No device token at $config\ntfy-device.token. Copy it from the gateway machine's ~/.config/courier."
}

if (-not (Test-Path (Join-Path $config "device.toml"))) {
    if (-not $Ntfy) {
        $tailscale = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
        if (-not $tailscale -or -not (Get-ScheduledTask -TaskName "Courier ntfy" -ErrorAction SilentlyContinue)) {
            throw "Pass -Ntfy https://<broker machine>.<tailnet>.ts.net:2586 (this machine doesn't run ntfy)."
        }
        $Ntfy = "https://" + ((& $tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".") + ":2586"
    }
    $initArgs = @("-m", "courier", "device-init", "--ntfy", $Ntfy)
    if ($GatewayKey) { $initArgs += @("--gateway-key", $GatewayKey) }
    Push-Location $src
    try { & $venvPython @initArgs | Out-Null } finally { Pop-Location }
    if ($LASTEXITCODE) { throw "device-init failed (above)." }
    Write-Host "Wrote $config\device.toml."
} else {
    Write-Host "Kept $config\device.toml."
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $TaskName }
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m courier device --data `"$data`" --log-file `"$data\device.log`"" -WorkingDirectory $src
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Courier device agent: acts on messages per $config\device.toml. Installed by $PSCommandPath." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$log = Join-Path $data "device.log"
$up = $false
foreach ($i in 1..15) {
    Start-Sleep -Seconds 1
    if ((Test-Path $log) -and (Select-String -Path $log -Pattern "subscribed to" -Quiet)) { $up = $true; break }
}
if (-not $up) { throw "The agent didn't subscribe within 15 s. See $log." }
Write-Host "Courier Device is running and subscribed (task '$TaskName')."
Write-Host ""
Write-Host "Policy:  $config\device.toml   (restart the task after editing it)"
Write-Host "Inbox:   cd $src; ..\.venv\Scripts\python -m courier inbox"
Write-Host "Logs:    $log, $data\device.jsonl"
Write-Host "Remove:  .\scripts\uninstall-device-windows.ps1"
