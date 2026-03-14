#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

PID_FILE="$STATE_DIR/relay-daemon.pid"
LOG_FILE="$STATE_DIR/relay-daemon.log"
TMUX_TARGET_FILE="$STATE_DIR/tmux-target.txt"
AUTONOMOUS_MODE="${TELECODEXBOT_AUTONOMOUS:-0}"
ENSURE_WEBHOOK="${TELECODEXBOT_ENSURE_WEBHOOK:-1}"
TMUX_INJECT="${TELECODEXBOT_TMUX_INJECT:-1}"
TMUX_TARGET="${TELECODEXBOT_TMUX_TARGET:-}"
TMUX_COMMAND="${TELECODEXBOT_TMUX_COMMAND:-codex}"
TMUX_ENTER="${TELECODEXBOT_TMUX_ENTER:-1}"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

if is_running; then
  echo "relay ya activo (pid=$(cat "$PID_FILE"))"
  exit 0
fi

if [[ "$ENSURE_WEBHOOK" == "1" ]]; then
  "$APP_ROOT/scripts/start_webhook.sh" >/dev/null
fi

if [[ -z "$CODEX_BIN" ]]; then
  echo "codex no esta instalado o no esta en PATH" >&2
  exit 1
fi

RELAY_ARGS=(relay-daemon --from-now --codex-cmd "$CODEX_BIN")
if [[ "$AUTONOMOUS_MODE" != "1" ]]; then
  RELAY_ARGS+=(--no-codex-resume)
fi
if [[ "$TMUX_INJECT" == "1" ]]; then
  if [[ -z "$TMUX_TARGET" && -n "${TMUX:-}" ]]; then
    TMUX_TARGET="$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)"
  fi
  if [[ -z "$TMUX_TARGET" && -f "$TMUX_TARGET_FILE" ]]; then
    TMUX_TARGET="$(cat "$TMUX_TARGET_FILE" 2>/dev/null || true)"
  fi
  RELAY_ARGS+=(--tmux-inject --tmux-command "$TMUX_COMMAND")
  if [[ -n "$TMUX_TARGET" ]]; then
    RELAY_ARGS+=(--tmux-target "$TMUX_TARGET")
  fi
  if [[ "$TMUX_ENTER" != "1" ]]; then
    RELAY_ARGS+=(--no-tmux-enter)
  fi
fi

setsid "$PYTHON_BIN" -u "$APP_ROOT/scripts/telecodexbot.py" "${RELAY_ARGS[@]}" "$@" >>"$LOG_FILE" 2>&1 </dev/null &
echo $! > "$PID_FILE"

sleep 1
if ! is_running; then
  echo "relay no levanto, revisa $LOG_FILE" >&2
  exit 1
fi

printf 'relay_pid=%s\n' "$(cat "$PID_FILE")"
printf 'relay_log=%s\n' "$LOG_FILE"
printf 'workspace=%s\n' "$WORKSPACE_DIR"
