# Builds the release folder and zip from a Release build: what .github/workflows/taskdeck-desktop-release.yml
# publishes, and what to run locally to try a release (src/updater.h describes the layout and the updates).
#
#   pwsh tools/package.ps1 [-Build 42]        (from taskdeck-desktop/, after cmake --build build --config Release)
#
# Writes dist/TaskDeck/ (the folder a user unzips), dist/TaskDeck-windows-x64.zip and
# dist/taskdeck-desktop-manifest.json. Needs a Python with pip (to install the jobs' packages) and the internet
# (Python's embeddable package from python.org, checked against its SHA-256 below).
param(
    [int]$Build = 0,
    [string]$CMakeBuild = "build",
    [string]$Out = "dist",
    [string]$Python = "python",
    [string]$PythonVersion = "3.14.10",
    [string]$PythonSha256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot  # taskdeck-desktop
Set-Location $root
$jobsSrc = Join-Path $root "..\taskdeck\src\taskdeck"
$deps = Join-Path $CMakeBuild "_deps"
$stage = Join-Path $Out "TaskDeck"

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null

# The app and 7-Zip's library.
Copy-Item (Join-Path $CMakeBuild "Release\TaskDeck.exe"), (Join-Path $CMakeBuild "Release\7z.dll") $stage

# The job host and the phone app's jobs (the taskdeck package, as the phone's live update ships it).
New-Item -ItemType Directory -Force (Join-Path $stage "python") | Out-Null
Copy-Item python\taskdeck_host.py (Join-Path $stage "python")
robocopy $jobsSrc (Join-Path $stage "jobs\taskdeck") /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying the jobs failed (robocopy $LASTEXITCODE)" }
$global:LASTEXITCODE = 0

# Python: the embeddable package (python.org's, for apps that bring their own Python), plus the jobs' packages.
$zip = Join-Path $Out "python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $zip
}
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($sha -ne $PythonSha256) { Remove-Item $zip; throw "Python's package has SHA-256 $sha, expected $PythonSha256" }
$runtime = Join-Path $stage "python-runtime"
Expand-Archive $zip $runtime
$short = ($PythonVersion -split '\.')[0..1] -join ''           # 314
$pth = Get-ChildItem $runtime -Filter "python$short._pth" | Select-Object -First 1
Set-Content $pth.FullName "python$short.zip`n.`nLib\site-packages`n" -NoNewline
& $Python -m pip install --disable-pip-version-check --quiet --no-compile --target (Join-Path $runtime "Lib\site-packages") `
    --only-binary=:all: --platform win_amd64 --implementation cp --python-version (($PythonVersion -split '\.')[0..1] -join '.') `
    -r python\requirements-release.txt
if ($LASTEXITCODE -ne 0) { throw "pip failed" }

# Licences of what ships (7-Zip's LGPL with the unRAR restriction, Python's, Dear ImGui's, nlohmann/json's;
# rsa's and pyasn1's are in their .dist-info folders).
$lic = Join-Path $stage "LICENSES"
New-Item -ItemType Directory -Force $lic | Out-Null
Copy-Item (Join-Path $deps "sevenzip_src-src\DOC\License.txt") (Join-Path $lic "7-Zip.txt")
Copy-Item (Join-Path $deps "sevenzip_src-src\DOC\unRarLicense.txt") (Join-Path $lic "7-Zip-unRAR.txt")
Copy-Item (Join-Path $runtime "LICENSE.txt") (Join-Path $lic "Python.txt")
Copy-Item (Join-Path $deps "imgui-src\LICENSE.txt") (Join-Path $lic "Dear-ImGui.txt")
Copy-Item (Join-Path $deps "json-src\LICENSE.MIT") (Join-Path $lic "nlohmann-json.txt")
@"
TaskDeck Desktop includes:

* 7-Zip 26.05 (7z.dll), by Igor Pavlov: GNU LGPL, with the unRAR restriction (the RAR code may not be used
  to create RAR archives). 7z.dll is built unchanged from 7-Zip's source: https://github.com/ip7z/7zip
  (tag 26.05) and https://www.7-zip.org. 7-Zip.txt, 7-Zip-unRAR.txt.
* Python $PythonVersion (python-runtime), the Python Software Foundation's embeddable package. Python.txt.
* Dear ImGui (MIT), Dear-ImGui.txt; nlohmann/json (MIT), nlohmann-json.txt.
* rsa and pyasn1 (python-runtime\Lib\site-packages, their licences in the .dist-info folders).
"@ | Set-Content (Join-Path $lic "README.txt")

@"
TaskDeck for Windows, version 0.1.$Build

Run TaskDeck.exe. Keep this folder together: the jobs (jobs\), their host (python\) and Python itself
(python-runtime\) are used from here. Unzip it into a folder you can write to (not Program Files): TaskDeck
updates itself here when a new version is out (Settings > App updates).

Your pins, history and settings are in %LOCALAPPDATA%\TaskDeck, keys and passwords in Windows Credential
Manager; neither is in this folder.

Source: https://github.com/jbalcomb/ScratchPatch/tree/main/taskdeck-desktop
"@ | Set-Content (Join-Path $stage "README.txt")

# Check: the bundled Python runs the job host, every job loads, and the jobs' packages import.
$ready = "" | & (Join-Path $runtime "python.exe") -I -u -X utf8 (Join-Path $stage "python\taskdeck_host.py") --src (Join-Path $stage "jobs") |
    Select-Object -First 1 | ConvertFrom-Json
if ($ready.event -ne "ready") { throw "the job host didn't start: $($ready | ConvertTo-Json -Compress)" }
$errors = @($ready.errors.PSObject.Properties)
if ($errors.Count -gt 0) { throw "jobs that don't load: $($ready.errors | ConvertTo-Json -Compress)" }
& (Join-Path $runtime "python.exe") -I -c "import rsa, ssl, sqlite3, ctypes; print('packages ok')"
if ($LASTEXITCODE -ne 0) { throw "the jobs' packages don't import" }
Write-Host "Job host: Python $($ready.python), $(@($ready.jobs).Count) jobs"

# The zip and its manifest (the zip first in the release, the manifest last: see the workflow).
$file = "TaskDeck-windows-x64.zip"
$zipOut = Join-Path $Out $file
if (Test-Path $zipOut) { Remove-Item $zipOut }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipOut -CompressionLevel Optimal
$commit = (git rev-parse --short=7 HEAD)
[ordered]@{
    build   = $Build
    version = "0.1.$Build"
    commit  = "$commit"
    built   = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    file    = $file
    sha256  = (Get-FileHash $zipOut -Algorithm SHA256).Hash.ToLower()
    size    = (Get-Item $zipOut).Length
} | ConvertTo-Json | Set-Content (Join-Path $Out "taskdeck-desktop-manifest.json")
Get-Content (Join-Path $Out "taskdeck-desktop-manifest.json")
