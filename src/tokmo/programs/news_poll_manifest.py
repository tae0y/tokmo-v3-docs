"""Private NEWS target manifest loader."""

from dataclasses import dataclass
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import stat

from tokmo.programs.news_poll_policy import NewsGroup


@dataclass(frozen=True)
class NewsPollManifest:
    version: str
    naver_enabled: bool
    naver_query: str
    naver_display: int
    finnhub_symbols: tuple[str, ...]


@dataclass(frozen=True)
class NewsPollInvocation:
    source: str
    query: str | None = None
    display: int | None = None
    symbol: str | None = None
    from_date: str | None = None
    to_date: str | None = None


def _query(value) -> str:
    if (type(value) is not str or not value.strip() or len(value) > 200
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError("manifest_contract")
    return value.strip()


def load_news_poll_manifest(path: Path) -> NewsPollManifest:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
        raise ValueError("manifest_safety")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if set(raw) != {"version", "naver", "finnhub-company"}:
            raise ValueError
        if raw["version"] != "news16-v1":
            raise ValueError
        naver, finnhub = raw["naver"], raw["finnhub-company"]
        if (set(naver) != {"enabled", "query", "display"}
                or type(naver["enabled"]) is not bool
                or set(finnhub) != {"symbols"}):
            raise ValueError
        if type(naver["display"]) is not int or not 1 <= naver["display"] <= 100:
            raise ValueError
        symbols = finnhub["symbols"]
        if (type(symbols) is not list or not 1 <= len(symbols) <= 10
                or any(type(symbol) is not str
                       or re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol) is None
                       for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError
        return NewsPollManifest(
            raw["version"], naver["enabled"], _query(naver["query"]), naver["display"],
            tuple(symbols),
        )
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError, ValueError):
        raise ValueError("manifest_contract") from None


def plan_news_group(
        group: NewsGroup, manifest: NewsPollManifest, *, on_date: str
) -> tuple[NewsPollInvocation, ...]:
    day = date.fromisoformat(on_date)
    if group is NewsGroup.A:
        return (
            NewsPollInvocation("bbc"),
            NewsPollInvocation("yna-market"),
            *((NewsPollInvocation("naver", query=manifest.naver_query,
                                  display=manifest.naver_display),)
              if manifest.naver_enabled else ()),
        )
    if group is NewsGroup.B:
        previous = (day - timedelta(days=1)).isoformat()
        return (
            *(NewsPollInvocation(
                "finnhub-company", symbol=symbol, from_date=previous,
                to_date=day.isoformat(),
            ) for symbol in manifest.finnhub_symbols),
            NewsPollInvocation("jtbc-breaking"),
            NewsPollInvocation("investing-analyst"),
        )
    if group is NewsGroup.C:
        return (
            NewsPollInvocation("finnhub-market"),
            NewsPollInvocation("yna-industry"),
        )
    raise ValueError("unknown_group")
