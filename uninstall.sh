#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
TARGET_ROOT="${CODEX_HOME_DIR}"
KEEP_FILES=0

usage() {
  cat <<'USAGE'
Usage: ./uninstall.sh [--target-root DIR] [--keep-files]

Removes the auto-review Codex plugin registration, marketplace entry, installed
plugin/skill files, plugin cache, legacy global hooks, and stale trust/config
entries.

Options:
  --target-root install root. Default: ${CODEX_HOME:-$HOME/.codex}
  --keep-files   remove registrations and hook entries, but keep plugin/skill files or links
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$(resolve_python3)"
ARGS=(
  "${SCRIPT_DIR}/scripts/auto_review_installer.py"
  uninstall
  --project-root "${SCRIPT_DIR}"
  --target-root "${TARGET_ROOT}"
  --codex-home "${CODEX_HOME_DIR}"
)

if [[ "$KEEP_FILES" -eq 1 ]]; then
  ARGS+=(--keep-files)
fi

"$PYTHON_BIN" "${ARGS[@]}"
