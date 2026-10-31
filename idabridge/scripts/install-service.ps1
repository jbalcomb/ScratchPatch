<#
.SYNOPSIS
  Install the IDA bridge as a Windows service with NSSM, and publish it on your tailnet with `tailscale serve`.

.EXAMPLE
  # From an elevated PowerShell in the idabridge folder:
  .\scripts\install-service.ps1 -IdaDir "C:\IDA 5.5" -InputDir "D:\re\binaries"

  Run it again to change settings: it removes and re-creates the service (the token is kept).
#>
param(
  [Parameter(Mandatory)] [string] $IdaDir,          # folder with idaw.exe
  [Parameter(Mandatory)] [string] $InputDir,        # becomes IDA_INPUT_DIR
  [string] $DataDir = "$env:ProgramData\idabridge", # IDABRIDGE_HOME: databases, jobs, logs, token
  [int]    $Port = 8765,
  [int]    $MaxParallel = 8,
  [int]    $Timeout = 900,
  [string] $ServiceName = "IdaBridge",
  [string] $Nssm = "nssm.exe",
  [string] $Python = "py -3.12",
  [switch] $AllowIdc,                               # allow the run_idc tool (arbitrary IDC)
  [switch] $NoTailscaleServe
)
$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run this from an elevated (Administrator) PowerShell." }
if (-not (Test-Path (Join-Path $IdaDir "idaw.exe"))) { throw "idaw.exe not found in $IdaDir" }
if (-not (Test-Path $InputDir)) { throw "Input folder $InputDir not found" }
if (-not (Get-Command $Nssm -ErrorAction SilentlyContinue)) { throw "nssm.exe not found: install it (winget install NSSM.NSSM) or pass -Nssm <path>" }

$Project = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Project ".venv"
New-Item -ItemType Directory -Force -Path $DataDir, "$DataDir\logs" | Out-Null

# 1. Python environment
if (-not (Test-Path "$Venv\Scripts\python.exe")) {
  Write-Host "Creating $Venv"
  Invoke-Expression "$Python -m venv `"$Venv`""
}
& "$Venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& "$Venv\Scripts\python.exe" -m pip install -e $Project
if ($LASTEXITCODE) { throw "pip install failed" }

# 2. Token (kept across reinstalls). Readable by Administrators and the service account only.
$TokenFile = Join-Path $DataDir "token.txt"
if (-not (Test-Path $TokenFile)) {
  $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_") | Set-Content -NoNewline $TokenFile
}
$Token = (Get-Content $TokenFile -Raw).Trim()

# 3. The account the service runs as. IDA keeps its licence and settings per user, so use the account
#    you normally run IDA with (or a dedicated one where you have started IDA once by hand).
Write-Host "The service needs a Windows account that can run IDA (not LocalSystem)."
$Cred = Get-Credential -Message "Account for the $ServiceName service, e.g. .\you or MACHINE\you"

# 4. NSSM service
& $Nssm stop $ServiceName 2>$null | Out-Null
& $Nssm remove $ServiceName confirm 2>$null | Out-Null
& $Nssm install $ServiceName "$Venv\Scripts\python.exe" "-m idabridge"
$env_extra = @(
  "IDA_DIR=$IdaDir", "IDA_INPUT_DIR=$InputDir", "IDABRIDGE_HOME=$DataDir", "IDABRIDGE_TOKEN=$Token",
  "IDABRIDGE_HOST=127.0.0.1", "IDABRIDGE_PORT=$Port", "IDABRIDGE_MAX_PARALLEL=$MaxParallel",
  "IDABRIDGE_TIMEOUT=$Timeout", "IDABRIDGE_ALLOW_IDC=$([int][bool]$AllowIdc)", "PYTHONUNBUFFERED=1")
$settings = [ordered]@{
  DisplayName          = "IDA Pro 5.5 bridge (REST + MCP)"
  Description          = "Runs idaw.exe for agents on the tailnet. http://127.0.0.1:$Port"
  AppDirectory         = $Project
  AppStdout            = "$DataDir\logs\service.log"
  AppStderr            = "$DataDir\logs\service.log"
  AppRotateFiles       = 1
  AppRotateOnline      = 1
  AppRotateBytes       = 10485760
  AppExit              = "Default Restart"
  AppRestartDelay      = 5000
  AppStopMethodConsole = 15000   # Ctrl+C first, so running IDA processes are stopped cleanly
  AppKillProcessTree   = 1
  Start                = "SERVICE_DELAYED_AUTO_START"
}
foreach ($k in $settings.Keys) { & $Nssm set $ServiceName $k $settings[$k] | Out-Null }
& $Nssm set $ServiceName AppEnvironmentExtra @env_extra | Out-Null
& $Nssm set $ServiceName ObjectName $Cred.UserName $Cred.GetNetworkCredential().Password | Out-Null
if (Get-Service Tailscale -ErrorAction SilentlyContinue) { & $Nssm set $ServiceName DependOnService Tailscale | Out-Null }

# The service account must be able to write the data folder.
icacls $DataDir /grant "$($Cred.UserName):(OI)(CI)M" | Out-Null
icacls $TokenFile /inheritance:r /grant "Administrators:F" "$($Cred.UserName):R" | Out-Null

& $Nssm start $ServiceName
Start-Sleep -Seconds 3
try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/health"; Write-Host "Health: $($h | ConvertTo-Json -Compress)" }
catch { Write-Warning "Service did not answer yet; see $DataDir\logs\service.log" }

# 5. Tailnet HTTPS: https://<this-machine>.<tailnet>.ts.net -> 127.0.0.1:$Port
if (-not $NoTailscaleServe) {
  if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    tailscale serve --bg $Port
    tailscale serve status
  } else { Write-Warning "tailscale not on PATH: run 'tailscale serve --bg $Port' yourself." }
}

Write-Host ""
Write-Host "Token (also in $TokenFile):"
Write-Host "  $Token"
Write-Host "Next: idactl selftest <a 32-bit exe under $InputDir>   (see README: Check it on real IDA)"
