#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SERVER_PID_FILE="$STATE_DIR/webhook-server.pid"
NGROK_PID_FILE="$STATE_DIR/ngrok.pid"
LAST_WEBHOOK_URL_FILE="$STATE_DIR/last-webhook-url.txt"

kill_from_file() {
  local pid_file="$1"
  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
}

bridge_py delete-webhook >/dev/null || true
kill_from_file "$NGROK_PID_FILE"
kill_from_file "$SERVER_PID_FILE"
rm -f "$LAST_WEBHOOK_URL_FILE"

echo "webhook detenido"
