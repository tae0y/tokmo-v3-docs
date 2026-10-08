#!/bin/bash
# G2.5 v4 minimal verification wrapper — reuses the existing macOS
# sandbox-exec write-deny path for one additional user-approved run.
# canonical root.
#
# The canary must prove it does not mutate ~/.local/share/tokmo-v3 while the
# production cadence is concurrently writing the same stores. Content hashes
# cannot name an actor, so the proof is moved to the OS: this wrapper runs the
# canary under /usr/bin/sandbox-exec with file-write* denied for the canonical
# subpath. Every write the canary could attempt there fails EPERM.
#
# The wrapper only *creates* the denial. The Python CLI independently *proves*
# it, from inside the sandboxed process, with a bounded EPERM probe sequence
# (see src/tokmo/programs/canary_sandbox_attestation.py). This wrapper exports
# no attestation variable and the CLI reads no environment: an env var would be
# a claim, not a proof.
#
# Nothing here is caller-selectable. There are no arguments; the canonical root,
# evidence root, campaign, market, symbol, tick count and cap are all fixed.
# Fails closed when not macOS, when sandbox-exec is missing, or when any
# derived path cannot be resolved.

set -euo pipefail

# ── Platform guard ─────────────────────────────────────────────────────
if [[ "$(/usr/bin/uname)" != "Darwin" ]]; then
    echo "ERROR: sandbox wrapper requires macOS (sandbox-exec)" >&2
    exit 1
fi

if [[ ! -x /usr/bin/sandbox-exec ]]; then
    echo "ERROR: /usr/bin/sandbox-exec not present or not executable" >&2
    exit 1
fi

# ── Derive the home directory from the account database ────────────────
# $HOME is caller-settable, so the account's real NFSHomeDirectory is the
# authority and $HOME is only a fallback for non-directory-service setups.
LOGIN_NAME="$(/usr/bin/id -un)"
HOME_DIR="$(/usr/bin/dscl . -read "/Users/${LOGIN_NAME}" NFSHomeDirectory 2>/dev/null \
    | /usr/bin/awk '/^NFSHomeDirectory:/ {print $2}')"
if [[ -z "${HOME_DIR}" ]]; then
    HOME_DIR="${HOME:-}"
fi
if [[ -z "${HOME_DIR}" || ! -d "${HOME_DIR}" ]]; then
    echo "ERROR: cannot determine home directory for ${LOGIN_NAME}" >&2
    exit 1
fi

# ── Derive and RESOLVE paths ───────────────────────────────────────────
# sandbox-exec matches (subpath ...) against the kernel's resolved path.
# An unresolved path containing a symlink (e.g. /tmp -> /private/tmp) silently
# matches nothing and the deny rule becomes a no-op, so every path in the
# profile is passed through a physical-path resolution first.
resolve_dir() {
    ( cd -P "$1" >/dev/null 2>&1 && pwd -P ) || return 1
}

CANONICAL_ROOT_RAW="${HOME_DIR}/.local/share/tokmo-v3"
if [[ ! -d "${CANONICAL_ROOT_RAW}" ]]; then
    echo "ERROR: canonical root not found: ${CANONICAL_ROOT_RAW}" >&2
    exit 1
fi
CANONICAL_ROOT="$(resolve_dir "${CANONICAL_ROOT_RAW}")" || {
    echo "ERROR: cannot resolve canonical root" >&2; exit 1; }

EVIDENCE_ROOT_RAW="${HOME_DIR}/.local/share/tokmo-v3-canary"
/bin/mkdir -p "${EVIDENCE_ROOT_RAW}"
EVIDENCE_ROOT="$(resolve_dir "${EVIDENCE_ROOT_RAW}")" || {
    echo "ERROR: cannot resolve evidence root" >&2; exit 1; }

PROJECT_ROOT="$(resolve_dir "$(/usr/bin/dirname "$0")/..")" || {
    echo "ERROR: cannot resolve project root" >&2; exit 1; }

PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "ERROR: project interpreter not found: ${PYTHON_BIN}" >&2
    exit 1
fi

# A canonical root nested inside the evidence root (or vice versa) would make
# the deny rule and the evidence writes contradict each other.
case "${EVIDENCE_ROOT}/" in
    "${CANONICAL_ROOT}"/*) echo "ERROR: evidence root inside canonical root" >&2; exit 1;;
esac
case "${CANONICAL_ROOT}/" in
    "${EVIDENCE_ROOT}"/*) echo "ERROR: canonical root inside evidence root" >&2; exit 1;;
esac

# ── HEAD commit (the CLI re-verifies this against a clean tree) ────────
HEAD_SHA="$(/usr/bin/git -C "${PROJECT_ROOT}" rev-parse HEAD 2>/dev/null || true)"
if [[ -z "${HEAD_SHA}" ]]; then
    echo "ERROR: cannot determine git HEAD in ${PROJECT_ROOT}" >&2
    exit 1
fi

# ── Sandbox profile ────────────────────────────────────────────────────
# (allow default) keeps the process otherwise normal — it still reads the
# project, writes its own evidence store, and reaches the provider over the
# network. The single deny is the whole point of the wrapper.
SANDBOX_PROFILE="$(/usr/bin/mktemp -t tokmo_canary_sandbox)"
trap '/bin/rm -f "${SANDBOX_PROFILE}"' EXIT

/bin/cat > "${SANDBOX_PROFILE}" <<SANDBOX_EOF
(version 1)
(allow default)
(deny file-write* (subpath "${CANONICAL_ROOT}"))
SANDBOX_EOF

# ── Run the canary under the sandbox ───────────────────────────────────
# set -e would abort before the exit status could be reported, and the CLI
# uses a nonzero exit for every non-success stop reason, so the status is
# captured explicitly.
set +e
/usr/bin/sandbox-exec -f "${SANDBOX_PROFILE}" \
    "${PYTHON_BIN}" \
    "${PROJECT_ROOT}/tools/run_tokmo_pulse_canary.py" \
    --mode bounded-live-canary \
    --market KR --symbol 005930 \
    --canonical-root "${CANONICAL_ROOT}" \
    --project-root "${PROJECT_ROOT}" \
    --max-ticks 5 \
    --max-provider-starts 32 \
    --base-commit "${HEAD_SHA}"
EXIT_CODE=$?
set -e

exit "${EXIT_CODE}"
