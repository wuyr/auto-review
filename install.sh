#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="auto-review"
MODE="symlink"
CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
TARGET_ROOT="${CODEX_HOME_DIR}"
FORCE=1

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--mode symlink|copy] [--target-root DIR] [--force]

Installs the auto-review Codex plugin into:
  <target-root>/plugins/auto-review
  <target-root>/.agents/plugins/marketplace.json
  ${CODEX_HOME:-$HOME/.codex}/hooks.json
  ${CODEX_HOME:-$HOME/.codex}/config.toml hook trust state
  ${CODEX_HOME:-$HOME/.codex}/skills/auto-review

Options:
  --mode        symlink or copy. Default: symlink
  --target-root install root. Default: ${CODEX_HOME:-$HOME/.codex}
  --force       replace an existing installed plugin directory/link. Default: enabled
USAGE
}

fail() {
  echo "Error: $*" >&2
  exit 1
}

require_command() {
  local command_name="$1"
  local install_hint="$2"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    fail "Required command '${command_name}' was not found. ${install_hint}"
  fi
}

check_python3() {
  local python_bin="$1"

  "$python_bin" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

resolve_command() {
  local command_name="$1"

  if [[ "$command_name" == */* ]]; then
    [[ -x "$command_name" ]] || return 1
    printf '%s\n' "$command_name"
  else
    command -v "$command_name"
  fi
}

detect_python3() {
  local candidate
  local resolved

  if [[ -n "${PYTHON:-}" ]]; then
    resolved="$(resolve_command "$PYTHON")" || fail "PYTHON is set to '${PYTHON}', but it is not executable."
    check_python3 "$resolved" || fail "PYTHON is set to '${PYTHON}', but it is not Python 3.10 or newer."
    printf '%s\n' "$resolved"
    return
  fi

  for candidate in python3 python; do
    if resolved="$(command -v "$candidate" 2>/dev/null)" && check_python3 "$resolved"; then
      printf '%s\n' "$resolved"
      return
    fi
  done

  fail "Python 3.10 or newer is required. Install a supported Python or set PYTHON to that executable."
}

check_codex_cli() {
  require_command "codex" "Install Codex CLI or add it to PATH, then re-run this installer."

  if ! codex --version >/dev/null 2>&1; then
    fail "Codex CLI was found at '$(command -v codex)', but 'codex --version' failed."
  fi
}

check_source_layout() {
  local required_file
  local required_files=(
    "${PLUGIN_SOURCE}/.codex-plugin/plugin.json"
    "${PLUGIN_SOURCE}/hooks/auto_review_hook.py"
    "${PLUGIN_SOURCE}/hooks/hooks.json"
    "${SKILL_SOURCE}/SKILL.md"
  )

  for required_file in "${required_files[@]}"; do
    [[ -f "$required_file" ]] || fail "Required plugin source file is missing: ${required_file}"
  done
}

check_json_file() {
  local path="$1"
  local label="$2"

  "$PYTHON_BIN" - "$path" "$label" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
label = sys.argv[2]

try:
    raw = path.read_text(encoding="utf-8")
except OSError as exc:
    raise SystemExit(f"Cannot read {label}: {path}: {exc}")

if not raw.strip():
    raise SystemExit(f"{label} is empty: {path}")

try:
    json.loads(raw)
except json.JSONDecodeError as exc:
    raise SystemExit(f"Invalid JSON in {label}: {path}: {exc}")
PY
}

check_marketplace_json() {
  [[ -f "$MARKETPLACE_PATH" ]] || return 0

  "$PYTHON_BIN" - "$MARKETPLACE_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    raw = path.read_text(encoding="utf-8")
except OSError as exc:
    raise SystemExit(f"Cannot read existing marketplace.json: {path}: {exc}")

if not raw.strip():
    raise SystemExit(f"Existing marketplace.json is empty: {path}")

try:
    payload = json.loads(raw)
except json.JSONDecodeError as exc:
    raise SystemExit(f"Invalid JSON in existing marketplace.json: {path}: {exc}")

if not isinstance(payload, dict):
    raise SystemExit(f"Existing marketplace.json must contain a JSON object: {path}")

plugins = payload.get("plugins")
if plugins is not None and not isinstance(plugins, list):
    raise SystemExit(f"Existing marketplace.json field 'plugins' must be an array: {path}")

interface = payload.get("interface")
if interface is not None and not isinstance(interface, dict):
    raise SystemExit(f"Existing marketplace.json field 'interface' must be an object: {path}")
PY
}

check_codex_hooks_json() {
  [[ -f "$CODEX_HOOKS_PATH" ]] || return 0

  "$PYTHON_BIN" - "$CODEX_HOOKS_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    raw = path.read_text(encoding="utf-8")
except OSError as exc:
    raise SystemExit(f"Cannot read existing hooks.json: {path}: {exc}")

if not raw.strip():
    raise SystemExit(f"Existing hooks.json is empty: {path}")

try:
    payload = json.loads(raw)
except json.JSONDecodeError as exc:
    raise SystemExit(f"Invalid JSON in existing hooks.json: {path}: {exc}")

if not isinstance(payload, dict):
    raise SystemExit(f"Existing hooks.json must contain a JSON object: {path}")

hooks = payload.get("hooks")
if hooks is not None and not isinstance(hooks, dict):
    raise SystemExit(f"Existing hooks.json field 'hooks' must be an object: {path}")
PY
}

check_target_conflicts() {
  if [[ -e "$PLUGIN_DEST" || -L "$PLUGIN_DEST" ]]; then
    [[ "$FORCE" -eq 1 ]] || fail "Install target exists at ${PLUGIN_DEST}. Re-run with --force to replace it."
  fi

  if [[ -e "$SKILL_DEST" || -L "$SKILL_DEST" ]]; then
    [[ "$FORCE" -eq 1 ]] || fail "Skill target exists at ${SKILL_DEST}. Re-run with --force to replace it."
  fi
}

check_targets_do_not_overwrite_sources() {
  "$PYTHON_BIN" - "$PLUGIN_SOURCE" "$PLUGIN_DEST" "$SKILL_SOURCE" "$SKILL_DEST" <<'PY'
import sys
from pathlib import Path

plugin_source = Path(sys.argv[1]).expanduser().resolve()
plugin_dest = Path(sys.argv[2]).expanduser().resolve()
skill_source = Path(sys.argv[3]).expanduser().resolve()
skill_dest = Path(sys.argv[4]).expanduser().resolve()

if plugin_source == plugin_dest:
    raise SystemExit(
        f"Plugin install target resolves to the source directory: {plugin_dest}. "
        "Choose a different --target-root."
    )

if skill_source == skill_dest:
    raise SystemExit(
        f"Skill install target resolves to the source directory: {skill_dest}. "
        "Set CODEX_HOME or choose a different --target-root."
    )
PY
}

check_target_root_writable() {
  local probe_dir

  if [[ -e "$TARGET_ROOT" && ! -d "$TARGET_ROOT" ]]; then
    fail "Install target root exists but is not a directory: ${TARGET_ROOT}"
  fi

  mkdir -p "$TARGET_ROOT" || fail "Cannot create install target root: ${TARGET_ROOT}"
  probe_dir="$(mktemp -d "${TARGET_ROOT}/.auto-review-install-check.XXXXXX")" \
    || fail "Install target root is not writable: ${TARGET_ROOT}"
  rm -rf "$probe_dir"
}

check_symlink_supported() {
  local probe_dir
  local probe_target
  local probe_link

  [[ "$MODE" == "symlink" ]] || return 0

  require_command "ln" "Use --mode copy on systems that do not support symbolic links."

  probe_dir="$(mktemp -d "${TARGET_ROOT}/.auto-review-symlink-check.XXXXXX")" \
    || fail "Cannot create symlink capability probe under: ${TARGET_ROOT}"
  probe_target="${probe_dir}/target"
  probe_link="${probe_dir}/link"
  : > "$probe_target" || {
    rm -rf "$probe_dir"
    fail "Cannot write symlink capability probe under: ${TARGET_ROOT}"
  }

  if ! ln -s "$probe_target" "$probe_link" 2>/dev/null || [[ ! -L "$probe_link" ]]; then
    rm -rf "$probe_dir"
    fail "Symbolic links are not available under ${TARGET_ROOT}. Re-run with --mode copy, or enable symlink support."
  fi

  rm -rf "$probe_dir"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --target-root)
      TARGET_ROOT="${2:-}"
      shift 2
      ;;
    --force)
      FORCE=1
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

if [[ "$MODE" != "symlink" && "$MODE" != "copy" ]]; then
  fail "--mode must be symlink or copy"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SOURCE="${SCRIPT_DIR}/plugins/${PLUGIN_NAME}"
SKILL_SOURCE="${SCRIPT_DIR}/skills/${PLUGIN_NAME}"
PLUGIN_DEST="${TARGET_ROOT}/plugins/${PLUGIN_NAME}"
MARKETPLACE_PATH="${TARGET_ROOT}/.agents/plugins/marketplace.json"
CODEX_HOOKS_PATH="${CODEX_HOME_DIR}/hooks.json"
SKILL_DEST="${CODEX_HOME_DIR}/skills/${PLUGIN_NAME}"

require_command "mkdir" "Install standard POSIX command-line tools, then re-run this installer."
require_command "rm" "Install standard POSIX command-line tools, then re-run this installer."
require_command "mktemp" "Install standard POSIX command-line tools, then re-run this installer."
[[ "$MODE" == "symlink" ]] || require_command "cp" "Install standard POSIX command-line tools, then re-run this installer."

PYTHON_BIN="$(detect_python3)"
HOOK_PYTHON_BIN="$PYTHON_BIN"

check_codex_cli
check_source_layout
check_json_file "${PLUGIN_SOURCE}/.codex-plugin/plugin.json" "plugin.json"
check_json_file "${PLUGIN_SOURCE}/hooks/hooks.json" "source hooks.json"
check_json_file "${SCRIPT_DIR}/.agents/plugins/marketplace.json" "source marketplace.json"
check_marketplace_json
check_codex_hooks_json
check_targets_do_not_overwrite_sources
check_target_conflicts
check_target_root_writable
check_symlink_supported

mkdir -p "${TARGET_ROOT}/plugins" "${TARGET_ROOT}/.agents/plugins" "${CODEX_HOME_DIR}/skills"

if [[ "$FORCE" -eq 1 ]]; then
  rm -rf "$PLUGIN_DEST" "$SKILL_DEST"
fi

if [[ "$MODE" == "symlink" ]]; then
  mkdir -p "$PLUGIN_DEST/hooks"
  ln -s "$PLUGIN_SOURCE/.codex-plugin" "$PLUGIN_DEST/.codex-plugin"
  ln -s "$PLUGIN_SOURCE/tests" "$PLUGIN_DEST/tests"
  ln -s "$PLUGIN_SOURCE/hooks/auto_review_hook.py" "$PLUGIN_DEST/hooks/auto_review_hook.py"
  ln -s "$SKILL_SOURCE" "$SKILL_DEST"
else
  mkdir -p "$PLUGIN_DEST"
  rm -rf "$PLUGIN_DEST"
  cp -R "$PLUGIN_SOURCE" "$PLUGIN_DEST"
  cp -R "$SKILL_SOURCE" "$SKILL_DEST"
fi

"$PYTHON_BIN" - "$PLUGIN_DEST/hooks/hooks.json" "$HOOK_PYTHON_BIN" "$PLUGIN_DEST/hooks/auto_review_hook.py" <<'PY'
import json
import shlex
import sys
from pathlib import Path

hooks_path = Path(sys.argv[1])
python_bin = sys.argv[2]
hook_script = sys.argv[3]
command = f"{shlex.quote(python_bin)} {shlex.quote(hook_script)}"
payload = {
    "description": "Auto Review hooks: opt-in UserPromptSubmit arming and Stop-driven review/fix loop",
    "hooks": {
        "UserPromptSubmit": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": command, "timeout": 5}],
            }
        ],
        "Stop": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": command, "timeout": 30}],
            }
        ],
    },
}
hooks_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

"$PYTHON_BIN" - "$CODEX_HOOKS_PATH" "$HOOK_PYTHON_BIN" "$PLUGIN_DEST/hooks/auto_review_hook.py" <<'PY'
import json
import shlex
import sys
from pathlib import Path

hooks_path = Path(sys.argv[1]).expanduser()
python_bin = sys.argv[2]
hook_script = sys.argv[3]
command = f"{shlex.quote(python_bin)} {shlex.quote(hook_script)}"

if hooks_path.exists():
    try:
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"Invalid JSON: {hooks_path}")
else:
    payload = {
        "description": "Codex user hooks",
        "hooks": {},
    }

if not isinstance(payload, dict):
    raise SystemExit(f"{hooks_path} must contain a JSON object")

hooks = payload.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit(f"{hooks_path} field 'hooks' must be an object")

def without_auto_review(entries):
    if not isinstance(entries, list):
        return []
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
            continue
        if kept_hooks:
            updated_entry = dict(entry)
            updated_entry["hooks"] = kept_hooks
            filtered.append(updated_entry)
    return filtered

for event_name in ("UserPromptSubmit", "Stop"):
    hooks[event_name] = without_auto_review(hooks.get(event_name))

hooks["UserPromptSubmit"].append(
    {
        "matcher": "*",
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    }
)
hooks["Stop"].append(
    {
        "matcher": "*",
        "hooks": [{"type": "command", "command": command, "timeout": 30}],
    }
)

hooks_path.parent.mkdir(parents=True, exist_ok=True)
hooks_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

"$PYTHON_BIN" - "$CODEX_HOOKS_PATH" <<'PY'
import json
import selectors
import shutil
import subprocess
import sys
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

selector = selectors.DefaultSelector()
assert process.stdout is not None
selector.register(process.stdout, selectors.EVENT_READ)


def send(payload: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_response(request_id: int, timeout_seconds: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        events = selector.select(timeout=remaining)
        if not events:
            continue
        for key, _ in events:
            line = key.fileobj.readline()
            if not line:
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
PY

"$PYTHON_BIN" - "$MARKETPLACE_PATH" "$PLUGIN_NAME" <<'PY'
import json
import sys
from pathlib import Path

marketplace_path = Path(sys.argv[1]).expanduser()
plugin_name = sys.argv[2]

if marketplace_path.exists():
    try:
        payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"Invalid JSON: {marketplace_path}")
else:
    payload = {
        "name": "local-plugins",
        "interface": {"displayName": "Local Plugins"},
        "plugins": [],
    }

if not isinstance(payload, dict):
    raise SystemExit(f"{marketplace_path} must contain a JSON object")

payload.setdefault("name", "local-plugins")
interface = payload.setdefault("interface", {})
if isinstance(interface, dict):
    interface.setdefault("displayName", "Local Plugins")
else:
    payload["interface"] = {"displayName": "Local Plugins"}

plugins = payload.setdefault("plugins", [])
if not isinstance(plugins, list):
    raise SystemExit(f"{marketplace_path} field 'plugins' must be an array")

plugins = [
    item
    for item in plugins
    if not (isinstance(item, dict) and item.get("name") == plugin_name)
]
plugins.append(
    {
        "name": plugin_name,
        "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
        "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
)
payload["plugins"] = plugins

marketplace_path.parent.mkdir(parents=True, exist_ok=True)
marketplace_path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

codex plugin marketplace add "$TARGET_ROOT" >/dev/null

echo "Installed ${PLUGIN_NAME} using ${MODE} mode."
echo "Plugin: ${PLUGIN_DEST}"
echo "Marketplace: ${MARKETPLACE_PATH}"
echo "Hooks: ${CODEX_HOOKS_PATH}"
echo "Hook trust: ${CODEX_HOME_DIR}/config.toml"
echo "Skill: ${SKILL_DEST}"
echo "Restart Codex, then use: \$auto-review <your task>"
