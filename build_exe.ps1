param(
    [string]$VersionTag,
    [switch]$Windowed
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Could not find virtual environment Python at $venvPython"
}

$appFilePath = Join-Path $projectRoot "app.py"
$logoFilePath = Join-Path $projectRoot "logo.png"
$launcherFilePath = Join-Path $projectRoot "launcher.py"

if (-not (Test-Path $appFilePath)) {
    throw "Could not find app file at $appFilePath"
}
if (-not (Test-Path $launcherFilePath)) {
    throw "Could not find launcher file at $launcherFilePath"
}

if ([string]::IsNullOrWhiteSpace($VersionTag)) {
    $VersionTag = Get-Date -Format "yyyyMMdd-HHmm"
}

$safeVersionTag = ($VersionTag -replace "[^0-9A-Za-z._-]", "-")
$buildName = "LibertySmokes-$safeVersionTag"

Write-Host "Using Python: $venvPython"
& $venvPython -m pip install --upgrade pip pyinstaller

$distPath = Join-Path $projectRoot "dist"
$buildPath = Join-Path $projectRoot "build"
if (Test-Path $distPath) {
    Remove-Item $distPath -Recurse -Force
}
if (Test-Path $buildPath) {
    Remove-Item $buildPath -Recurse -Force
}

$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--specpath", "build",
    "--name", $buildName,
    "--collect-all", "streamlit",
    "--hidden-import", "streamlit.web.cli",
    "--collect-all", "postgrest",
    "--hidden-import", "postgrest",
    "--add-data", "$appFilePath;.",
    "--add-data", "$logoFilePath;.",
    $launcherFilePath
)

if ($Windowed) {
    $pyInstallerArgs += "--windowed"
}

Write-Host "Building EXE..."
& $venvPython @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "Build complete."
Write-Host "Run: .\dist\$buildName\$buildName.exe"
