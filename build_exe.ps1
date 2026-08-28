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
$iconFilePath = Join-Path $projectRoot "logo.ico"
$launcherFilePath = Join-Path $projectRoot "launcher.py"

if (-not (Test-Path $appFilePath)) {
    throw "Could not find app file at $appFilePath"
}
if (-not (Test-Path $logoFilePath)) {
    throw "Could not find logo file at $logoFilePath"
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

if (-not (Test-Path $iconFilePath)) {
    Write-Host "Creating icon file: $iconFilePath"
    $iconGenCode = "from PIL import Image; img=Image.open(r'''$logoFilePath''').convert('RGBA'); img.save(r'''$iconFilePath''', format='ICO', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])"
    & $venvPython -c $iconGenCode
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create icon file from $logoFilePath"
    }
}

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
    "--icon", $iconFilePath,
    "--collect-all", "streamlit",
    "--hidden-import", "streamlit.web.cli",
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
