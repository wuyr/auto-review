#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="auto-review"
CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
TARGET_ROOT="${CODEX_HOME_DIR}"
KEEP_FILES=0

usage() {
  cat <<'USAGE'
Usage: ./uninstall.sh [--target-root DIR] [--keep-files]

Removes the auto-review Codex plugin marketplace entry, installed plugin files,
${CODEX_HOME:-$HOME/.codex}/hooks.json entries,
and ${CODEX_HOME:-$HOME/.codex}/skills/auto-review.

Options:
  --target-root install root. Default: ${CODEX_HOME:-$HOME/.codex}
  --keep-files   remove marketplace and hook entries, but keep plugin/skill files or links
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-root)
      TARGET_ROOT="${2:-}"
      shift 2
      ;;
    --keep-files)
      KEEP_FILES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "Python 3 is required to update marketplace.json" >&2
    exit 1
  fi
fi

MARKETPLACE_PATH="${TARGET_ROOT}/.agents/plugins/marketplace.json"
CODEX_HOOKS_PATH="${CODEX_HOME_DIR}/hooks.json"
PLUGIN_DEST="${TARGET_ROOT}/plugins/${PLUGIN_NAME}"
SKILL_DEST="${CODEX_HOME_DIR}/skills/${PLUGIN_NAME}"

if [[ -f "$MARKETPLACE_PATH" ]]; then
  "$PYTHON_BIN" - "$MARKETPLACE_PATH" "$PLUGIN_NAME" <<'PY'
import json
import sys
from pathlib import Path

marketplace_path = Path(sys.argv[1]).expanduser()
plugin_name = sys.argv[2]

payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
if isinstance(payload, dict) and isinstance(payload.get("plugins"), list):
    payload["plugins"] = [
        item
        for item in payload["plugins"]
        if not (isinstance(item, dict) and item.get("name") == plugin_name)
    ]
    marketplace_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
PY
fi

if [[ -f "$CODEX_HOOKS_PATH" ]]; then
  "$PYTHON_BIN" - "$CODEX_HOOKS_PATH" <<'PY'
import json
import sys
from pathlib import Path

hooks_path = Path(sys.argv[1]).expanduser()
payload = json.loads(hooks_path.read_text(encoding="utf-8"))
if isinstance(payload, dict) and isinstance(payload.get("hooks"), dict):
    def without_auto_review(entries):
        if not isinstance(entries, list):
            return entries
        filtered = []
        for entry in entries:
            if not isinstance(entry, dict):
                filtered.append(entry)
                continue
            entry_hooks = entry.get("hooks")
            if not isinstance(entry_hooks, list):
                filtered.append(entry)
                continue
            kept_hooks = [
                hook
                for hook in entry_hooks
                if not (
                    isinstance(hook, dict)
                    and "auto_review_hook.py" in str(hook.get("command", ""))
                )
            ]
            if len(kept_hooks) == len(entry_hooks):
                filtered.append(entry)
            elif kept_hooks:
                updated_entry = dict(entry)
                updated_entry["hooks"] = kept_hooks
                filtered.append(updated_entry)
        return filtered

    for event_name in ("UserPromptSubmit", "Stop"):
        entries = payload["hooks"].get(event_name)
        if not isinstance(entries, list):
            continue
        payload["hooks"][event_name] = without_auto_review(entries)
    hooks_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
PY
fi

if [[ "$KEEP_FILES" -eq 0 ]]; then
  rm -rf "$PLUGIN_DEST" "$SKILL_DEST"
fi

echo "Uninstalled ${PLUGIN_NAME} from ${TARGET_ROOT}."
echo "Restart Codex to unload plugin hooks."
