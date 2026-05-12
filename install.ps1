#Requires -Version 5.1
<#
.SYNOPSIS
    Remy-CC one-line installer for Windows.
.DESCRIPTION
    Usage:
      irm https://raw.githubusercontent.com/patchescamerababy/Remy-CC/main/install.ps1 | iex
      .\install.ps1 -Update
      .\install.ps1 -Uninstall
#>
param(
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$RepoUrl = if ($env:REMY_REPO_URL) { $env:REMY_REPO_URL } else { 'https://github.com/patchescamerababy/Remy-CC.git' }
$Branch = if ($env:REMY_BRANCH) { $env:REMY_BRANCH } else { 'main' }

function Show-Usage {
    Write-Host @"
Usage: install.ps1 [-Update] [-Uninstall] [-Help]

Options:
  (none)        Install Remy-CC
  -Update       Reinstall latest version
  -Uninstall    Remove Remy-CC
  -Help         Show this message
"@
    exit 0
}

function Find-Python {
    $candidates = @('python3', 'python', 'py')
    foreach ($cmd in $candidates) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            $ver = & $cmd -c "import sys; print(sys.version_info >= (3,7))" 2>$null
            if ($ver -eq 'True') { return $cmd }
        }
    }
    throw 'Python 3.7+ is required but not found. Install Python and retry.'
}

function Assert-Git {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'git is required but not found. Install git and retry.'
    }
}

function Invoke-Installer {
    param([string]$Mode)

    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "remy-cc-$(Get-Random)"
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

    try {
        Write-Host "[*] Cloning Remy-CC ($Branch)..."
        git clone --depth 1 --branch $Branch $RepoUrl "$tmpDir\remy-cc" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Failed to clone repository. Check network and URL: $RepoUrl" }

        Write-Host '[*] Running installer...'
        $installerPath = Join-Path $tmpDir 'remy-cc\install.py'
        switch ($Mode) {
            'install'   { & $python $installerPath }
            'update'    { & $python $installerPath }
            'uninstall' { & $python $installerPath --uninstall }
        }
        if ($LASTEXITCODE -ne 0) { throw 'Installer exited with error.' }

        Write-Host '[*] Cleanup complete.'
    }
    finally {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}

if ($Help) { Show-Usage }

Assert-Git
$python = Find-Python

$mode = 'install'
if ($Update) { $mode = 'update' }
if ($Uninstall) { $mode = 'uninstall' }

Invoke-Installer -Mode $mode
