#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

PID_FILE="$STATE_DIR/relay-daemon.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "relay no estaba activo"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  kill "$pid" 2>/dev/null || true
fi
rm -f "$PID_FILE"

echo "relay detenido"
