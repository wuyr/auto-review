# Completion checks
1. Run `python3 plugins/auto-review/tests/test_auto_review_hook.py`.
2. Run `python3 scripts/test_auto_review_installer.py` when installer, manifest, marketplace, or install behavior is touched.
3. Run plugin validation: `python3 /home/wuyr/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/auto-review`.
4. If `skills/auto-review/SKILL.md` changed, run `python3 /home/wuyr/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/auto-review`.
5. Run `git diff --check`.
6. For an existing installed local plugin, update its cachebuster with plugin-creator's helper, reinstall from the confirmed local marketplace, then test behavior in a new Codex thread.