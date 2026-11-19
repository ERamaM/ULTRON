<#
.SYNOPSIS
Zero-Python bootstrap installer for ULTRON on Windows.

.EXAMPLE
irm https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.ps1 | iex

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\install.ps1 --project D:\my-project
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArgs
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Add-ToolBinsToPath {
    $candidates = @(
        (Join-Path $HOME '.local\bin'),
        (Join-Path $HOME '.cargo\bin')
    )
    foreach ($dir in $candidates) {
        if ((Test-Path -LiteralPath $dir -PathType Container) -and (($env:PATH -split ';') -notcontains $dir)) {
            $env:PATH = "$dir;$env:PATH"
        }
    }
}

function Test-UltronSourceDir([string]$Dir) {
    if ([string]::IsNullOrWhiteSpace($Dir)) { return $false }
    return (Test-Path -LiteralPath (Join-Path $Dir 'install.py') -PathType Leaf) -and
           (Test-Path -LiteralPath (Join-Path $Dir 'skill\ultron\SKILL.md') -PathType Leaf)
}

function Find-UvExecutable {
    return Get-Command uv -All -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandType -eq 'Application' -and $_.Source -notlike '*\WindowsApps\*' } |
        Select-Object -First 1
}

function Find-WorkingPython {
    $probes = @(
        @{ Name = 'py';      Args = @('-3.13') },
        @{ Name = 'py';      Args = @('-3.12') },
        @{ Name = 'py';      Args = @('-3') },
        @{ Name = 'python';  Args = @() },
        @{ Name = 'python3'; Args = @() }
    )
    foreach ($probe in $probes) {
        $cmd = Get-Command $probe.Name -All -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandType -eq 'Application' -and $_.Source -notlike '*\WindowsApps\*' } |
            Select-Object -First 1
        if (-not $cmd) { continue }
        $checkCode = 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
        try {
            & $cmd.Source @($probe.Args) -c $checkCode *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{
                    Executable = $cmd.Source
                    ExtraArgs  = $probe.Args
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Ensure-Tls12 {
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        # Older/newer .NET handles TLS defaults transparently.
    }
}

function Install-StandaloneUv {
    [Console]::Error.WriteLine('[ultron] Python 3.12+ with venv not found; bootstrapping standalone uv + Python 3.12...')
    Ensure-Tls12
    $uvScript = Invoke-RestMethod -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1'
    $null = & { Invoke-Expression $uvScript } 1>$null 6>$null
    Add-ToolBinsToPath
    $uv = Find-UvExecutable
    if (-not $uv) {
        throw "Standalone uv installation completed, but 'uv' was not found on PATH."
    }
    return $uv
}

Add-ToolBinsToPath

$repoUrl = if ($env:ULTRON_REPO) { $env:ULTRON_REPO.TrimEnd('/') } else { 'https://github.com/ERamaM/ULTRON' }
$ref = if ($env:ULTRON_REF) { $env:ULTRON_REF } else { 'main' }

$scriptDir = if ($PSCommandPath) {
    Split-Path -Parent $PSCommandPath
} elseif ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    ''
}

$sourceDir = $null
if (Test-UltronSourceDir $scriptDir) {
    $sourceDir = $scriptDir
} elseif (Test-UltronSourceDir (Get-Location).Path) {
    $sourceDir = (Get-Location).Path
}

$tempDir = $null
try {
    if (-not $sourceDir) {
        Ensure-Tls12
        $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ultron-bootstrap-" + [System.Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
        $zipPath = Join-Path $tempDir 'ultron.zip'
        $extractDir = Join-Path $tempDir 'src'
        $zipUrl = "$repoUrl/archive/refs/heads/$ref.zip"
        [Console]::Error.WriteLine("[ultron] Downloading ULTRON ($ref) from $repoUrl...")
        Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zipPath
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
        $candidates = Get-ChildItem -LiteralPath $extractDir -Directory
        foreach ($dir in $candidates) {
            if (Test-UltronSourceDir $dir.FullName) {
                $sourceDir = $dir.FullName
                break
            }
        }
        if (-not $sourceDir) {
            throw 'Downloaded archive did not contain install.py and skill\ultron\SKILL.md.'
        }
    }

    $installPy = Join-Path $sourceDir 'install.py'
    $argsToPass = if ($InstallerArgs) { $InstallerArgs } else { @() }

    $uvCmd = Find-UvExecutable
    if ($uvCmd) {
        & $uvCmd.Source run --python '>=3.12' --no-project $installPy @argsToPass
        exit $LASTEXITCODE
    }

    $py = Find-WorkingPython
    if ($py) {
        & $py.Executable @($py.ExtraArgs) $installPy @argsToPass
        exit $LASTEXITCODE
    }

    $uvCmd = Install-StandaloneUv
    & $uvCmd.Source run --python '3.12' --no-project $installPy @argsToPass
    exit $LASTEXITCODE
}
finally {
    if ($tempDir -and (Test-Path -LiteralPath $tempDir)) {
        Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
