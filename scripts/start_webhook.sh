#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SERVER_PID_FILE="$STATE_DIR/webhook-server.pid"
NGROK_PID_FILE="$STATE_DIR/ngrok.pid"
SERVER_LOG="$STATE_DIR/webhook-server.log"
NGROK_LOG="$STATE_DIR/ngrok.log"
LAST_WEBHOOK_URL_FILE="$STATE_DIR/last-webhook-url.txt"
HOST="${TELECODEXBOT_WEBHOOK_HOST:-127.0.0.1}"
PORT="${TELECODEXBOT_WEBHOOK_PORT:-8765}"
NGROK_WEB_ADDR="${TELECODEXBOT_NGROK_WEB_ADDR:-127.0.0.1:4040}"
NGROK_API_URL="${TELECODEXBOT_NGROK_API_URL:-http://$NGROK_WEB_ADDR/api/tunnels}"
ACK_TEXT="${TELECODEXBOT_WEBHOOK_ACK_TEXT:-Recibido. Lo sumo al inbox de TelecodexBot.}"
NOTIFY_ON_START="${TELECODEXBOT_NOTIFY_WEBHOOK_START:-0}"

ensure_cmd curl
ensure_cmd ngrok

started_server=0
started_ngrok=0

ngrok_supports_web_addr_flag() {
  ngrok http --help 2>/dev/null | grep -q -- '--web-addr'
}

is_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

if ! is_running "$SERVER_PID_FILE"; then
  setsid "$PYTHON_BIN" "$APP_ROOT/scripts/telecodexbot.py" webhook-serve --host "$HOST" --port "$PORT" --ack-text "$ACK_TEXT" >>"$SERVER_LOG" 2>&1 </dev/null &
  echo $! > "$SERVER_PID_FILE"
  started_server=1
fi

for _ in $(seq 1 20); do
  if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
  echo "webhook server no levanto" >&2
  exit 1
fi

if ! is_running "$NGROK_PID_FILE"; then
  NGROK_CMD=(ngrok http "http://$HOST:$PORT" --log=stdout)
  if ngrok_supports_web_addr_flag; then
    NGROK_CMD+=(--web-addr "$NGROK_WEB_ADDR")
  elif [[ -n "${TELECODEXBOT_NGROK_WEB_ADDR:-}" ]]; then
    echo "warning: ngrok no soporta --web-addr, ignoro TELECODEXBOT_NGROK_WEB_ADDR=$TELECODEXBOT_NGROK_WEB_ADDR" >>"$NGROK_LOG"
  fi
  setsid "${NGROK_CMD[@]}" >>"$NGROK_LOG" 2>&1 </dev/null &
  echo $! > "$NGROK_PID_FILE"
  started_ngrok=1
fi

PUBLIC_URL=""
for _ in $(seq 1 30); do
  if PUBLIC_URL_JSON="$(bridge_py ngrok-url --port "$PORT" --api-url "$NGROK_API_URL" 2>/dev/null)"; then
    PUBLIC_URL="$(printf '%s' "$PUBLIC_URL_JSON" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["public_url"])')"
    break
  fi
  sleep 1
done

if [[ -z "$PUBLIC_URL" ]]; then
  echo "ngrok no expuso una URL publica (api_url=$NGROK_API_URL)" >&2
  echo "revisar log: $NGROK_LOG" >&2
  if [[ -f "$NGROK_LOG" ]]; then
    echo "--- tail ngrok.log ---" >&2
    tail -n 40 "$NGROK_LOG" >&2 || true
  fi
  exit 1
fi

TARGET_WEBHOOK_URL="$PUBLIC_URL/telegram"
CURRENT_WEBHOOK_URL="$(bridge_py webhook-info 2>/dev/null | "$PYTHON_BIN" -c 'import json,sys; data=json.load(sys.stdin); print((data.get("info") or {}).get("url",""))' 2>/dev/null || true)"
if [[ "$TARGET_WEBHOOK_URL" != "$CURRENT_WEBHOOK_URL" ]]; then
  bridge_py set-webhook --url "$TARGET_WEBHOOK_URL" >/dev/null
fi
printf '%s' "$TARGET_WEBHOOK_URL" > "$LAST_WEBHOOK_URL_FILE"

if [[ "$NOTIFY_ON_START" == "1" ]] && { [[ "$started_server" -eq 1 ]] || [[ "$started_ngrok" -eq 1 ]]; }; then
  bridge_py send --text "TelecodexBot webhook activo para $WORKSPACE_DIR" >/dev/null || true
fi

printf 'webhook_url=%s\n' "$TARGET_WEBHOOK_URL"
printf 'health_url=http://%s:%s/health\n' "$HOST" "$PORT"
printf 'ngrok_api_url=%s\n' "$NGROK_API_URL"
printf 'server_pid=%s\n' "$(cat "$SERVER_PID_FILE")"
printf 'ngrok_pid=%s\n' "$(cat "$NGROK_PID_FILE")"
printf 'workspace=%s\n' "$WORKSPACE_DIR"
