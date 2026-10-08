#!/bin/bash
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -u
PROJECT="${TOKMO_PROJECT_ROOT}"
exec "${TOKMO_SYSTEM_ALERT_JOB:-${HERMES_HOME:-$HOME/.hermes}/scripts/run_system_alert_job.py}" \
  --name "Tokmo SEC targeted collector" -- \
  "$PROJECT/.venv/bin/python" "$PROJECT/tools/run_sec_targeted_operation.py" --home "$HOME"
