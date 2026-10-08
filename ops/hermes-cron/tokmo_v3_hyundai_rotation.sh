#!/bin/bash
# PAUSED ARTIFACT: installation requires the owner's explicit step
# (see docs/hyundai-rotation-automation-plan-20260911.md §4).
# This repository copy is the source of truth for the installed wrapper.
#
# Daily unattended rotation cycle, one market per job. KR runs at 10:12
# Asia/Seoul on weekdays (off the five-minute cadence grid); a US job would
# set TOKMO_ROTATION_MARKET=US at 23:42 KST (inside the New York regular
# session in both EST and EDT). The launch waits past the :00-:05 pulse tick
# because a freshly issued broker bearer invalidates the previous one; the
# Python side refuses to start outside seconds 12..40 of the minute and,
# before any token, checks the cadence-collected calendar store for a
# regular session — holidays and weekends end there with no broker call.
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
umask 077

PROJECT="${TOKMO_ROTATION_PROJECT:-${TOKMO_PROJECT_ROOT}}"
RUNTIME="${TOKMO_ROTATION_RUNTIME:-${TOKMO_PROJECT_ROOT}/var/runtime/hyundai-rotation}"
COMPLETION_SAFE="${TOKMO_COMPLETION_SAFE_CLI:-${HERMES_HOME:-$HOME/.hermes}/scripts/completion-safe-cli}"
MARKET="${TOKMO_ROTATION_MARKET:-KR}"
case "$MARKET" in KR|US) ;; *) printf 'tokmo-v3-hyundai-rotation unknown market\n' >&2; exit 1 ;; esac

# Stock-bot Telegram credentials come from the user's shell-private source and
# are exported for the child only; never printed or written.
source "$HOME/.zshrc" >/dev/null 2>&1
export TELEGRAM_STOCK_BOT_TOKEN="${TELEGRAM_STOCK_BOT:-}"
export TELEGRAM_STOCK_BOT_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

now_sec="$(date '+%S')"
now_sec="${now_sec#0}"
if [ "$now_sec" -lt 15 ]; then
  sleep $((15 - now_sec))
fi

SLOT="$(date -u '+%Y%m%dT%H%M')-$MARKET"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE"
chmod 700 "$RUNTIME" "$BASE"
OUT="$BASE/stdout.log"
ERR="$BASE/stderr.log"
MANIFEST="$BASE/manifest.json"

"$COMPLETION_SAFE" \
  --job-id "tokmo-v3-hyundai-rotation-$SLOT" \
  --stdout "$OUT" --stderr "$ERR" --manifest "$MANIFEST" \
  --cwd "$PROJECT" --timeout 45 -- \
  "$PROJECT/.venv/bin/python" "$PROJECT/tools/run_hyundai_rotation_daily.py" run --notify --market "$MARKET" >/dev/null
wrapper_rc=$?

child_rc="$(/usr/bin/python3 - "$MANIFEST" <<'PY'
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
printf 'TOKMO_HYUNDAI_ROTATION_NEEDS_ATTENTION: wrapper_rc=%s child_rc=%s stdout=%s stderr=%s\n' \
  "$wrapper_rc" "$child_rc" "$OUT" "$ERR"
exit 1
