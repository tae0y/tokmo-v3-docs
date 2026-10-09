#!/bin/bash
# Container entrypoint. Prepares the private files the adapters insist on (regular file, 0600, no symlink),
# then runs one of the commands below. Anything else is executed as-is.
#   scheduler  run docker/crontab under supercronic (default)
#   test       pytest (extra args pass through)
#   gate       product-gate install + issue + check, then save the approvals into the secrets volume
#   keys       tools/check_api_keys.py
set -euo pipefail
umask 077

ROOT="${PROJECT:-/home/tokmo/tokmo-v3}"
SECRETS="$ROOT/.secrets"
APPROVALS="$SECRETS/product-approvals"
PY="$ROOT/.venv/bin/python"

# Approval files sit in the project tree (gitignored); keep copies in the secrets volume so they survive
# container recreation. They pin code hashes, so a rebuilt image with changed code needs `gate` again.
APPROVAL_FILES=(
  "ops/news-product-approval.json"
  "ops/opportunity-rotation-product-approval.json"
  "quant-on-demand-approval.json"
)

copy_private() {  # src dest
  if [ -f "$1" ] && [ ! -L "$1" ]; then install -m 600 "$1" "$2"; fi
}

prepare() {
  mkdir -p "$ROOT/var/data" "$ROOT/var/runtime" "$ROOT/var/state" "$APPROVALS"
  chmod 700 "$ROOT" "$ROOT/var" "$SECRETS" "$APPROVALS" "$HOME/.hermes" "$HOME/.codex" "$HOME/.claude"
  # .env.toss / .env.kis must be regular files at the project root, so they cannot be volumes themselves.
  copy_private "$SECRETS/env.toss" "$ROOT/.env.toss"
  copy_private "$SECRETS/env.kis" "$ROOT/.env.kis"
  for rel in "${APPROVAL_FILES[@]}"; do
    copy_private "$APPROVALS/$(basename "$rel")" "$ROOT/$rel"
  done
}

prepare
cmd="${1:-scheduler}"
[ $# -gt 0 ] && shift

case "$cmd" in
  scheduler)
    exec /usr/local/bin/supercronic -passthrough-logs "$ROOT/docker/crontab"
    ;;
  test)
    exec "$PY" -m pytest -q -p no:cacheprovider "$@"
    ;;
  gate)
    "$PY" tools/install_product_gate.py install "$@"
    "$PY" tools/install_product_gate.py issue --reissue
    for rel in "${APPROVAL_FILES[@]}"; do
      copy_private "$ROOT/$rel" "$APPROVALS/$(basename "$rel")"
    done
    exec "$PY" tools/install_product_gate.py check
    ;;
  keys)
    exec "$PY" tools/check_api_keys.py "$@"
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac
