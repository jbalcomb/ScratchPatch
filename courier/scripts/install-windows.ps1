<#
.SYNOPSIS
  Install Courier's Phase A broker (ntfy) and gateway as Windows scheduled tasks and publish both
  on your tailnet.

.DESCRIPTION
  1. Creates .venv with Courier's one dependency (cryptography).
  2. Downloads ntfy $NtfyVersion from its GitHub release into bin\, refusing it unless its SHA-256
     matches the one pinned below.
  3. Writes ~/.config/courier: the gateway's signing key, ntfy's server.yml (no anonymous access,
     web app off), a write-only token for the gateway and a read-only token for devices. Only you
     and SYSTEM can read the folder.
  4. Registers two tasks, "Courier ntfy" and "Courier Gateway", that start at logon and are checked
     every 5 minutes (restarted if they died), and starts them.
  5. If Tailscale is installed, runs `tailscale serve` for both ports, so devices reach them at
     https://<this machine>.<tailnet>.ts.net:<port>.
  Rerunning it is safe: keys, tokens, senders and the audit log are kept.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#>
param(
    [int]$GatewayPort = 8766,
    [int]$NtfyPort = 2586,
    [switch]$NoTailscale
)
$ErrorActionPreference = "Stop"

# Pinned: v2.35.0 (2026-10-30) fixes anonymous writes to protected topics via the Matrix gateway.
$NtfyVersion = "2.35.0"
$NtfySha256 = "f50b7f9fd4b4d485aad61e33465cc85ff457894c531ded144404d7b5e4972126"

$project = Split-Path -Parent $PSScriptRoot
$src = Join-Path $project "src"
$data = Join-Path $project "data"
$bin = Join-Path $project "bin"
$config = Join-Path $HOME ".config\courier"
$ntfy = Join-Path $bin "ntfy.exe"

# Stopping the task ends its pythonw launcher; ntfy.exe, its child, has to be stopped separately.
function Stop-CourierNtfy {
    if (Get-ScheduledTask -TaskName "Courier ntfy" -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName "Courier ntfy" }
    Get-Process ntfy -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $ntfy } | Stop-Process -Force
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python -or $python -like "*\WindowsApps\*") {
    throw "Python 3.11+ not found on PATH. Install it from python.org (tick 'Add to PATH')."
}
& $python -c "import sys; sys.exit(sys.version_info < (3, 11))"
if ($LASTEXITCODE) { throw "$python is older than 3.11." }

# 1. venv
$venvPython = Join-Path $project ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { & $python -m venv (Join-Path $project ".venv") }
& $venvPython -m pip install --quiet --disable-pip-version-check "cryptography>=42"
if ($LASTEXITCODE) { throw "Installing cryptography into .venv failed (above)." }
$pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"

# 2. ntfy, verified against the pinned hash
$versionFile = Join-Path $bin "ntfy.version"
if (-not (Test-Path $ntfy) -or (Get-Content $versionFile -ErrorAction SilentlyContinue) -ne $NtfyVersion) {
    $name = "ntfy_${NtfyVersion}_windows_amd64"
    $zip = Join-Path $env:TEMP "$name.zip"
    $url = "https://github.com/binwiederhier/ntfy/releases/download/v$NtfyVersion/$name.zip"
    Write-Host "Downloading $url"
    Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $NtfySha256) {
        Remove-Item $zip -Force
        throw "ntfy download has SHA-256 $hash, expected $NtfySha256. Not installing it."
    }
    $unpacked = Join-Path $env:TEMP $name
    if (Test-Path $unpacked) { Remove-Item -Recurse -Force $unpacked }
    Expand-Archive $zip -DestinationPath $unpacked
    $exe = Get-ChildItem $unpacked -Recurse -Filter ntfy.exe | Select-Object -First 1
    if (-not $exe) { throw "No ntfy.exe inside $zip." }
    Stop-CourierNtfy  # so the binary can be replaced
    New-Item -ItemType Directory -Force $bin | Out-Null
    Copy-Item $exe.FullName $ntfy -Force
    Set-Content $versionFile $NtfyVersion
    Remove-Item -Recurse -Force $unpacked, $zip
}

# 3. Keys, tokens and ntfy's config, in a folder only you (and SYSTEM) can read
New-Item -ItemType Directory -Force $config, $data | Out-Null
icacls $config /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
Push-Location $src
try {
    $gatewayKey = & $venvPython -m courier gateway-key
    & $venvPython -m courier ntfy-setup --ntfy-exe $ntfy --listen "127.0.0.1:$NtfyPort" | Out-Null
    if ($LASTEXITCODE) { throw "ntfy-setup failed (above)." }
} finally { Pop-Location }

# 4. Scheduled tasks, each restarted by a 5-minute watchdog if it died
function Register-CourierTask($TaskName, $Arguments, $Description) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $TaskName }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $Arguments -WorkingDirectory $src
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
        (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))
    )
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
        -Description "$Description Installed by $PSCommandPath." -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}

function Wait-Healthy($Url, $Field) {
    foreach ($i in 1..15) {
        Start-Sleep -Seconds 1
        try { if ((Invoke-RestMethod $Url -TimeoutSec 2).$Field) { return $true } } catch {}
    }
    return $false
}

Stop-CourierNtfy  # picks up the new server.yml on start
Register-CourierTask "Courier ntfy" `
    "-m courier ntfy-run --ntfy-exe `"$ntfy`" --log-file `"$data\ntfy.log`"" `
    "Courier's message broker (ntfy, private: tokens only)."
if (-not (Wait-Healthy "http://127.0.0.1:$NtfyPort/v1/health" "healthy")) {
    throw "ntfy didn't come up on port $NtfyPort. See $data\ntfy.log."
}
Register-CourierTask "Courier Gateway" `
    ("-m courier serve --port $GatewayPort --ntfy http://127.0.0.1:$NtfyPort " +
     "--ntfy-token-file `"$config\ntfy-gateway.token`" --data `"$data`" --log-file `"$data\gateway.log`"") `
    "Courier gateway: screens, signs and publishes messages to ntfy."
if (-not (Wait-Healthy "http://127.0.0.1:$GatewayPort/health" "ok")) {
    throw "The gateway didn't come up on port $GatewayPort. See $data\gateway.log."
}
Write-Host "ntfy is up on http://127.0.0.1:$NtfyPort and the gateway on http://127.0.0.1:$GatewayPort."

# 5. Tailscale
$base = $null
$tailscale = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if ($NoTailscale) {
    Write-Host "Skipped Tailscale. Both only listen on this machine until something proxies them."
} elseif (-not $tailscale) {
    Write-Host "Tailscale isn't on PATH. Once it is:"
    Write-Host "  tailscale serve --bg --https=$GatewayPort http://127.0.0.1:$GatewayPort"
    Write-Host "  tailscale serve --bg --https=$NtfyPort http://127.0.0.1:$NtfyPort"
} else {
    $failed = $false
    foreach ($port in $GatewayPort, $NtfyPort) {
        & $tailscale serve --bg --https=$port "http://127.0.0.1:$port"
        if ($LASTEXITCODE) { $failed = $true }
    }
    if ($failed) {
        Write-Host "tailscale serve failed (above). It may ask you to enable HTTPS for the tailnet first;"
        Write-Host "follow its link, then rerun this script."
    } else {
        $base = "https://" + ((& $tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".")
    }
}
if (-not $base) { $base = "https://<this machine>.<tailnet>.ts.net" }

Write-Host ""
Write-Host "Gateway URL:   ${base}:$GatewayPort"
Write-Host "ntfy URL:      ${base}:$NtfyPort   (devices subscribe here)"
Write-Host "Gateway key:   $gatewayKey   (devices verify envelopes with this)"
Write-Host "Device token:  $config\ntfy-device.token   (read-only; copy it to each device agent)"
Write-Host ""
Write-Host "Add a sender:  cd $src; ..\.venv\Scripts\python -m courier add-sender NAME --kind script --channels clip --gateway ${base}:$GatewayPort"
Write-Host "Logs:          $data\ntfy.log, $data\gateway.log, $data\audit.jsonl"
Write-Host "Remove:        .\scripts\uninstall-windows.ps1"
