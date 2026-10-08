"""Bounded harness lifecycle channel for the 48-hour live shadow (Stage 4 B).

The runner may emit only typed harness lifecycle events — START once,
FATAL/STALLED durably deduplicated per signature with a cooldown, FINISH
once — and never a stock signal or analysis alert. The default sink is a
local dry-run JSON-lines file with no network. The optional Telegram
stock-bot sender reads its token and chat id exclusively from an injected
environment mapping at construction and never exposes the token through
``repr``, ``describe``, logs, argv, or durable state; its HTTPS opener is
injected by the ambient edge (``tools/run_48h_shadow.py``) so this module
imports no transport. A transport failure is swallowed: lifecycle delivery
may never fail or mutate the harness.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
from typing import Callable, Mapping
import urllib.parse

HARNESS_EVENT_KINDS = frozenset({"START", "FATAL", "STALLED", "FINISH"})
_SIGNATURE_KINDS = frozenset({"FATAL", "STALLED"})

# closed token shape: no spaces, no free text, no secret-bearing payloads
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,119}")

DEDUP_SCHEMA = "tokmo-48h-lifecycle-dedup-v1"
DEFAULT_COOLDOWN_SECONDS = 3600

ENV_TELEGRAM_TOKEN = "TELEGRAM_STOCK_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_STOCK_BOT_CHAT_ID"


class LifecycleTransportError(ValueError):
    """The requested lifecycle transport cannot be configured."""


@dataclass(frozen=True)
class HarnessLifecycleEventV1:
    """One typed harness lifecycle event; token-shaped fields only."""

    kind: str
    label: str
    at: datetime
    signature: str | None = None
    detail: str | None = None

    def __post_init__(self):
        if self.kind not in HARNESS_EVENT_KINDS:
            raise ValueError("unknown harness lifecycle kind")
        if type(self.label) is not str or not _TOKEN.fullmatch(self.label):
            raise ValueError("invalid lifecycle label")
        if (not isinstance(self.at, datetime) or self.at.tzinfo is None
                or self.at.utcoffset() is None):
            raise ValueError("lifecycle event time must be aware")
        if self.kind in _SIGNATURE_KINDS:
            if (type(self.signature) is not str
                    or not _TOKEN.fullmatch(self.signature)):
                raise ValueError("FATAL/STALLED require a dedup signature")
        elif self.signature is not None:
            raise ValueError("signature is only for FATAL/STALLED")
        if self.detail is not None and (
                type(self.detail) is not str
                or not _TOKEN.fullmatch(self.detail)):
            raise ValueError("lifecycle detail must be token-shaped")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


class LifecycleDeduperV1:
    """Durable START-once / FINISH-once / per-signature cooldown gate."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime],
                 cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS):
        self.path = Path(path)
        self._clock = clock
        self._cooldown = timedelta(seconds=cooldown_seconds)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema": DEDUP_SCHEMA, "start_sent": None,
                    "finish_sent": None, "signatures": {}}
        state = json.loads(self.path.read_text())
        if state.get("schema") != DEDUP_SCHEMA:
            raise ValueError("unknown lifecycle dedup schema")
        return state

    def admit(self, event: HarnessLifecycleEventV1) -> bool:
        """True exactly when the event may be delivered; records durably."""
        state = self._read()
        now = self._clock()
        if event.kind == "START":
            if state["start_sent"] is not None:
                return False
            state["start_sent"] = now.isoformat()
        elif event.kind == "FINISH":
            if state["finish_sent"] is not None:
                return False
            state["finish_sent"] = now.isoformat()
        else:
            last = state["signatures"].get(event.signature)
            if last is not None and (
                    now - datetime.fromisoformat(last) < self._cooldown):
                return False
            state["signatures"][event.signature] = now.isoformat()
        _atomic_write_private(
            self.path, json.dumps(state, sort_keys=True).encode(),
        )
        return True


class DryRunLifecycleSink:
    """Default local sink: one JSON line per event, no network ever."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __call__(self, event: HarnessLifecycleEventV1) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = json.dumps({
            "kind": event.kind, "label": event.label,
            "at": event.at.isoformat(), "signature": event.signature,
            "detail": event.detail,
        }, sort_keys=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, (line + "\n").encode())
        finally:
            os.close(descriptor)
        os.chmod(self.path, 0o600)


class TelegramStockBotSender:
    """Optional lifecycle sender through the Telegram stock bot.

    Token and chat id come exclusively from the injected environment
    mapping; the token is held privately for the process lifetime and is
    never represented, logged, persisted, or passed through argv. The
    HTTPS opener is injected by the ambient edge, so this module carries no
    transport import. Send failures are swallowed.
    """

    def __init__(self, *, environ: Mapping, opener: Callable,
                 timeout: float = 10.0):
        token = environ.get(ENV_TELEGRAM_TOKEN)
        chat_id = environ.get(ENV_TELEGRAM_CHAT_ID)
        if not token or not chat_id:
            raise LifecycleTransportError(
                "telegram lifecycle sender requires "
                f"{ENV_TELEGRAM_TOKEN} and {ENV_TELEGRAM_CHAT_ID}"
            )
        if not callable(opener):
            raise LifecycleTransportError(
                "telegram lifecycle sender requires an injected https opener"
            )
        self._token = token
        self._chat_id = chat_id
        self._opener = opener
        self._timeout = timeout

    def __repr__(self) -> str:
        return "TelegramStockBotSender(configured)"

    def describe(self) -> str:
        return "telegram-stock-bot:configured"

    def __call__(self, event: HarnessLifecycleEventV1) -> None:
        text = f"[{event.label}] {event.kind}"
        if event.signature:
            text += f" {event.signature}"
        if event.detail:
            text += f" {event.detail}"
        body = urllib.parse.urlencode(
            {"chat_id": self._chat_id, "text": text},
        ).encode()
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            self._opener(url, body, self._timeout)
        except Exception:
            pass  # lifecycle delivery may never fail the harness


def build_lifecycle_sink(mode: str, *, state_dir: Path | None,
                         clock: Callable[[], datetime], environ: Mapping,
                         cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
                         opener: Callable | None = None) -> Callable:
    """Compose deduper plus transport for one closed lifecycle mode."""
    if mode == "dry-run":
        transport = DryRunLifecycleSink(Path(state_dir) / "lifecycle_log.jsonl")
    elif mode == "telegram":
        transport = TelegramStockBotSender(environ=environ, opener=opener)
    else:
        raise ValueError("unknown lifecycle sink mode")
    deduper = LifecycleDeduperV1(
        Path(state_dir) / "lifecycle_dedup.json" if state_dir is not None
        else Path("lifecycle_dedup.json"),
        clock=clock, cooldown_seconds=cooldown_seconds,
    )

    def sink(event: HarnessLifecycleEventV1) -> None:
        if deduper.admit(event):
            transport(event)

    return sink
