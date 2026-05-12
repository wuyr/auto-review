[CmdletBinding()]
param(
    [string]$TargetRoot = $(if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }),

    [switch]$KeepFiles
)

$ErrorActionPreference = "Stop"

$PluginName = "auto-review"
$PluginRoot = Join-Path $TargetRoot "plugins"
$PluginDest = Join-Path $PluginRoot $PluginName
$MarketplacePath = Join-Path $TargetRoot ".agents/plugins/marketplace.json"
$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$CodexHooksPath = Join-Path $CodexHome "hooks.json"
$SkillDest = Join-Path (Join-Path $CodexHome "skills") $PluginName

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

if (Test-Path $MarketplacePath) {
    $Raw = Get-Content -LiteralPath $MarketplacePath -Raw
    if (-not [string]::IsNullOrWhiteSpace($Raw)) {
        $Payload = $Raw | ConvertFrom-Json
        if ($null -ne $Payload.plugins) {
            $Payload.plugins = @($Payload.plugins | Where-Object {
                $_.name -ne $PluginName
            })
            $Payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $MarketplacePath -Encoding UTF8
        }
    }
}

if (Test-Path $CodexHooksPath) {
    $Raw = Get-Content -LiteralPath $CodexHooksPath -Raw
    if (-not [string]::IsNullOrWhiteSpace($Raw)) {
        $Payload = $Raw | ConvertFrom-Json
        if ($null -ne $Payload.hooks) {
            foreach ($EventName in @("UserPromptSubmit", "Stop")) {
                if ($null -ne $Payload.hooks.$EventName) {
                    $Payload.hooks.$EventName = @(Remove-AutoReviewHookEntries $Payload.hooks.$EventName)
                }
            }
            $Payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $CodexHooksPath -Encoding UTF8
        }
    }
}

if (-not $KeepFiles) {
    foreach ($ExistingPath in @($PluginDest, $SkillDest)) {
        if (Test-Path $ExistingPath) {
            Remove-Item -LiteralPath $ExistingPath -Recurse -Force
        }
    }
}

Write-Host "Uninstalled $PluginName from $TargetRoot."
Write-Host "Restart Codex to unload hooks."
