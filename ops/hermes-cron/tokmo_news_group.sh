#!/bin/bash
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
set -eu
GROUP="${1:?group required}"
case "$GROUP" in A) OFFSET=0;; B) OFFSET=5;; C) OFFSET=10;; *) printf 'TOKMO_NEWS_GROUP_FAILED: invalid_group\n'; exit 2;; esac
ROOT="${TOKMO_PROJECT_ROOT}"
PY="$ROOT/.venv/bin/python"
MINUTE=$(date '+%M')
MINUTE=$((10#$MINUTE))
DELTA=$(((MINUTE-OFFSET+15)%15))
SCHEDULED_FOR=$($PY - "$DELTA" <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sys
now=datetime.now(ZoneInfo('Asia/Seoul')).replace(second=0,microsecond=0)
print((now-timedelta(minutes=int(sys.argv[1]))).isoformat())
PY
)
exec "$PY" "$ROOT/tools/run_news_group.py" \
  --group "$GROUP" \
  --scheduled-for "$SCHEDULED_FOR" \
  --manifest "${TOKMO_PROJECT_ROOT}/.secrets/config/tokmo-v3/news_poll_manifest_v1.json" \
  --runtime ${TOKMO_PROJECT_ROOT}/var/data/news/group_runtime \
  --db ${TOKMO_PROJECT_ROOT}/var/data/news/external_news_v4.sqlite3 \
  --ledger ${TOKMO_PROJECT_ROOT}/var/data/news/fetch_attempts.sqlite3 \
  --credentials "${TOKMO_PROJECT_ROOT}/.secrets/config/tokmo/news.env" \
  --health-db ${TOKMO_PROJECT_ROOT}/var/data/news/source_health.sqlite3 \
  --health-telegram-env "${TOKMO_PROJECT_ROOT}/.secrets/hermes-credentials/news_health_telegram.env" \
  --universe \
  --holdings-store ${TOKMO_PROJECT_ROOT}/var/data/stores/current_holdings.json \
  --holdings-snapshots ${TOKMO_PROJECT_ROOT}/var/data/toss/holdings_snapshots.sqlite3 \
  --watchlists-dir ${TOKMO_PROJECT_ROOT}/watchlists \
  --universe-store ${TOKMO_PROJECT_ROOT}/var/data/news/news_universe.json
