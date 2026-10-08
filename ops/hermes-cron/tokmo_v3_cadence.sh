#!/bin/bash
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
PROJECT="${TOKMO_PROJECT_ROOT}"
RUNTIME="${TOKMO_PROJECT_ROOT}/var/runtime/cadence"
SLOT="$(date -u '+%Y%m%dT%H%M')"
BASE="$RUNTIME/$SLOT"
mkdir -p "$BASE"
chmod 700 "$RUNTIME" "$BASE"
OUT="$BASE/stdout.log"
ERR="$BASE/stderr.log"
MANIFEST="$BASE/manifest.json"

"${TOKMO_COMPLETION_SAFE_CLI:-${HERMES_HOME:-$HOME/.hermes}/scripts/completion-safe-cli}" \
  --job-id "tokmo-v3-cadence-$SLOT" \
  --stdout "$OUT" --stderr "$ERR" --manifest "$MANIFEST" \
  --cwd "$PROJECT" --timeout 270 -- \
  "$PROJECT/.venv/bin/python" "$PROJECT/tools/run_tokmo_cadence.py" >/dev/null
wrapper_rc=$?

child_rc="$(/usr/bin/python3 - "$MANIFEST" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1]))["child_exit_code"])
except Exception:
    print(99)
PY
)"
STATE="$RUNTIME/alert-state.json"
GATE="$PROJECT/tools/cadence_alert_gate.py"
if [ "$wrapper_rc" -ne 0 ] || [ "$child_rc" != "0" ]; then
  signature="$(/usr/bin/python3 - "$ERR" "$OUT" "$wrapper_rc" "$child_rc" <<'PY'
import json, re, sys
for path in sys.argv[1:3]:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    match = re.search(r"tokmo-cadence (?:refusal|failure)=([A-Za-z0-9_]+)", text)
    if match:
        print(f"reason={match.group(1)}|markets=all")
        raise SystemExit
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        reason = payload.get("reason") or payload.get("outcome") or "unknown"
        reason = re.sub(r"[^A-Za-z0-9_]", "_", str(reason))
        markets = payload.get("open_markets") or ["none"]
        markets = ",".join(sorted(str(item) for item in markets))
        markets = re.sub(r"[^A-Za-z0-9_,]", "_", markets)
        print(f"reason={reason}|markets={markets}")
        raise SystemExit
print(f"reason=wrapper_{sys.argv[3]}_child_{sys.argv[4]}|markets=unknown")
PY
)"
  fingerprint="task=tokmo-cadence|$signature"
  gate_output="$("$PROJECT/.venv/bin/python" "$GATE" --state "$STATE" \
    failure --fingerprint "$fingerprint" \
    --detail "wrapper_rc=$wrapper_rc child_rc=$child_rc $signature")"
  gate_rc=$?
  if [ "$gate_rc" -eq 0 ]; then
    exit 0
  fi
  if [ "$gate_rc" -eq 10 ]; then
    printf '%s\n' "$gate_output"
  else
    printf 'TOKMO_CADENCE_FAILED: wrapper_rc=%s child_rc=%s signature=%s alert_gate_rc=%s\n' \
      "$wrapper_rc" "$child_rc" "$signature" "$gate_rc"
  fi
  exit 1
fi

gate_output="$("$PROJECT/.venv/bin/python" "$GATE" --state "$STATE" success)"
gate_rc=$?
if [ "$gate_rc" -eq 10 ]; then
  printf '%s\n' "$gate_output"
elif [ "$gate_rc" -ne 0 ]; then
  printf 'TOKMO_CADENCE_RECOVERY_STATE_FAILED: alert_gate_rc=%s\n' "$gate_rc"
  exit 1
fi
exit 0
