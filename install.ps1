#Requires -Version 5.1
<#
.SYNOPSIS
    Remy-CC one-line installer for Windows.
#>
param(
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$NonInteractive,
    [switch]$Json,
    [switch]$PurgeState,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$RepoUrl = if ($env:REMY_REPO_URL) { $env:REMY_REPO_URL } else { 'https://github.com/MyPeacefulValentine/Remy-CC.git' }
$Branch = if ($env:REMY_BRANCH) { $env:REMY_BRANCH } else { 'main' }
if ($Json) { $NonInteractive = $true }

function Show-Usage {
    Write-Host @"
Usage: install.ps1 [-Update] [-Uninstall] [-NonInteractive] [-Json] [-PurgeState] [-Help]

Options:
  (none)           Install Remy-CC
  -Update          Reinstall latest version
  -Uninstall       Remove Remy-CC
  -NonInteractive  Disable installer prompts
  -Json            Emit one JSON result object; implies -NonInteractive
  -PurgeState      With -Uninstall, remove user-level engine state
  -Help            Show this message
"@
    exit 0
}

function Write-InstallerLog {
    param([string]$Message)
    if ($Json) {
        [Console]::Error.WriteLine($Message)
    } else {
        Write-Host $Message
    }
}

function Find-Python {
    $candidates = @('python3', 'python', 'py')
    foreach ($cmd in $candidates) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            $ver = & $cmd -c "import sys; print(sys.version_info >= (3,10))" 2>$null
            if ($ver -eq 'True') { return $cmd }
        }
    }
    throw 'Python 3.10+ is required but not found. Install Python and retry.'
}

function Assert-Git {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'git is required but not found. Install git and retry.'
    }
}

function Invoke-Installer {
    param([string]$Mode)

    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "remy-cc-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $tmpDir | Out-Null
    $installerExit = 1

    try {
        Write-InstallerLog "[*] Cloning Remy-CC ($Branch)..."
        git clone --depth 1 --branch $Branch $RepoUrl "$tmpDir\remy-cc" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Failed to clone repository. Check network and URL: $RepoUrl" }

        $installerPath = Join-Path $tmpDir 'remy-cc\install.py'
        $installerArgs = @($installerPath)
        if ($Mode -eq 'uninstall') { $installerArgs += '--uninstall' }
        if ($NonInteractive) { $installerArgs += '--non-interactive' }
        if ($Json) { $installerArgs += '--json' }
        if ($PurgeState) { $installerArgs += '--purge-state' }

        Write-InstallerLog '[*] Running installer...'
        & $python @installerArgs
        $installerExit = $LASTEXITCODE
        Write-InstallerLog '[*] Cleanup complete.'
    }
    finally {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
    return $installerExit
}

if ($Help) { Show-Usage }

try {
    if ($PurgeState -and -not $Uninstall) { throw '-PurgeState requires -Uninstall' }
    Assert-Git
    $python = Find-Python
    $mode = if ($Uninstall) { 'uninstall' } elseif ($Update) { 'update' } else { 'install' }
    $exitCode = Invoke-Installer -Mode $mode
    exit $exitCode
}
catch {
    if ($Json) {
        [ordered]@{
            schema_version = 1
            operation = if ($Uninstall) { 'uninstall' } elseif ($Update) { 'update' } else { 'install' }
            status = 'preflight_rejected'
            exit_code = 1
            hook_mode = $null
            changed = @()
            warnings = @('installer entry preflight failed')
            recovery = $null
        } | ConvertTo-Json -Compress
    } else {
        [Console]::Error.WriteLine($_.Exception.Message)
    }
    exit 1
}
