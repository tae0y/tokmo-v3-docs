#!/bin/bash
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -euo pipefail
umask 077

PROJECT="${TOKMO_YOUTUBE_WEEKLY_PROJECT:-${TOKMO_PROJECT_ROOT}}"
DB="${TOKMO_YOUTUBE_WEEKLY_DB:-${TOKMO_PROJECT_ROOT}/var/data/ops/ops_kernel.sqlite3}"
RUNTIME="${TOKMO_YOUTUBE_WEEKLY_RUNTIME:-${TOKMO_PROJECT_ROOT}/var/runtime/youtube-weekly-review}"
NOW="${TOKMO_YOUTUBE_WEEKLY_NOW:-$(/bin/date -u '+%Y-%m-%dT%H:%M:%S+00:00')}"
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"
WEEK="$($PROJECT/.venv/bin/python - "$NOW" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
now=datetime.fromisoformat(sys.argv[1].replace('Z','+00:00'))
k=now.astimezone(ZoneInfo('Asia/Seoul')).isocalendar()
print(f'{k.year}-W{k.week:02d}')
PY
)"
OUT="$RUNTIME/$WEEK.stdout.txt"
ERR="$RUNTIME/$WEEK.stderr.txt"

if PYTHONPATH="$PROJECT/src" "$PROJECT/.venv/bin/python" -m tokmo.programs.youtube_weekly_review \
  "$DB" --now "$NOW" >"$OUT.tmp" 2>"$ERR.tmp"; then
  mv "$OUT.tmp" "$OUT"
  mv "$ERR.tmp" "$ERR"
  chmod 600 "$OUT" "$ERR"
  exit 0
fi
rc=$?
mv "$OUT.tmp" "$OUT" 2>/dev/null || true
mv "$ERR.tmp" "$ERR" 2>/dev/null || true
chmod 600 "$OUT" "$ERR" 2>/dev/null || true
{
  printf 'Tokmo YouTube weekly semantic review failed rc=%s week=%s\n' "$rc" "$WEEK"
  /usr/bin/sed -n '1,20p' "$ERR" 2>/dev/null || true
} | "${TOKMO_SYSTEM_ALERT_SCRIPT:-${HERMES_HOME:-$HOME/.hermes}/scripts/send_system_alert.py}" --stdin || true
exit "$rc"
