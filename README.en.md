# Auto Review (for Codex)

As the name suggests, when you use this skill in a prompt that modifies files, Codex automatically reviews `the task's changes` after it finishes. If it finds issues, it runs a **repair + review loop** within the original task's scope (up to two rounds).

## Usage

To avoid unnecessary reviews, this skill does not trigger automatically. You must enable it explicitly.

Like other skills, add `$auto-review` to your prompt, for example:

```text
$auto-review Fix the xxx issue
```

```text
$auto-review Add the xxx feature
```

Or simply use:

```text
$auto-review
```

This form first reviews the most recent substantive task involving file changes in the current session.

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

Fully quit and restart Codex after installation or updates. Creating or forking a session without restarting will not apply the changes.

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

It uses the hook mechanism provided by Codex to listen for the task `Stop` signal, then automatically starts a review:

```text
Review the task changes for omissions and logic errors.
```

If no issues are found, the review ends immediately. If an issue can be fixed within the original task, Codex submits:

```text
Please fix these issues, then review again.
```
