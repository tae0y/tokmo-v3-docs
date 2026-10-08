#!/bin/bash
# Settlement-only job (v0.4 A1): book yesterday's fills, close finished
# reservations. No decision, no order. KR at 08:40 KST (before the 10:05
# analysis reads the journal); a US variant sets TOKMO_ROTATION_MARKET=US at
# 21:40 KST. The calendar store, not the clock, decides whether the market's
# last regular session closed long enough ago; the cron slot only retries.
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
umask 077

PROJECT="${TOKMO_ROTATION_PROJECT:-${TOKMO_PROJECT_ROOT}}"
RUNTIME="${TOKMO_ROTATION_RUNTIME:-${TOKMO_PROJECT_ROOT}/var/runtime/hyundai-rotation}"
COMPLETION_SAFE="${TOKMO_COMPLETION_SAFE_CLI:-${HERMES_HOME:-$HOME/.hermes}/scripts/completion-safe-cli}"
MARKET="${TOKMO_ROTATION_MARKET:-KR}"
case "$MARKET" in KR|US) ;; *) printf 'tokmo-v3-rotation-settle unknown market\n' >&2; exit 1 ;; esac

source "$HOME/.zshrc" >/dev/null 2>&1
export TELEGRAM_STOCK_BOT_TOKEN="${TELEGRAM_STOCK_BOT:-}"
export TELEGRAM_STOCK_BOT_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

now_sec="$(date '+%S')"
now_sec="${now_sec#0}"
if [ "$now_sec" -lt 15 ]; then
  sleep $((15 - now_sec))
fi

SLOT="$(date -u '+%Y%m%dT%H%M')-settle-$MARKET"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE"
chmod 700 "$RUNTIME" "$BASE"

"$COMPLETION_SAFE" \
  --job-id "tokmo-v3-rotation-settle-$SLOT" \
  --stdout "$BASE/stdout.log" --stderr "$BASE/stderr.log" --manifest "$BASE/manifest.json" \
  --cwd "$PROJECT" --timeout 60 -- \
  "$PROJECT/.venv/bin/python" "$PROJECT/tools/run_hyundai_rotation_daily.py" settle --market "$MARKET" --notify >/dev/null
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
printf 'TOKMO_ROTATION_SETTLE_NEEDS_ATTENTION: market=%s wrapper_rc=%s child_rc=%s stdout=%s\n' "$MARKET" "$wrapper_rc" "$child_rc" "$BASE/stdout.log"
exit 1
