#!/bin/zsh
# PAUSED ARTIFACT: installation and activation require separate approval.
# This repository copy is the source of truth for any later paused install.
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -euo pipefail
umask 077

# Reuse the existing TokmoStock_BOT credential contract. Values remain in
# the user's shell-private source and are never printed or copied to disk.
source "$HOME/.zshrc" >/dev/null 2>&1
export TELEGRAM_STOCK_BOT_TOKEN="${TELEGRAM_STOCK_BOT:-}"
export TELEGRAM_STOCK_BOT_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

PROJECT="${TOKMO_PULSE_PROJECT:-${TOKMO_PROJECT_ROOT}}"
PYTHON="${TOKMO_PULSE_PYTHON:-${TOKMO_PROJECT_ROOT}/.venv/bin/python}"
RUNTIME="${TOKMO_PULSE_RUNTIME:-${TOKMO_PROJECT_ROOT}/var/runtime/pulse}"
COMPLETION_SAFE="${TOKMO_COMPLETION_SAFE_CLI:-${HERMES_HOME:-$HOME/.hermes}/scripts/completion-safe-cli}"
ALERT_SCRIPT="${TOKMO_SYSTEM_ALERT_SCRIPT:-${HERMES_HOME:-$HOME/.hermes}/scripts/send_system_alert.sh}"
SUFFIX="${TOKMO_PULSE_SLOT_SUFFIX:-}"

case "$SUFFIX" in
  *[!A-Za-z0-9._-]*)
    printf 'tokmo-v3-pulse unsafe slot suffix\n' >&2
    exit 1
    ;;
esac

SLOT="$(date -u '+%Y%m%dT%H%M')${SUFFIX}"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE"
chmod 700 "$RUNTIME" "$BASE"
OUT="$BASE/stdout.log"
ERR="$BASE/stderr.log"
MANIFEST="$BASE/manifest.json"
LAUNCHER="$BASE/launcher.log"
: >"$LAUNCHER"
chmod 600 "$LAUNCHER"

if "$COMPLETION_SAFE" \
  --job-id "tokmo-v3-pulse-$SLOT" \
  --stdout "$OUT" --stderr "$ERR" --manifest "$MANIFEST" \
  --cwd "$PROJECT" --timeout 40 -- \
  env TOKMO_QUANT_INPUT_CAPTURE=1 TOKMO_OPPORTUNITY_CAPTURE=1 PYTHONPATH="$PROJECT/src" "$PYTHON" "$PROJECT/tools/run_tokmo_pulse.py" \
  >"$LAUNCHER" 2>&1; then
  wrapper_rc=0
else
  wrapper_rc=$?
fi

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

fingerprint="${wrapper_rc}:${child_rc}"
state="$RUNTIME/.alert_fingerprint_state"
now_epoch="${TOKMO_PULSE_NOW_EPOCH:-$(date '+%s')}"
previous_fingerprint=""
previous_epoch=0
if [ -f "$state" ]; then
  IFS=' ' read -r previous_fingerprint previous_epoch <"$state" || true
fi

send_alert=0
if [ "$fingerprint" != "$previous_fingerprint" ]; then
  send_alert=1
elif [ $((now_epoch - previous_epoch)) -ge 900 ]; then
  send_alert=1
fi

if [ "$send_alert" -eq 1 ]; then
  printf '%s %s\n' "$fingerprint" "$now_epoch" >"$state"
  chmod 600 "$state"
  launcher_head="$(/usr/bin/sed -n '1,5p' "$LAUNCHER" | \
    /usr/bin/sed -E 's/([Tt]oken|[Pp]assword|[Ss]ecret|[Kk]ey)=([^ ]+)/\1=[REDACTED]/g' | \
    /usr/bin/cut -c 1-240)"
  alert_message="tokmo-v3-pulse failure wrapper_rc=$wrapper_rc child_rc=$child_rc
manifest=$MANIFEST stdout=$OUT stderr=$ERR launcher=$LAUNCHER
launcher_head:
$launcher_head"
  if [ -x "$ALERT_SCRIPT" ]; then
    "$ALERT_SCRIPT" "$alert_message" || true
  fi
fi

exit 1
