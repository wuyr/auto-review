# Useful commands
- Hook suite: `python3 plugins/auto-review/tests/test_auto_review_hook.py`
- Installer suite: `python3 scripts/test_auto_review_installer.py`
- All repository tests: run both commands above (tests live in separate non-package paths).
- Install local symlink distribution: `./install.sh`
- Install local copy distribution: `./install.sh --mode copy`
- Check patch whitespace: `git diff --check`
- Plugin validation uses the system plugin-creator helper; skill validation uses the system skill-creator `quick_validate.py` helper.