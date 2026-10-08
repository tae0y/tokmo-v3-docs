#!/bin/bash
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -euo pipefail
exec "${TOKMO_SYSTEM_ALERT_JOB:-${HERMES_HOME:-$HOME/.hermes}/scripts/run_system_alert_job.py}" \
  --name "Tokmo OpenDART collector" -- \
  ${TOKMO_PROJECT_ROOT}/.venv/bin/python \
  ${TOKMO_PROJECT_ROOT}/tools/run_opendart_operation.py \
  --home "$HOME"
