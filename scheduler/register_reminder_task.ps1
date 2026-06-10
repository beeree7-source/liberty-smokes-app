param(
    [string]$TaskName = "LibertySmokes-ReminderEmails",
    [string]$StartTime = "08:00",
    [switch]$RunAsSystem,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

if ($StartTime -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
    throw "StartTime must be in HH:mm 24-hour format, for example 08:00 or 09:30."
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $RepoRoot "scheduler\run_email_reminders.py"

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found at $PythonExe. Activate/create .venv first."
}
if (-not (Test-Path $Runner)) {
    throw "Runner script not found at $Runner."
}

$taskExists = $false
try {
    $null = schtasks.exe /Query /TN $TaskName 2>$null
    if ($LASTEXITCODE -eq 0) {
        $taskExists = $true
    }
} catch {
    $taskExists = $false
}

if ($taskExists -and -not $Force) {
    throw "Task '$TaskName' already exists. Re-run with -Force to replace it."
}

if ($taskExists -and $Force) {
    schtasks.exe /Delete /TN $TaskName /F | Out-Null
}

$command = '"' + $PythonExe + '" "' + $Runner + '"'

schtasks.exe /Create `
    /TN $TaskName `
    /TR $command `
    /SC DAILY `
    /ST $StartTime `
    /RU $(if ($RunAsSystem) { "SYSTEM" } else { $env:USERNAME }) `
    /F | Out-Null

if ($LASTEXITCODE -ne 0) {
    throw "Failed to create scheduled task."
}

if ($RunAsSystem) {
    Write-Host "Created task '$TaskName' to run daily at $StartTime as SYSTEM."
} else {
    Write-Host "Created task '$TaskName' to run daily at $StartTime as $env:USERNAME."
}
Write-Host "Command: $command"
Write-Host "To run once now: schtasks /Run /TN '$TaskName'"
Write-Host "To remove later: schtasks /Delete /TN '$TaskName' /F"
