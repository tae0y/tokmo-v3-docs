#!/bin/bash
# Local read-only observation, no agent/model/broker/Telegram calls.
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
umask 077
export TZ=Asia/Seoul
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
PROJECT="${TOKMO_ROTATION_ANALYSIS_PROJECT:-${TOKMO_PROJECT_ROOT}}"
PYTHON="${TOKMO_ROTATION_PYTHON:-${TOKMO_PROJECT_ROOT}/.venv/bin/python}"
STATE="${TOKMO_ROTATION_STATE:-${TOKMO_PROJECT_ROOT}/var/state/hyundai-rotation}"
DATA="${TOKMO_EVALUATION_DATA_ROOT:-${TOKMO_PROJECT_ROOT}/var/data}"
RUNTIME="${TOKMO_EVALUATION_RUNTIME:-${TOKMO_PROJECT_ROOT}/var/runtime/hyundai-rotation-evaluation}"
COMPLETION_SAFE="${TOKMO_COMPLETION_SAFE_CLI:-$HERMES_ROOT/scripts/completion-safe-cli}"
SLOT="$(date -u '+%Y%m%dT%H%M%S')-$$"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE" || exit 1
chmod 700 "$RUNTIME" "$BASE" || exit 1
"$COMPLETION_SAFE" \
  --job-id "tokmo-v3-hyundai-rotation-evaluation-$SLOT" \
  --stdout "$BASE/stdout.log" --stderr "$BASE/stderr.log" --manifest "$BASE/manifest.json" \
  --cwd "$PROJECT" --timeout 60 -- \
  "$PYTHON" "$PROJECT/tools/run_hyundai_rotation_evaluation.py" \
  --state-dir "$STATE" --data-root "$DATA" >/dev/null
wrapper_rc=$?
child_rc="$("$PYTHON" - "$BASE/manifest.json" <<'PY'
import json, sys
try:
    value=json.load(open(sys.argv[1]))['child_exit_code']
    print(value if isinstance(value,int) else 99)
except Exception:print(99)
PY
)"
if [ "$wrapper_rc" -eq 0 ] && [ "$child_rc" = "0" ]; then exit 0; fi
printf 'TOKMO_ROTATION_EVALUATION_NEEDS_ATTENTION: wrapper_rc=%s child_rc=%s stdout=%s stderr=%s\n' \
  "$wrapper_rc" "$child_rc" "$BASE/stdout.log" "$BASE/stderr.log"
exit 1
