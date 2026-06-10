param(
    [string]$VersionTag
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildScript = Join-Path $scriptRoot "build_exe.ps1"

if (-not (Test-Path $buildScript)) {
    throw "Could not find build script at $buildScript"
}

& $buildScript -VersionTag $VersionTag -Windowed
