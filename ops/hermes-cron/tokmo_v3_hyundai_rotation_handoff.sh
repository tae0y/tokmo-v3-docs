#!/bin/bash
# PAUSED ARTIFACT — the v0.4 execution entry. Install only at cutover, and
# remove tokmo-v3-hyundai-rotation-daily (10:12 GLM path) in the same step;
# never run both. Schedule 5 10 * * 1-5 (Asia/Seoul): the runner polls the
# analysis decision file from 10:05, executes in the next token-safe window
# once it is consumable, and ends the day otherwise: analysis must complete by
# 10:40, and a delivered proposal stays approvable for 30 minutes after its
# notice (file valid_until, latest 11:10). The wrapper timeout (65 min) is only
# cleanup headroom; the business deadlines are enforced by the Python side.
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
umask 077

PROJECT="${TOKMO_ROTATION_PROJECT:-${TOKMO_PROJECT_ROOT}}"
RUNTIME="${TOKMO_ROTATION_RUNTIME:-${TOKMO_PROJECT_ROOT}/var/runtime/hyundai-rotation}"
COMPLETION_SAFE="${TOKMO_COMPLETION_SAFE_CLI:-${HERMES_HOME:-$HOME/.hermes}/scripts/completion-safe-cli}"
MARKET="${TOKMO_ROTATION_MARKET:-KR}"
DEADLINE="${TOKMO_ROTATION_DEADLINE:-11:10}"
case "$MARKET" in KR|US) ;; *) printf 'tokmo-v3-hyundai-rotation-handoff unknown market\n' >&2; exit 1 ;; esac

source "$HOME/.zshrc" >/dev/null 2>&1
export TELEGRAM_STOCK_BOT_TOKEN="${TELEGRAM_STOCK_BOT:-}"
export TELEGRAM_STOCK_BOT_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

SLOT="$(date -u '+%Y%m%dT%H%M')-handoff-$MARKET"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE"
chmod 700 "$RUNTIME" "$BASE"

"$COMPLETION_SAFE" \
  --job-id "tokmo-v3-hyundai-rotation-handoff-$SLOT" \
  --stdout "$BASE/stdout.log" --stderr "$BASE/stderr.log" --manifest "$BASE/manifest.json" \
  --cwd "$PROJECT" --timeout 3900 -- \
  "$PROJECT/.venv/bin/python" "$PROJECT/tools/run_hyundai_rotation_daily.py" run-file --market "$MARKET" --deadline "$DEADLINE" --notify >/dev/null
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
printf 'TOKMO_HYUNDAI_ROTATION_HANDOFF_NEEDS_ATTENTION: wrapper_rc=%s child_rc=%s stdout=%s\n' "$wrapper_rc" "$child_rc" "$BASE/stdout.log"
exit 1
