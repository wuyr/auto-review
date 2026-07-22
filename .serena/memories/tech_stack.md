# Tech stack
- Runtime: Python 3.10+, standard library only in the hook/installer.
- Tests: standard-library `unittest`; no package manager or dependency bootstrap is required.
- Distribution metadata/config: JSON plugin manifest and hooks config, YAML skill agent metadata, Markdown skill/docs.
- Platform entrypoints: Bash for macOS/Linux and PowerShell for Windows.
- Codex plugin version uses a `+codex.<cachebuster>` suffix during local update/reinstall.