#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

TMUX_TARGET_FILE="$STATE_DIR/tmux-target.txt"
SESSION_NAME="${TELECODEXBOT_SESSION_NAME:-telecodexbot-$WORKSPACE_KEY}"
WINDOW_NAME="${TELECODEXBOT_WINDOW_NAME:-codex}"
CODEX_CMD="${TELECODEXBOT_CODEX_CMD:-$APP_ROOT/scripts/launch_codex.sh}"
ATTACH_MODE="${TELECODEXBOT_ATTACH:-1}"
STOP_SERVICE="${TELECODEXBOT_STOP_SERVICE:-1}"

ensure_cmd tmux
if [[ -z "$CODEX_BIN" ]]; then
  echo "codex no esta instalado o no esta en PATH" >&2
  exit 1
fi

if [[ "$STOP_SERVICE" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop telecodexbot.service >/dev/null 2>&1 || true
fi

"$APP_ROOT/scripts/stop_relay.sh" >/dev/null 2>&1 || true
"$APP_ROOT/scripts/stop_webhook.sh" >/dev/null 2>&1 || true

printf -v startup_cmd 'cd %q && TELECODEXBOT_CODEX_BIN=%q TELECODEXBOT_WORKSPACE_DIR=%q exec %q' "$WORKSPACE_DIR" "$CODEX_BIN" "$WORKSPACE_DIR" "$CODEX_CMD"

window_exists() {
  tmux list-windows -t "$SESSION_NAME" -F '#{window_name}' 2>/dev/null | grep -Fxq "$WINDOW_NAME"
}

resolve_pane_id() {
  tmux list-panes -t "$SESSION_NAME:$WINDOW_NAME" -F '#{pane_id}' 2>/dev/null | head -n 1 || true
}

ensure_window() {
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux new-session -d -s "$SESSION_NAME" -n "$WINDOW_NAME"
    return
  fi
  if ! window_exists; then
    tmux new-window -d -t "$SESSION_NAME" -n "$WINDOW_NAME"
  fi
}

respawn_window() {
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux new-session -d -s "$SESSION_NAME" -n "$WINDOW_NAME"
    return
  fi
  if window_exists; then
    tmux respawn-window -k -t "$SESSION_NAME:$WINDOW_NAME"
  else
    tmux new-window -d -t "$SESSION_NAME" -n "$WINDOW_NAME"
  fi
}

ensure_window
pane_id="$(resolve_pane_id)"
if [[ -z "$pane_id" ]]; then
  respawn_window
  sleep 1
  pane_id="$(resolve_pane_id)"
fi
if [[ -z "$pane_id" ]]; then
  echo "no se pudo resolver el pane de Codex en $SESSION_NAME:$WINDOW_NAME" >&2
  exit 1
fi

current_command="$(tmux display-message -p -t "$pane_id" '#{pane_current_command}' 2>/dev/null || true)"
pane_dead="$(tmux display-message -p -t "$pane_id" '#{pane_dead}' 2>/dev/null || true)"
if [[ -z "$current_command" || -z "$pane_dead" || "$pane_dead" == "1" ]]; then
  respawn_window
  sleep 1
  pane_id="$(resolve_pane_id)"
  current_command="$(tmux display-message -p -t "$pane_id" '#{pane_current_command}' 2>/dev/null || true)"
fi
if [[ "$current_command" =~ ^(bash|zsh|fish|sh)$ ]]; then
  tmux send-keys -t "$pane_id" C-c
  tmux send-keys -t "$pane_id" "$startup_cmd" Enter
  sleep 1
fi

printf '%s\n' "$pane_id" > "$TMUX_TARGET_FILE"

TELECODEXBOT_TMUX_TARGET="$pane_id" \
TELECODEXBOT_AUTONOMOUS="${TELECODEXBOT_AUTONOMOUS:-0}" \
TELECODEXBOT_TMUX_INJECT="${TELECODEXBOT_TMUX_INJECT:-1}" \
TELECODEXBOT_ENSURE_WEBHOOK=1 \
  "$APP_ROOT/scripts/start_relay.sh" >/dev/null

printf 'tmux_session=%s\n' "$SESSION_NAME"
printf 'tmux_window=%s\n' "$WINDOW_NAME"
printf 'tmux_target=%s\n' "$pane_id"
printf 'workspace=%s\n' "$WORKSPACE_DIR"
printf 'state_dir=%s\n' "$STATE_DIR"

if [[ "$ATTACH_MODE" != "1" ]]; then
  exit 0
fi

if [[ -n "${TMUX:-}" ]]; then
  exec tmux switch-client -t "$SESSION_NAME"
else
  exec tmux attach-session -t "$SESSION_NAME"
fi
