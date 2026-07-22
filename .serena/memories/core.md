# Project map
- Codex auto-review distribution with two coupled surfaces:
  - Runtime plugin: `plugins/auto-review/` (manifest, lifecycle hook, hook tests).
  - User-facing skill: `skills/auto-review/` (workflow contract and agent metadata).
- Install/update tooling: `scripts/auto_review_installer.py`, shell/PowerShell wrappers, and installer tests.
- User docs are bilingual: `README.md` and `README.en.md`; behavior changes must keep both aligned.
- Hook state is external to the repo under the active Codex home; tests isolate it through temporary state homes.
- Read `mem:tech_stack` for runtime/tooling, `mem:conventions` for implementation contracts, `mem:suggested_commands` for local commands, and `mem:task_completion` before handoff.