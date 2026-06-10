param(
    [string]$TargetExePath,
    [string]$ShortcutName = "Liberty Smokes",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if ([string]::IsNullOrWhiteSpace($TargetExePath)) {
    $latestExe = Get-ChildItem -Path (Join-Path $projectRoot "dist") -Filter "LibertySmokes-*.exe" -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if (-not $latestExe) {
        throw "No versioned EXE found under .\\dist. Build first with .\\build_exe.ps1"
    }

    $TargetExePath = $latestExe.FullName
}

$resolvedExePath = (Resolve-Path $TargetExePath).Path
if (-not (Test-Path $resolvedExePath)) {
    throw "Target EXE not found: $TargetExePath"
}

$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath ($ShortcutName + ".lnk")

if ((Test-Path $shortcutPath) -and (-not $Force)) {
    throw "Shortcut already exists at $shortcutPath. Re-run with -Force to overwrite."
}

$wshShell = New-Object -ComObject WScript.Shell
$shortcut = $wshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $resolvedExePath
$shortcut.WorkingDirectory = Split-Path -Parent $resolvedExePath
$shortcut.IconLocation = $resolvedExePath
$shortcut.Description = "Launch Liberty Smokes"
$shortcut.Save()

Write-Host "Created shortcut: $shortcutPath"
Write-Host "Target: $resolvedExePath"
