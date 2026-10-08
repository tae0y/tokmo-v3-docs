#!/bin/zsh
TOKMO_PROJECT_ROOT="${TOKMO_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
set -euo pipefail
umask 077
source "$HOME/.zshrc" >/dev/null 2>&1
export TELEGRAM_STOCK_BOT_TOKEN="${TELEGRAM_STOCK_BOT:-}"
export TELEGRAM_STOCK_BOT_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
export PYTHONDONTWRITEBYTECODE=1
exec ${TOKMO_PROJECT_ROOT}/.venv/bin/python -u ${TOKMO_PROJECT_ROOT}/tools/run_stockbot_inbox.py
