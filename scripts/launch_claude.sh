#!/usr/bin/env bash
set -euo pipefail

CLAUDE_BIN="${TELECODEXBOT_CLAUDE_BIN:-claude}"
CLAUDE_ARGS=()

if [[ "${TELECODEXBOT_RESUME_LAST:-1}" == "1" ]]; then
  exec "$CLAUDE_BIN" --continue "${CLAUDE_ARGS[@]}"
else
  exec "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}"
fi
