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
NGROK_POOLING_ON_CONFLICT="${TELECODEXBOT_NGROK_POOLING_ON_CONFLICT:-1}"
NGROK_ALT_URL_ON_CONFLICT="${TELECODEXBOT_NGROK_ALT_URL_ON_CONFLICT:-1}"
ACK_TEXT="${TELECODEXBOT_WEBHOOK_ACK_TEXT:-Recibido. Lo sumo al inbox de TelecodexBot.}"
NOTIFY_ON_START="${TELECODEXBOT_NOTIFY_WEBHOOK_START:-0}"

ensure_cmd curl
ensure_cmd ngrok

started_server=0
started_ngrok=0

ngrok_supports_web_addr_flag() {
  ngrok http --help 2>/dev/null | grep -q -- '--web-addr'
}

ngrok_supports_pooling_flag() {
  ngrok http --help 2>/dev/null | grep -q -- '--pooling-enabled'
}

ngrok_supports_url_flag() {
  ngrok http --help 2>/dev/null | grep -q -- '--url'
}

is_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

kill_from_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 0
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
}

start_ngrok_agent() {
  local pooling_enabled="$1"
  local alt_url="${2:-}"
  NGROK_CMD=(ngrok http "http://$HOST:$PORT" --log=stdout)
  if [[ "$pooling_enabled" == "1" ]]; then
    NGROK_CMD+=(--pooling-enabled)
  fi
  if [[ -n "$alt_url" ]]; then
    NGROK_CMD+=(--url "$alt_url")
  fi
  if ngrok_supports_web_addr_flag; then
    NGROK_CMD+=(--web-addr "$NGROK_WEB_ADDR")
  elif [[ -n "${TELECODEXBOT_NGROK_WEB_ADDR:-}" ]]; then
    echo "warning: ngrok no soporta --web-addr, ignoro TELECODEXBOT_NGROK_WEB_ADDR=$TELECODEXBOT_NGROK_WEB_ADDR" >>"$NGROK_LOG"
  fi
  setsid "${NGROK_CMD[@]}" >>"$NGROK_LOG" 2>&1 </dev/null &
  echo $! > "$NGROK_PID_FILE"
  started_ngrok=1
}

poll_public_url() {
  local public_url=""
  for _ in $(seq 1 30); do
    if PUBLIC_URL_JSON="$(bridge_py ngrok-url --port "$PORT" --api-url "$NGROK_API_URL" 2>/dev/null)"; then
      public_url="$(printf '%s' "$PUBLIC_URL_JSON" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["public_url"])')"
      break
    fi
    sleep 1
  done
  printf '%s' "$public_url"
}

generate_alt_ngrok_url() {
  local suffix
  suffix="$(printf '%s-%s' "$WORKSPACE_KEY" "$RANDOM" | tr -cd 'a-z0-9-' | cut -c1-40)"
  printf 'https://telecodexbot-%s.ngrok-free.app' "$suffix"
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

PUBLIC_URL=""
# Reuse an already-running ngrok agent when possible to avoid endpoint conflicts.
if PUBLIC_URL_JSON="$(bridge_py ngrok-url --port "$PORT" --api-url "$NGROK_API_URL" 2>/dev/null)"; then
  PUBLIC_URL="$(printf '%s' "$PUBLIC_URL_JSON" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["public_url"])')"
fi

if [[ -z "$PUBLIC_URL" ]] && ! is_running "$NGROK_PID_FILE"; then
  : >"$NGROK_LOG"
  start_ngrok_agent "0"
fi

if [[ -z "$PUBLIC_URL" ]]; then
  PUBLIC_URL="$(poll_public_url)"
fi

if [[ -z "$PUBLIC_URL" ]] && [[ "$started_ngrok" -eq 1 ]] && [[ "$NGROK_POOLING_ON_CONFLICT" == "1" ]] && [[ -f "$NGROK_LOG" ]] && grep -q 'ERR_NGROK_334' "$NGROK_LOG"; then
  if ngrok_supports_pooling_flag; then
    echo "info: reintentando ngrok con --pooling-enabled por conflicto ERR_NGROK_334" >>"$NGROK_LOG"
    kill_from_file "$NGROK_PID_FILE"
    : >"$NGROK_LOG"
    start_ngrok_agent "1"
    PUBLIC_URL="$(poll_public_url)"
  fi
fi

if [[ -z "$PUBLIC_URL" ]] && [[ "$started_ngrok" -eq 1 ]] && [[ "$NGROK_ALT_URL_ON_CONFLICT" == "1" ]] && [[ -f "$NGROK_LOG" ]] && grep -q 'ERR_NGROK_334' "$NGROK_LOG"; then
  if ngrok_supports_url_flag; then
    for _ in $(seq 1 3); do
      ALT_URL="$(generate_alt_ngrok_url)"
      echo "info: reintentando ngrok con URL alternativa $ALT_URL" >>"$NGROK_LOG"
      kill_from_file "$NGROK_PID_FILE"
      : >"$NGROK_LOG"
      start_ngrok_agent "0" "$ALT_URL"
      PUBLIC_URL="$(poll_public_url)"
      if [[ -n "$PUBLIC_URL" ]]; then
        break
      fi
      if [[ ! -f "$NGROK_LOG" ]] || ! grep -q 'ERR_NGROK_334' "$NGROK_LOG"; then
        break
      fi
    done
  fi
fi

if [[ -z "$PUBLIC_URL" ]]; then
  echo "ngrok no expuso una URL publica (api_url=$NGROK_API_URL)" >&2
  if [[ -f "$NGROK_LOG" ]] && grep -q 'ERR_NGROK_334' "$NGROK_LOG"; then
    echo "ngrok reporta endpoint ocupado (ERR_NGROK_334)." >&2
    echo "si tenes otro ngrok activo, detenelo y reintenta (ej: pkill -x ngrok)." >&2
  fi
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
