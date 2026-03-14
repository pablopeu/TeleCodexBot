#!/usr/bin/env bash
set -euo pipefail

CODEX_BIN="${TELECODEXBOT_CODEX_BIN:-codex}"
CODEX_ARGS=()

if [[ "${TELECODEXBOT_DISABLE_PASTE_BURST:-1}" == "1" ]]; then
  CODEX_ARGS+=(-c disable_paste_burst=true)
fi

if [[ "${TELECODEXBOT_RESUME_LAST:-1}" == "1" ]] && "$CODEX_BIN" resume "${CODEX_ARGS[@]}" --last; then
  exit 0
fi

exec "$CODEX_BIN" "${CODEX_ARGS[@]}"
