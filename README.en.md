# Auto Review (for Codex)

As the name suggests, when you use this skill in a code-change prompt, it will automatically start a review of the files modified in the current turn after Codex finishes making changes. If issues are found, it will automatically fix them, then run another review. The loop continues until no issues are detected. Each review performs a convergent full pass that searches for sibling cases of the same root cause, and each fix pass checks equivalent affected paths, reducing long chains of rounds that surface only one or two findings at a time.

## Usage

To avoid unnecessary reviews, this skill does not trigger automatically. You must enable it explicitly.

Like other skills, add `$auto-review` to your prompt, for example:

```text
$auto-review Fix the xxx issue
```

```text
$auto-review Add the xxx feature
```

The skill supports `/plan`, `/goal`.

When used in **plan** mode, it will not trigger immediately. Instead, it will trigger after the plan is implemented.

In the **goal** mode, the hook waits until the goal is marked `complete`, then starts the review loop instead of reviewing intermediate goal-continuation turns.

## Requirements

- Codex is installed.
- Python 3.10 or newer is installed.

## Installation

### macOS / Linux

Run the install script from the project directory:

```bash
./install.sh
```

macOS / Linux defaults to symlink mode. This does not copy files; it creates links instead. If you want to copy the files directly, use:

```bash
./install.sh --mode copy
```

### Windows

```powershell
.\install.ps1
```

Windows defaults to copy mode. To use symlink mode, enable Windows Developer Mode or run PowerShell as Administrator:

```powershell
.\install.ps1 -Mode symlink
```

Restart Codex after installation.

## Uninstallation

macOS / Linux:

```bash
cd /path/to/auto-review
./uninstall.sh
```

Windows PowerShell:

```powershell
cd C:\path\to\auto-review
.\uninstall.ps1
```

## How It Works

It uses the hook mechanism provided by Codex to listen for the task `Stop` signal, then automatically submits a prompt:

```text
Review the changes from this turn, perform a complete pass, and do not stop after the first issue.
```

After that prompt finishes, if issues are found, it continues by submitting another prompt:

```text
Please fix these issues.
```

This process keeps looping until the review can no longer find new issues.

The review treats a defect as a finding only when it is reachable within supported usage or the explicit threat model, has material impact, and is backed by direct evidence. Rare but credible, high-impact defects still count; theoretical attacks outside the stated threat model, scenarios requiring arbitrary rewrites of multiple trusted artifacts, style preferences, and extra hardening do not block the loop. There is no minimum finding count: completeness is measured by coverage, so a review with only zero, one, or two real issues must report exactly that number without padding the result.

**Note:** The review scope is limited to the files touched by the turn where the skill was used. It does not additionally review all local changes or the whole current branch.
