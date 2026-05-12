[CmdletBinding()]
param(
    [ValidateSet("symlink", "copy")]
    [string]$Mode = "copy",

    [string]$TargetRoot = $(if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }),

    [switch]$Force = $true
)

$ErrorActionPreference = "Stop"

$PluginName = "auto-review"
$ProjectRoot = $PSScriptRoot
$PluginSource = Join-Path $ProjectRoot "plugins/$PluginName"
$SkillSource = Join-Path $ProjectRoot "skills/$PluginName"
$PluginRoot = Join-Path $TargetRoot "plugins"
$PluginDest = Join-Path $PluginRoot $PluginName
$MarketplaceDir = Join-Path $TargetRoot ".agents/plugins"
$MarketplacePath = Join-Path $MarketplaceDir "marketplace.json"
$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$CodexHooksPath = Join-Path $CodexHome "hooks.json"
$SkillRoot = Join-Path $CodexHome "skills"
$SkillDest = Join-Path $SkillRoot $PluginName
$Script:HookPythonCommand = $null
$Script:HookPythonArgs = @()

function New-DefaultMarketplacePayload {
    [pscustomobject]@{
        name = "local-plugins"
        interface = [pscustomobject]@{ displayName = "Local Plugins" }
        plugins = @()
    }
}

function Quote-CmdArgument([string]$Value) {
    '"' + $Value.Replace('"', '\"') + '"'
}

function Test-PathExists([string]$Path) {
    $null -ne (Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue)
}

function Get-NormalizedPath([string]$Path) {
    try {
        return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    } catch {
        return [System.IO.Path]::GetFullPath($Path)
    }
}

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

function Get-HookPythonCommand {
    if (-not [string]::IsNullOrWhiteSpace($env:PYTHON)) {
        if (Test-Python3Command -Command $env:PYTHON) {
            $Script:HookPythonCommand = $env:PYTHON
            $Script:HookPythonArgs = @()
            return (Quote-CmdArgument $env:PYTHON)
        }

        throw "PYTHON is set to '$env:PYTHON', but it is not an executable Python 3.10+ command."
    }

    $Candidates = @(
        @{ Command = "python3"; PrefixArgs = @(); HookPrefix = "python3" },
        @{ Command = "python"; PrefixArgs = @(); HookPrefix = "python" },
        @{ Command = "py"; PrefixArgs = @("-3"); HookPrefix = "py -3" }
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Python3Command -Command $Candidate.Command -PrefixArgs $Candidate.PrefixArgs) {
            $Script:HookPythonCommand = $Candidate.Command
            $Script:HookPythonArgs = $Candidate.PrefixArgs
            return $Candidate.HookPrefix
        }
    }

    throw "Python 3.10 or newer is required. Install Python 3.10+ and ensure 'python3', 'python', or 'py -3' works."
}

function Test-CodexCli {
    $Codex = Get-Command "codex" -ErrorAction SilentlyContinue
    if ($null -eq $Codex) {
        throw "Codex CLI is required, but 'codex' was not found in PATH. Install Codex CLI or add it to PATH, then re-run this installer."
    }

    try {
        & codex --version *> $null
    } catch {
        throw "Codex CLI was found, but 'codex --version' failed: $($_.Exception.Message)"
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Codex CLI was found, but 'codex --version' exited with code $LASTEXITCODE."
    }
}

function Test-SourceLayout {
    $RequiredFiles = @(
        Join-Path $PluginSource ".codex-plugin/plugin.json",
        Join-Path $PluginSource "hooks/auto_review_hook.py",
        Join-Path $PluginSource "hooks/hooks.json",
        Join-Path $SkillSource "SKILL.md"
    )

    foreach ($RequiredFile in $RequiredFiles) {
        if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
            throw "Required plugin source file is missing: $RequiredFile"
        }
    }
}

