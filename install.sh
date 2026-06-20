#!/usr/bin/env bash
set -euo pipefail

MODE="symlink"
CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
TARGET_ROOT="${CODEX_HOME_DIR}"
FORCE=1

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--mode symlink|copy] [--target-root DIR] [--force]

Installs the auto-review Codex plugin and skill into:
  <target-root>/plugins/auto-review
  <target-root>/.agents/plugins/marketplace.json
  ${CODEX_HOME:-$HOME/.codex}/skills/auto-review

The installer enables the plugin with `codex plugin add` and trusts the plugin
hooks. It does not install duplicate global hooks into hooks.json.

Options:
  --mode        symlink or copy. Default: symlink
  --target-root install root. Default: ${CODEX_HOME:-$HOME/.codex}
  --force       replace existing installed plugin/skill files. Default: enabled
USAGE
}

fail() {
  echo "Error: $*" >&2
  exit 1
}

check_python3() {
  local python_bin="$1"
  "$python_bin" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

resolve_python3() {
  local candidate

  if [[ -n "${PYTHON:-}" ]]; then
    command -v "$PYTHON" >/dev/null 2>&1 || fail "PYTHON is set to '${PYTHON}', but it is not executable."
    check_python3 "$PYTHON" || fail "PYTHON is set to '${PYTHON}', but it is not Python 3.10 or newer."
    command -v "$PYTHON"
    return
  fi

  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && check_python3 "$candidate"; then
      command -v "$candidate"
      return
    fi
  done

  fail "Python 3.10 or newer is required. Install a supported Python or set PYTHON to that executable."
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
PYTHON_BIN="$(resolve_python3)"
ARGS=(
  "${SCRIPT_DIR}/scripts/auto_review_installer.py"
  install
  --project-root "${SCRIPT_DIR}"
  --target-root "${TARGET_ROOT}"
  --codex-home "${CODEX_HOME_DIR}"
  --mode "${MODE}"
)

if [[ "$FORCE" -eq 1 ]]; then
  ARGS+=(--force)
fi

"$PYTHON_BIN" "${ARGS[@]}"
