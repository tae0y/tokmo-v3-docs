# tokmo-v3 runtime image (Linux). Build from the project root:
#   docker compose -f docker/compose.yaml build
# Code lives in the image; state, credentials and CLI logins live in named volumes (see compose.yaml).
FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA1_AMD64=e63c11a9726b775a6a11801e81af4f3fb926aa68
ARG SUPERCRONIC_SHA1_ARM64=e8e25a4672a3caf3cebee10ef418cad085926595
# Optional model CLIs. Set to "" to skip (Z.AI GLM over HTTP needs neither).
ARG CODEX_VERSION=rust-v0.162.0
ARG CLAUDE_VERSION=stable
ARG UID=1000
ARG GID=1000

ENV TZ=Asia/Seoul \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Wrappers need zsh (two use #!/bin/zsh), sqlite3, curl, coreutils timeout; code shells out to git and lsof.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git lsof sqlite3 tzdata zsh procps \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) arch=amd64; sum="$SUPERCRONIC_SHA1_AMD64" ;; \
      arm64) arch=arm64; sum="$SUPERCRONIC_SHA1_ARM64" ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}"; \
    echo "${sum}  /usr/local/bin/supercronic" | sha1sum -c -; \
    chmod 755 /usr/local/bin/supercronic

RUN set -eu; \
    if [ -n "$CODEX_VERSION" ]; then \
      case "$TARGETARCH" in amd64) triple=x86_64 ;; arm64) triple=aarch64 ;; *) exit 1 ;; esac; \
      curl -fsSL "https://github.com/openai/codex/releases/download/${CODEX_VERSION}/codex-${triple}-unknown-linux-musl.tar.gz" \
        | tar -xz -C /tmp; \
      install -m 755 "/tmp/codex-${triple}-unknown-linux-musl" /usr/local/bin/codex; \
      rm -f /tmp/codex-*; \
    fi

RUN groupadd -g "$GID" tokmo && useradd -m -u "$UID" -g "$GID" -s /bin/zsh tokmo
USER tokmo
WORKDIR /home/tokmo

# The code calls claude at ~/.local/bin/claude, which is where the native installer puts it.
RUN set -eu; \
    if [ -n "$CLAUDE_VERSION" ]; then curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_VERSION"; fi

ENV PROJECT=/home/tokmo/tokmo-v3 \
    CLAUDE_CONFIG_DIR=/home/tokmo/.claude \
    TOKMO_COMPLETION_SAFE_CLI=/home/tokmo/tokmo-v3/ops/cron/fallback/completion-safe-cli \
    TOKMO_SYSTEM_ALERT_JOB=/home/tokmo/tokmo-v3/ops/cron/fallback/run_system_alert_job.py \
    TOKMO_SYSTEM_ALERT_SCRIPT=/home/tokmo/tokmo-v3/ops/cron/fallback/send_system_alert.sh

# Dependencies first so code edits do not reinstall them. Wrappers default to $PROJECT/.venv/bin/python.
COPY --chown=tokmo:tokmo pyproject.toml ${PROJECT}/pyproject.toml
RUN python -m venv ${PROJECT}/.venv \
 && ${PROJECT}/.venv/bin/pip install openpyxl==3.1.5 defusedxml==0.7.1 et-xmlfile==2.0.0 pytest

COPY --chown=tokmo:tokmo . ${PROJECT}
# Volume mount points are created here so a fresh named volume inherits tokmo ownership and 0700.
# Two wrappers source ~/.zshrc for stock-bot values; in the container those come from the environment.
RUN ${PROJECT}/.venv/bin/pip install --no-deps -e ${PROJECT} \
 && mkdir -p ${PROJECT}/var/data ${PROJECT}/var/runtime ${PROJECT}/var/state ${PROJECT}/.secrets \
             /home/tokmo/.hermes /home/tokmo/.codex /home/tokmo/.claude /home/tokmo/.local/share/tokmo-v3 \
 && chmod 700 ${PROJECT} ${PROJECT}/var ${PROJECT}/.secrets \
              /home/tokmo/.hermes /home/tokmo/.codex /home/tokmo/.claude /home/tokmo/.local/share/tokmo-v3 \
 && touch /home/tokmo/.zshrc \
 && chmod 755 ${PROJECT}/docker/entrypoint.sh

WORKDIR ${PROJECT}
ENTRYPOINT ["/home/tokmo/tokmo-v3/docker/entrypoint.sh"]
CMD ["scheduler"]