function Test-JsonFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found: $Path"
    }

    try {
        $Raw = Get-Content -LiteralPath $Path -Raw
    } catch {
        throw "Cannot read $Label at $Path`: $($_.Exception.Message)"
    }

    if ([string]::IsNullOrWhiteSpace($Raw)) {
        throw "$Label is empty: $Path"
    }

    try {
        $null = $Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Invalid JSON in $Label at $Path`: $($_.Exception.Message)"
    }
}

function Test-MarketplaceJson {
    if (-not (Test-Path -LiteralPath $MarketplacePath -PathType Leaf)) {
        return
    }

    try {
        $Raw = Get-Content -LiteralPath $MarketplacePath -Raw
    } catch {
        throw "Cannot read existing marketplace.json at $MarketplacePath`: $($_.Exception.Message)"
    }

    if ([string]::IsNullOrWhiteSpace($Raw)) {
        throw "Existing marketplace.json is empty: $MarketplacePath"
    }

    try {
        $Payload = $Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Invalid JSON in existing marketplace.json at $MarketplacePath`: $($_.Exception.Message)"
    }

    if ($Payload -isnot [pscustomobject]) {
        throw "Existing marketplace.json must contain a JSON object: $MarketplacePath"
    }

    if ($null -ne $Payload.plugins -and $Payload.plugins -isnot [array]) {
        throw "Existing marketplace.json field 'plugins' must be an array: $MarketplacePath"
    }

    if ($null -ne $Payload.interface -and $Payload.interface -isnot [pscustomobject]) {
        throw "Existing marketplace.json field 'interface' must be an object: $MarketplacePath"
    }
}

function Test-CodexHooksJson {
    if (-not (Test-Path -LiteralPath $CodexHooksPath -PathType Leaf)) {
        return
    }

    try {
        $Raw = Get-Content -LiteralPath $CodexHooksPath -Raw
    } catch {
        throw "Cannot read existing hooks.json at $CodexHooksPath`: $($_.Exception.Message)"
    }

    if ([string]::IsNullOrWhiteSpace($Raw)) {
        throw "Existing hooks.json is empty: $CodexHooksPath"
    }

    try {
        $Payload = $Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Invalid JSON in existing hooks.json at $CodexHooksPath`: $($_.Exception.Message)"
    }

    if ($Payload -isnot [pscustomobject]) {
        throw "Existing hooks.json must contain a JSON object: $CodexHooksPath"
    }

    if ($null -ne $Payload.hooks -and $Payload.hooks -isnot [pscustomobject]) {
        throw "Existing hooks.json field 'hooks' must be an object: $CodexHooksPath"
    }
}

function Test-InstallTargets {
    $PluginSourcePath = Get-NormalizedPath $PluginSource
    $PluginDestPath = Get-NormalizedPath $PluginDest
    if ([string]::Equals($PluginSourcePath, $PluginDestPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Plugin install target resolves to the source directory: $PluginDestPath. Choose a different -TargetRoot."
    }

    $SkillSourcePath = Get-NormalizedPath $SkillSource
    $SkillDestPath = Get-NormalizedPath $SkillDest
    if ([string]::Equals($SkillSourcePath, $SkillDestPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Skill install target resolves to the source directory: $SkillDestPath. Set CODEX_HOME or choose a different -TargetRoot."
    }

    foreach ($ExistingPath in @($PluginDest, $SkillDest)) {
        if (Test-PathExists $ExistingPath) {
            if ($Force) {
                continue
            }

            throw "Install target exists: $ExistingPath. Re-run with -Force to replace it."
        }
    }
}

function Test-TargetRootWritable {
    if ((Test-PathExists $TargetRoot) -and -not (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
        throw "Install target root exists but is not a directory: $TargetRoot"
    }

    try {
        New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
    } catch {
        throw "Cannot create install target root '$TargetRoot': $($_.Exception.Message)"
    }

    $ProbeDir = Join-Path $TargetRoot ".auto-review-install-check-$([guid]::NewGuid().ToString('N'))"
    try {
        New-Item -ItemType Directory -Path $ProbeDir | Out-Null
    } catch {
        throw "Install target root is not writable: $TargetRoot. $($_.Exception.Message)"
    } finally {
        if (Test-PathExists $ProbeDir) {
            Remove-Item -LiteralPath $ProbeDir -Recurse -Force
        }
    }
}

function Test-SymlinkSupported {
    if ($Mode -ne "symlink") {
        return
    }

    $ProbeDir = Join-Path $TargetRoot ".auto-review-symlink-check-$([guid]::NewGuid().ToString('N'))"
    $ProbeTarget = Join-Path $ProbeDir "target.txt"
    $ProbeLink = Join-Path $ProbeDir "link.txt"

    try {
        New-Item -ItemType Directory -Path $ProbeDir | Out-Null
        Set-Content -LiteralPath $ProbeTarget -Value "probe" -NoNewline
        New-Item -ItemType SymbolicLink -Path $ProbeLink -Target $ProbeTarget -ErrorAction Stop | Out-Null
        if (-not (Test-PathExists $ProbeLink)) {
            throw "Symbolic link probe did not create a link."
        }
    } catch {
        throw "Symbolic links are not available under $TargetRoot. On Windows, enable Developer Mode or run PowerShell as Administrator, or use: .\install.ps1 -Mode copy. $($_.Exception.Message)"
    } finally {
        if (Test-PathExists $ProbeDir) {
            Remove-Item -LiteralPath $ProbeDir -Recurse -Force
        }
    }
}

function Write-HooksJson([string]$PluginPath, [string]$HookPython) {
    $HookScript = Join-Path $PluginPath "hooks/auto_review_hook.py"
    $HooksPath = Join-Path $PluginPath "hooks/hooks.json"
    $Command = "$HookPython $(Quote-CmdArgument $HookScript)"

    $Payload = [pscustomobject]@{
        description = "Auto Review hooks: opt-in UserPromptSubmit arming and Stop-driven review/fix loop"
        hooks = [pscustomobject]@{
            UserPromptSubmit = @(
                [pscustomobject]@{
                    matcher = "*"
                    hooks = @(
                        [pscustomobject]@{
                            type = "command"
                            command = $Command
                            timeout = 5
                        }
                    )
                }
            )
            Stop = @(
                [pscustomobject]@{
                    matcher = "*"
                    hooks = @(
                        [pscustomobject]@{
                            type = "command"
                            command = $Command
                            timeout = 30
                        }
                    )
                }
            )
        }
    }

    $Payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $HooksPath -Encoding UTF8
}

function Test-AutoReviewHookCommand($Hook) {
    if ($null -eq $Hook -or $null -eq $Hook.command) {
        return $false
    }

    return "$($Hook.command)".Contains("auto_review_hook.py")
}

function Remove-AutoReviewHookEntries($Entries) {
    $Filtered = @()
    foreach ($Entry in @($Entries)) {
        if ($null -eq $Entry -or $null -eq $Entry.hooks) {
            $Filtered += $Entry
            continue
        }

        $OriginalHooks = @($Entry.hooks)
        $KeptHooks = @()
        foreach ($Hook in $OriginalHooks) {
            if (-not (Test-AutoReviewHookCommand $Hook)) {
                $KeptHooks += $Hook
            }
        }

        if ($KeptHooks.Count -eq $OriginalHooks.Count) {
            $Filtered += $Entry
        } elseif ($KeptHooks.Count -gt 0) {
            $Entry.hooks = $KeptHooks
            $Filtered += $Entry
        }
    }
    return $Filtered
}

function Write-CodexHooksJson([string]$HookPython, [string]$HookScript) {
    $Command = "$HookPython $(Quote-CmdArgument $HookScript)"

    if (Test-Path -LiteralPath $CodexHooksPath -PathType Leaf) {
        $Raw = Get-Content -LiteralPath $CodexHooksPath -Raw
        if ([string]::IsNullOrWhiteSpace($Raw)) {
            $Payload = [pscustomobject]@{
                description = "Codex user hooks"
                hooks = [pscustomobject]@{}
            }
        } else {
            $Payload = $Raw | ConvertFrom-Json
        }
    } else {
        $Payload = [pscustomobject]@{
            description = "Codex user hooks"
            hooks = [pscustomobject]@{}
        }
    }

    if ($Payload -isnot [pscustomobject]) {
        throw "$CodexHooksPath must contain a JSON object"
    }

    if ($null -eq $Payload.hooks) {
        $Payload | Add-Member -MemberType NoteProperty -Name hooks -Value ([pscustomobject]@{})
    }

    foreach ($EventName in @("UserPromptSubmit", "Stop")) {
        if ($null -eq $Payload.hooks.$EventName) {
            $Payload.hooks | Add-Member -MemberType NoteProperty -Name $EventName -Value @()
        } else {
            $Payload.hooks.$EventName = @(Remove-AutoReviewHookEntries $Payload.hooks.$EventName)
        }
    }

    $Payload.hooks.UserPromptSubmit = @($Payload.hooks.UserPromptSubmit) + [pscustomobject]@{
        matcher = "*"
        hooks = @(
            [pscustomobject]@{
                type = "command"
                command = $Command
                timeout = 5
            }
        )
    }
    $Payload.hooks.Stop = @($Payload.hooks.Stop) + [pscustomobject]@{
        matcher = "*"
        hooks = @(
            [pscustomobject]@{
                type = "command"
                command = $Command
                timeout = 30
            }
        )
    }

    $CodexHooksDir = Split-Path -Parent $CodexHooksPath
    New-Item -ItemType Directory -Force -Path $CodexHooksDir | Out-Null
    $Payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $CodexHooksPath -Encoding UTF8
}

function Invoke-PythonScript {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ScriptText,

        [string[]]$Arguments = @()
    )

    if ([string]::IsNullOrWhiteSpace($Script:HookPythonCommand)) {
        throw "Python command was not detected."
    }

    $TempScript = [System.IO.Path]::Combine(
        [System.IO.Path]::GetTempPath(),
        "auto-review-$([guid]::NewGuid().ToString('N')).py"
    )

    try {
        Set-Content -LiteralPath $TempScript -Value $ScriptText -Encoding UTF8
        $PythonArgs = @()
        $PythonArgs += $Script:HookPythonArgs
        $PythonArgs += $TempScript
        $PythonArgs += $Arguments
        & $Script:HookPythonCommand @PythonArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Python helper exited with code $LASTEXITCODE."
        }
    } finally {
        if (Test-PathExists $TempScript) {
            Remove-Item -LiteralPath $TempScript -Force
        }
    }
}

function Trust-CodexAutoReviewHooks {
    $TrustScript = @'
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

hooks_path = Path(sys.argv[1]).expanduser()
try:
    normalized_hooks_path = hooks_path.resolve()
except OSError:
    normalized_hooks_path = hooks_path.absolute()

codex = shutil.which("codex")
if codex is None:
    raise SystemExit("Cannot trust hooks: codex was not found in PATH.")

process = subprocess.Popen(
    [codex, "app-server", "--listen", "stdio://"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
    bufsize=1,
)

lines = queue.Queue()


def collect_stdout() -> None:
    assert process.stdout is not None
    for line in process.stdout:
        lines.put(line)
    lines.put(None)


threading.Thread(target=collect_stdout, daemon=True).start()


def send(payload: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_response(request_id: int, timeout_seconds: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            continue
        if line is None:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id:
            if "error" in payload:
                raise SystemExit(
                    f"Codex app-server request {request_id} failed: {payload['error']}"
                )
            return payload.get("result") or {}
    raise SystemExit(f"Timed out waiting for Codex app-server response {request_id}.")


try:
    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "auto-review-installer", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        }
    )
    read_response(1)
    send({"jsonrpc": "2.0", "method": "initialized"})
    send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "hooks/list",
            "params": {"cwds": [str(normalized_hooks_path.parent)]},
        }
    )
    hooks_result = read_response(2)
    trust_entries: dict[str, dict[str, str]] = {}
    for entry in hooks_result.get("data") or []:
        for hook in entry.get("hooks") or []:
            try:
                source_path = Path(hook.get("sourcePath") or "").expanduser().resolve()
            except OSError:
                source_path = Path(hook.get("sourcePath") or "").expanduser().absolute()
            command = str(hook.get("command") or "")
            key = hook.get("key")
            current_hash = hook.get("currentHash")
            if (
                source_path == normalized_hooks_path
                and "auto_review_hook.py" in command
                and isinstance(key, str)
                and isinstance(current_hash, str)
            ):
                trust_entries[key] = {"trusted_hash": current_hash}

    if len(trust_entries) < 2:
        raise SystemExit(
            f"Expected to discover 2 auto-review hooks in {normalized_hooks_path}, "
            f"found {len(trust_entries)}."
        )

    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "config/batchWrite",
            "params": {
                "edits": [
                    {
                        "keyPath": "hooks.state",
                        "value": trust_entries,
                        "mergeStrategy": "upsert",
                    }
                ],
                "reloadUserConfig": True,
            },
        }
    )
    read_response(3)

    send(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "hooks/list",
            "params": {"cwds": [str(normalized_hooks_path.parent)]},
        }
    )
    verified = read_response(4)
    remaining_untrusted = []
    for entry in verified.get("data") or []:
        for hook in entry.get("hooks") or []:
            if hook.get("key") in trust_entries and hook.get("trustStatus") != "trusted":
                remaining_untrusted.append(hook.get("key"))
    if remaining_untrusted:
        raise SystemExit(
            "Codex did not mark auto-review hooks trusted: "
            + ", ".join(str(key) for key in remaining_untrusted)
        )
finally:
    try:
        process.terminate()
    except OSError:
        pass
'@

    Invoke-PythonScript -ScriptText $TrustScript -Arguments @($CodexHooksPath)
}

$HookPython = Get-HookPythonCommand

Test-CodexCli
Test-SourceLayout
Test-JsonFile -Path (Join-Path $PluginSource ".codex-plugin/plugin.json") -Label "plugin.json"
Test-JsonFile -Path (Join-Path $PluginSource "hooks/hooks.json") -Label "source hooks.json"
Test-JsonFile -Path (Join-Path $ProjectRoot ".agents/plugins/marketplace.json") -Label "source marketplace.json"
Test-MarketplaceJson
Test-CodexHooksJson
Test-InstallTargets
Test-TargetRootWritable
Test-SymlinkSupported

New-Item -ItemType Directory -Force -Path $PluginRoot | Out-Null
New-Item -ItemType Directory -Force -Path $MarketplaceDir | Out-Null
New-Item -ItemType Directory -Force -Path $SkillRoot | Out-Null

if ($Force) {
    foreach ($ExistingPath in @($PluginDest, $SkillDest)) {
        if (Test-PathExists $ExistingPath) {
            Remove-Item -LiteralPath $ExistingPath -Recurse -Force
        }
    }
}

if ($Mode -eq "symlink") {
    try {
        New-Item -ItemType Directory -Force -Path (Join-Path $PluginDest "hooks") | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $PluginDest ".codex-plugin") -Target (Join-Path $PluginSource ".codex-plugin") | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $PluginDest "tests") -Target (Join-Path $PluginSource "tests") | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $PluginDest "hooks/auto_review_hook.py") -Target (Join-Path $PluginSource "hooks/auto_review_hook.py") | Out-Null
        New-Item -ItemType SymbolicLink -Path $SkillDest -Target $SkillSource | Out-Null
    } catch {
        throw "Symlink install failed. On Windows, enable Developer Mode or run PowerShell as Administrator, or use: .\install.ps1 -Mode copy"
    }
} else {
    Copy-Item -LiteralPath $PluginSource -Destination $PluginDest -Recurse
    Copy-Item -LiteralPath $SkillSource -Destination $SkillDest -Recurse
}

Write-HooksJson -PluginPath $PluginDest -HookPython $HookPython
Write-CodexHooksJson -HookPython $HookPython -HookScript (Join-Path $PluginDest "hooks/auto_review_hook.py")
Trust-CodexAutoReviewHooks

if (Test-Path $MarketplacePath) {
    $Raw = Get-Content -LiteralPath $MarketplacePath -Raw
    if ([string]::IsNullOrWhiteSpace($Raw)) {
        $Payload = New-DefaultMarketplacePayload
    } else {
        $Payload = $Raw | ConvertFrom-Json
    }
} else {
    $Payload = New-DefaultMarketplacePayload
}

if ($null -eq $Payload.plugins) {
    $Payload | Add-Member -MemberType NoteProperty -Name plugins -Value @()
}
if ($null -eq $Payload.interface) {
    $Payload | Add-Member -MemberType NoteProperty -Name interface -Value ([ordered]@{ displayName = "Local Plugins" })
}

$Plugins = @($Payload.plugins | Where-Object { $_.name -ne $PluginName })
$Plugins += [ordered]@{
    name = $PluginName
    source = [ordered]@{
        source = "local"
        path = "./plugins/$PluginName"
    }
    policy = [ordered]@{
        installation = "INSTALLED_BY_DEFAULT"
        authentication = "ON_INSTALL"
    }
    category = "Productivity"
}
$Payload.plugins = $Plugins

$Payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $MarketplacePath -Encoding UTF8

& codex plugin marketplace add $TargetRoot | Out-Null

Write-Host "Installed $PluginName using $Mode mode."
Write-Host "Plugin: $PluginDest"
Write-Host "Marketplace: $MarketplacePath"
Write-Host "Hooks: $CodexHooksPath"
Write-Host "Hook trust: $(Join-Path $CodexHome 'config.toml')"
Write-Host "Skill: $SkillDest"
Write-Host 'Restart Codex, then use: $auto-review <your task>'
