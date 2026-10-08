#!/bin/bash
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
exec ${TOKMO_PROJECT_ROOT}/ops/hermes-cron/tokmo_news_group.sh A
