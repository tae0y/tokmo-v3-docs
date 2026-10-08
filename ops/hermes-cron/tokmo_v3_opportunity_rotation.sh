#!/bin/bash
# Approved PAPER only; source/auth collection belongs to the existing Pulse.
set -euo pipefail
umask 077
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNTIME="$PROJECT/var/runtime/opportunity-rotation"
mkdir -p "$RUNTIME"
SLOT="$(date -u '+%Y%m%dT%H%M%SZ')"
OUT="$RUNTIME/$SLOT.json"
ERR="$RUNTIME/$SLOT.stderr"
export PYTHONPATH="$PROJECT/src"
if "$PROJECT/.venv/bin/python" "$PROJECT/tools/run_opportunity_rotation.py" step >"$OUT" 2>"$ERR"; then
  # Stdout is the cron message: one summary per newly applied slot, else empty.
  NOTICE_ERR="$RUNTIME/$SLOT.notice.stderr"
  if "$PROJECT/.venv/bin/python" "$PROJECT/tools/run_opportunity_result_notice.py" 2>"$NOTICE_ERR"; then
    exit 0
  fi
  printf 'TOKMO_PAPER_NOTICE_NEEDS_ATTENTION error=%s\n' "$NOTICE_ERR"
  exit 1
fi
printf 'TOKMO_PAPER_NEEDS_ATTENTION output=%s error=%s\n' "$OUT" "$ERR"
exit 1
