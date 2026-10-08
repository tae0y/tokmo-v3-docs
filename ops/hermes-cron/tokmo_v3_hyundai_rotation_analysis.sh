#!/bin/bash
# Hermes no-agent entry. Registration is paused until the coordinated cutover.
# The Python worker enforces the 10:40 KST analysis close and the 30-minute
# approval window after the notice (latest 11:10); the --watch process also
# sends the 5-minute reminder, so 3900s is cleanup headroom past 11:10 only.
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
umask 077
export TZ=Asia/Seoul
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
PROJECT="${TOKMO_ROTATION_ANALYSIS_PROJECT:-${TOKMO_PROJECT_ROOT}}"
PYTHON="${TOKMO_ROTATION_PYTHON:-${TOKMO_PROJECT_ROOT}/.venv/bin/python}"
STATE="${TOKMO_ROTATION_STATE:-${TOKMO_PROJECT_ROOT}/var/state/hyundai-rotation}"
RUNTIME="${TOKMO_ROTATION_ANALYSIS_RUNTIME:-${TOKMO_PROJECT_ROOT}/var/runtime/hyundai-rotation-analysis}"
COMPLETION_SAFE="${TOKMO_COMPLETION_SAFE_CLI:-$HERMES_ROOT/scripts/completion-safe-cli}"
case "${1:-}" in
  "") MODE=watch; TIMEOUT=3900; FLAG=--watch ;;
  --close-only) MODE=close; TIMEOUT=60; FLAG=--close-only ;;
  *) printf 'Unknown analysis wrapper argument\n' >&2; exit 1 ;;
esac
SLOT="$(date -u '+%Y%m%dT%H%M%S')-$MODE-$$"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE" || exit 1
chmod 700 "$RUNTIME" "$BASE" || exit 1
"$COMPLETION_SAFE" \
  --job-id "tokmo-v3-hyundai-rotation-analysis-$SLOT" \
  --stdout "$BASE/stdout.log" --stderr "$BASE/stderr.log" --manifest "$BASE/manifest.json" \
  --cwd "$PROJECT" --timeout "$TIMEOUT" -- \
  "$PYTHON" "$PROJECT/tools/run_hyundai_rotation_analysis.py" \
  --state-dir "$STATE" --quant-entry "$PROJECT/tools/run_quant_decisions.py" "$FLAG" >/dev/null
wrapper_rc=$?
child_rc="$("$PYTHON" - "$BASE/manifest.json" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1]))["child_exit_code"]
    print(value if isinstance(value, int) else 99)
except Exception:
    print(99)
PY
)"
if [ "$wrapper_rc" -eq 0 ] && [ "$child_rc" = "0" ]; then exit 0; fi
printf 'TOKMO_HYUNDAI_ANALYSIS_NEEDS_ATTENTION: wrapper_rc=%s child_rc=%s stdout=%s stderr=%s\n' \
  "$wrapper_rc" "$child_rc" "$BASE/stdout.log" "$BASE/stderr.log"
exit 1
