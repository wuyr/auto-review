[CmdletBinding()]
param(
    [ValidateSet("symlink", "copy")]
    [string]$Mode = "copy",

    [string]$TargetRoot = $(if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }),

    [switch]$Force = $true
)

$ErrorActionPreference = "Stop"

function Test-Python3Command {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [string[]]$PrefixArgs = @()
    )

    $Executable = Get-Command $Command -ErrorAction SilentlyContinue
    if ($null -eq $Executable) {
        return $false
    }

    try {
        & $Command @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-Python3Command {
    if (-not [string]::IsNullOrWhiteSpace($env:PYTHON)) {
        if (Test-Python3Command -Command $env:PYTHON) {
            return @{
                Command = $env:PYTHON
                Args = @()
            }
        }

        throw "PYTHON is set to '$env:PYTHON', but it is not an executable Python 3.10+ command."
    }

    $Candidates = @(
        @{ Command = "python3"; Args = @() },
        @{ Command = "python"; Args = @() },
        @{ Command = "py"; Args = @("-3") }
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Python3Command -Command $Candidate.Command -PrefixArgs $Candidate.Args) {
            return $Candidate
        }
    }

    throw "Python 3.10 or newer is required. Install Python 3.10+ and ensure 'python3', 'python', or 'py -3' works."
}

$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$Python = Get-Python3Command
$Helper = Join-Path $PSScriptRoot "scripts/auto_review_installer.py"
$Arguments = @()
$Arguments += $Python.Args
$Arguments += @(
    $Helper,
    "install",
    "--project-root",
    $PSScriptRoot,
    "--target-root",
    $TargetRoot,
    "--codex-home",
    $CodexHome,
    "--mode",
    $Mode
)

if ($Force) {
    $Arguments += "--force"
}

& $Python.Command @Arguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
