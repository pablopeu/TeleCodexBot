#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="${TELECODEXBOT_WORKSPACE_DIR:-${PWD}}"
WORKSPACE_DIR="$(cd "$WORKSPACE_DIR" && pwd)"
CONFIG_HOME="${TELECODEXBOT_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/telecodexbot}"
STATE_HOME="${TELECODEXBOT_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/telecodexbot}"
PYTHON_BIN="${TELECODEXBOT_PYTHON_BIN:-python3}"
CODEX_BIN="${TELECODEXBOT_CODEX_BIN:-$(command -v codex 2>/dev/null || true)}"
if command -v sha256sum >/dev/null 2>&1; then
  WORKSPACE_KEY="$(printf '%s' "$WORKSPACE_DIR" | sha256sum | awk '{print substr($1,1,16)}')"
elif command -v shasum >/dev/null 2>&1; then
  WORKSPACE_KEY="$(printf '%s' "$WORKSPACE_DIR" | shasum -a 256 | awk '{print substr($1,1,16)}')"
else
  echo "necesitas sha256sum o shasum para usar telecodexbot" >&2
  exit 1
fi
STATE_DIR="${TELECODEXBOT_STATE_DIR:-$STATE_HOME/$WORKSPACE_KEY}"

mkdir -p "$CONFIG_HOME" "$STATE_DIR"
printf '%s\n' "$WORKSPACE_DIR" > "$STATE_DIR/workspace-path.txt"

export TELECODEXBOT_APP_ROOT="$APP_ROOT"
export TELECODEXBOT_WORKSPACE_DIR="$WORKSPACE_DIR"
export TELECODEXBOT_CONFIG_DIR="$CONFIG_HOME"
export TELECODEXBOT_STATE_HOME="$STATE_HOME"
export TELECODEXBOT_STATE_DIR="$STATE_DIR"

ensure_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "$1 no esta instalado o no esta en PATH" >&2
    exit 1
  fi
}

bridge_py() {
  "$PYTHON_BIN" "$APP_ROOT/scripts/telecodexbot.py" "$@"
}
