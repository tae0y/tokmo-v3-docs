#!/bin/bash
# Stand-in for Hermes' send_system_alert.sh / send_system_alert.py.
# Usage: send_system_alert.sh "message"   |   ... | send_system_alert.sh --stdin
# Posts to Telegram when TELEGRAM_ALERT_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID are set; otherwise prints to stderr.
set -u
if [ "${1:-}" = "--stdin" ]; then MESSAGE="$(cat)"; else MESSAGE="${1:-}"; fi
[ -z "$MESSAGE" ] && exit 0
if [ -n "${TELEGRAM_ALERT_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
  curl -sS -m 15 -o /dev/null -X POST "https://api.telegram.org/bot${TELEGRAM_ALERT_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_ALERT_CHAT_ID}" --data-urlencode "text=${MESSAGE:0:3900}" && exit 0
fi
printf '[tokmo alert] %s\n' "$MESSAGE" >&2
