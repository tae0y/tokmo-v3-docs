#!/bin/bash
# One-off rehearsal wrapper for 2026-09-14/15 (Asia/Seoul). Five cron entries
# call this; the Python side picks the stage by the clock and never repeats
# one. Waits past the :00-:05 pulse tick like the production wrapper.
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
umask 077

PROJECT="${TOKMO_PROJECT_ROOT}"
RUNTIME="${TOKMO_PROJECT_ROOT}/var/runtime/hyundai-rotation-rehearsal"
COMPLETION_SAFE="${TOKMO_COMPLETION_SAFE_CLI:-${HERMES_HOME:-$HOME/.hermes}/scripts/completion-safe-cli}"

source "$HOME/.zshrc" >/dev/null 2>&1
export TELEGRAM_STOCK_BOT_TOKEN="${TELEGRAM_STOCK_BOT:-}"
export TELEGRAM_STOCK_BOT_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

now_sec="$(date '+%S')"
now_sec="${now_sec#0}"
if [ "$now_sec" -lt 15 ]; then
  sleep $((15 - now_sec))
fi

SLOT="$(date -u '+%Y%m%dT%H%M')"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE"
chmod 700 "$RUNTIME" "$BASE"

"$COMPLETION_SAFE" \
  --job-id "tokmo-v3-rotation-rehearsal-$SLOT" \
  --stdout "$BASE/stdout.log" --stderr "$BASE/stderr.log" --manifest "$BASE/manifest.json" \
  --cwd "$PROJECT" --timeout 170 -- \
  "$PROJECT/.venv/bin/python" "$PROJECT/tools/rotation_rehearsal.py" auto --notify --go-live-if-clean >/dev/null
wrapper_rc=$?
child_rc="$(/usr/bin/python3 - "$BASE/manifest.json" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1]))["child_exit_code"]
    print(value if isinstance(value, int) else 99)
except Exception:
    print(99)
PY
)"
if [ "$wrapper_rc" -eq 0 ] && [ "$child_rc" = "0" ]; then
  exit 0
fi
printf 'TOKMO_ROTATION_REHEARSAL_NEEDS_ATTENTION: wrapper_rc=%s child_rc=%s stdout=%s\n' "$wrapper_rc" "$child_rc" "$BASE/stdout.log"
exit 1
