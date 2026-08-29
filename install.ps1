#Requires -Version 5.1
<#
.SYNOPSIS
    Remy-CC one-line installer for Windows: downloads the remy-cc release
    binary, verifies its sha256 checksum, and hands over to `remy-cc install`
    (idempotent; a rerun installs the latest release, `remy-cc update`
    self-updates).
#>
param(
    [switch]$Uninstall,
    [switch]$NonInteractive,
    [switch]$PurgeState,
    [ValidateSet('en', 'zh-CN')][string]$Lang,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$Repo = if ($env:REMY_CC_REPO) { $env:REMY_CC_REPO } else { 'MyPeacefulValentine/Remy-CC' }
$Tag = $env:REMY_CC_TAG
$Target = 'x86_64-pc-windows-msvc'

function Show-Usage {
    Write-Host @"
Usage: install.ps1 [-Uninstall] [-Lang en|zh-CN] [-NonInteractive] [-PurgeState] [-Help]

Options:
  (none)           Install Remy-CC
  -Uninstall       Remove Remy-CC via the installed binary
  -Lang en|zh-CN   Interface language for the deployed artifacts
  -NonInteractive  Skip prompts
  -PurgeState      With -Uninstall, remove user-level engine state
  -Help            Show this message

Environment:
  REMY_CC_REPO     GitHub repository slug (default: MyPeacefulValentine/Remy-CC)
  REMY_CC_TAG      Pin a release tag (default: latest release)
"@
    exit 0
}

if ($Help) { Show-Usage }

try {
    if ($PurgeState -and -not $Uninstall) { throw '-PurgeState requires -Uninstall' }

    if ($Uninstall) {
        $remyHome = if ($env:REMY_CC_HOME) { $env:REMY_CC_HOME } else { Join-Path $env:USERPROFILE '.remy-cc' }
        $bin = Join-Path $remyHome 'bin\remy-cc.exe'
        if (-not (Test-Path $bin)) { throw "remy-cc binary not found at ${bin}; nothing to uninstall" }
        $cliArgs = @('uninstall')
        if ($PurgeState) { $cliArgs += '--purge-state' }
        if ($NonInteractive) { $cliArgs += '--yes' }
        & $bin @cliArgs
        exit $LASTEXITCODE
    }

    if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
        throw 'tar.exe is required but not found (it ships with Windows 10 1803+).'
    }

    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "remy-cc-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $tmpDir | Out-Null
    try {
        if (-not $Tag) {
            $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" `
                -Headers @{ 'User-Agent' = 'remy-cc-bootstrap' } -UseBasicParsing
            $Tag = $release.tag_name
            if (-not $Tag) { throw 'cannot parse tag_name from the release metadata' }
        }
        $asset = "remy-cc-$Tag-$Target.zip"
        $baseUrl = "https://github.com/$Repo/releases/download/$Tag"

        Write-Host "[*] Downloading $asset ..."
        Invoke-WebRequest -Uri "$baseUrl/$asset" -OutFile (Join-Path $tmpDir $asset) -UseBasicParsing
        Invoke-WebRequest -Uri "$baseUrl/$asset.sha256" -OutFile (Join-Path $tmpDir "$asset.sha256") -UseBasicParsing

        $expected = ((Get-Content (Join-Path $tmpDir "$asset.sha256") -Raw).Trim() -split '\s+')[0].ToLower()
        $actual = (Get-FileHash (Join-Path $tmpDir $asset) -Algorithm SHA256).Hash.ToLower()
        if (-not $expected -or $expected -ne $actual) {
            throw "sha256 mismatch for ${asset}: expected '$expected', got '$actual'"
        }
        Write-Host '[*] Checksum verified.'

        tar -xf (Join-Path $tmpDir $asset) -C $tmpDir
        if ($LASTEXITCODE -ne 0) { throw "archive extraction failed: $asset" }
        $bin = Join-Path $tmpDir 'remy-cc.exe'
        if (-not (Test-Path $bin)) { throw 'archive did not contain remy-cc.exe' }

        $cliArgs = @('install')
        if ($Lang) { $cliArgs += @('--lang', $Lang) }
        if ($NonInteractive) { $cliArgs += '--non-interactive' }
        Write-Host '[*] Running remy-cc install ...'
        & $bin @cliArgs
        exit $LASTEXITCODE
    }
    finally {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
