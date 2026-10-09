#!/usr/bin/env python3
"""One-shot bounded NEWS connector runner using production manager/adapters."""

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from tokmo.facts.bbc_news_live import BbcBusinessConnector, BbcBusinessRequest
from tokmo.facts.finnhub_news_live import (
    FinnhubCompanyNewsConnector,
    FinnhubCompanyNewsRequest,
    FinnhubMarketNewsConnector,
    FinnhubMarketNewsRequest,
    FinnhubNewsCredentials,
)
from tokmo.facts.google_news_live import GoogleNewsConnector, GoogleNewsQuery
from tokmo.facts.investing_news_live import (
    InvestingAnalystConnector,
    InvestingAnalystRequest,
)
from tokmo.facts.jtbc_news_live import JtbcBreakingConnector, JtbcBreakingRequest
from tokmo.facts.naver_news import build_naver_news_request
from tokmo.facts.naver_news_live import NaverNewsConnector, NaverNewsCredentials
from tokmo.facts.news_http import NewsHttpTransport
from tokmo.facts.yonhap_news_live import (
    YonhapFeed,
    YonhapNewsConnector,
    YonhapNewsRequest,
)
from tokmo.programs.external_news_storage import bootstrap_external_news
from tokmo.programs.news_collection import (
    FetchAttemptLedger,
    NewsCollectionJob,
    NewsCollectionManager,
)
from tokmo.programs.news_intake import persist_news_result
from tokmo.values.external_news import SaveOutcome
from tokmo.values.news_collection_diagnostics import exception_cause, safe_cause
from tokmo.values.news_connectors import (
    BBC_BUSINESS_RSS,
    FINNHUB_COMPANY_NEWS,
    FINNHUB_MARKET_NEWS,
    GOOGLE_NEWS_SEARCH_RSS,
    INVESTING_ANALYST_RSS,
    JTBC_BREAKING_RSS,
    NAVER_NEWS_API,
    YONHAP_INDUSTRY_RSS,
    YONHAP_MARKET_RSS,
    NewsFetchStatus,
)

_ALLOWED_HOSTS = frozenset(
    {
        "feeds.bbci.co.uk",
        "openapi.naver.com",
        "finnhub.io",
        "www.yna.co.kr",
        "news-ex.jtbc.co.kr",
        "news.google.com",
        "kr.investing.com",
    }
)
_KEYS = frozenset(
    {
        "NAVER_NEWS_CLIENT_ID",
        "NAVER_NEWS_CLIENT_SECRET",
        "FINNHUB_API_KEY",
    }
)


def _load_values(path: Path) -> dict[str, str]:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValueError("unsafe credential file")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError("invalid credential line")
        key, value = raw.split("=", 1)
        if key not in _KEYS or key in values or not value:
            raise ValueError("unknown, duplicate, or empty credential key")
        values[key] = value
    return values


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-news-connector")
    parser.add_argument(
        "--source",
        required=True,
        choices=(
            "bbc",
            "naver",
            "finnhub-company",
            "finnhub-market",
            "yna-market",
            "yna-industry",
            "jtbc-breaking",
            "google-news-search",
            "investing-analyst",
        ),
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--ceiling", required=True, type=int, choices=(1,))
    parser.add_argument(
        "--credentials", type=Path, default=Path.home() / ".config/tokmo/news.env"
    )
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--display", type=int, default=10)
    parser.add_argument("--symbol")
    parser.add_argument("--from-date", type=date.fromisoformat)
    parser.add_argument("--to-date", type=date.fromisoformat)
    return parser


class _Progress:
    def __init__(self, campaign_id):
        self.started = time.monotonic()
        self.stage, self.received, self.persisted = "PREPARE", None, 0
        self.http_status = None
        self.trace = _fingerprint(campaign_id)[:16]
        self.counts = dict(inserted=0, duplicate=0, conflict=0, revision=0)

    def snapshot(self, cause=None):
        return dict(
            stage=self.stage,
            cause=cause,
            received=self.received,
            persisted=self.persisted,
            http_status=self.http_status,
            elapsed_ms=int((time.monotonic() - self.started) * 1000),
            trace=self.trace,
        )

    def emit(self, stage=None):
        if stage is not None:
            self.stage = stage
        print(
            "NEWS_PROGRESS " + json.dumps(self.snapshot(), separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )

    def saved(self, receipt):
        self.persisted += 1
        key = (
            "revision"
            if receipt.revision_evidence_id is not None
            else receipt.outcome.value.lower()
        )
        self.counts[key] += 1
        self.emit()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    progress = _Progress(args.campaign_id)
    progress.emit()
    try:
        return _run(args, progress)
    except Exception as error:
        status = (
            "CONFIG_UNAVAILABLE"
            if progress.stage == "PREPARE"
            else "STORAGE_FAILURE"
            if progress.stage == "PERSIST"
            else "RUNNER_FAILURE"
        )
        print(
            json.dumps(
                dict(
                    status=status,
                    source=args.source,
                    http_status=progress.http_status,
                    **progress.counts,
                    diagnostics=progress.snapshot(exception_cause(error)),
                ),
                separators=(",", ":"),
            )
        )
        return 2 if status == "CONFIG_UNAVAILABLE" else 3


def _run(args, progress) -> int:
    now = datetime.now(UTC)
    if not args.db.exists():
        if not args.bootstrap:
            raise FileNotFoundError
        bootstrap_external_news(args.db, now=now)
    ledger = FetchAttemptLedger(args.ledger)
    ledger.bootstrap(now=now)

    http = NewsHttpTransport(allowed_hosts=_ALLOWED_HOSTS)
    if args.source == "bbc":
        spec, target, request = BBC_BUSINESS_RSS, "feed:business", BbcBusinessRequest()
        adapter = BbcBusinessConnector(http)
        fingerprint = _fingerprint(args.source, target)
    elif args.source in {"naver", "finnhub-company", "finnhub-market"}:
        values = _load_values(args.credentials)
        if args.source == "naver":
            if not args.query:
                raise ValueError("query required")
            credentials = NaverNewsCredentials(
                values["NAVER_NEWS_CLIENT_ID"], values["NAVER_NEWS_CLIENT_SECRET"]
            )
            request = build_naver_news_request(args.query, display=args.display)
            spec, target = NAVER_NEWS_API, f"query:{_fingerprint(args.query)}"
            adapter = NaverNewsConnector(http, credentials)
            fingerprint = _fingerprint(args.source, args.query, str(args.display))
        elif args.source == "finnhub-company":
            if not args.symbol or args.from_date is None or args.to_date is None:
                raise ValueError("company args required")
            credentials = FinnhubNewsCredentials(values["FINNHUB_API_KEY"])
            request = FinnhubCompanyNewsRequest(
                args.symbol, args.from_date, args.to_date
            )
            spec, target = FINNHUB_COMPANY_NEWS, f"symbol:{args.symbol}"
            adapter = FinnhubCompanyNewsConnector(http, credentials)
            fingerprint = _fingerprint(
                args.source,
                args.symbol,
                args.from_date.isoformat(),
                args.to_date.isoformat(),
            )
        else:
            credentials = FinnhubNewsCredentials(values["FINNHUB_API_KEY"])
            request = FinnhubMarketNewsRequest()
            spec, target = FINNHUB_MARKET_NEWS, "category:general"
            adapter = FinnhubMarketNewsConnector(http, credentials)
            fingerprint = _fingerprint(args.source, target)
    elif args.source == "yna-market":
        spec, target = YONHAP_MARKET_RSS, "feed:market"
        request, adapter = (
            YonhapNewsRequest(YonhapFeed.MARKET),
            YonhapNewsConnector(http),
        )
        fingerprint = _fingerprint(args.source, target)
    elif args.source == "yna-industry":
        spec, target = YONHAP_INDUSTRY_RSS, "feed:industry"
        request, adapter = (
            YonhapNewsRequest(YonhapFeed.INDUSTRY),
            YonhapNewsConnector(http),
        )
        fingerprint = _fingerprint(args.source, target)
    elif args.source == "jtbc-breaking":
        spec, target = JTBC_BREAKING_RSS, "feed:breaking"
        request, adapter = JtbcBreakingRequest(), JtbcBreakingConnector(http)
        fingerprint = _fingerprint(args.source, target)
    elif args.source == "investing-analyst":
        spec, target = INVESTING_ANALYST_RSS, "feed:analyst-opinion"
        request = InvestingAnalystRequest()
        adapter = InvestingAnalystConnector(http)
        fingerprint = _fingerprint(args.source, target)
    else:
        if not args.query:
            raise ValueError("query required")
        spec = GOOGLE_NEWS_SEARCH_RSS
        target = f"query:{_fingerprint(args.query)}"
        request, adapter = GoogleNewsQuery(args.query), GoogleNewsConnector(http)
        fingerprint = _fingerprint(args.source, args.query)

    def reserve(campaign, connector, fingerprint, reserved_at):
        progress.emit("RESERVE")
        sequence = ledger.reserve_bounded(
            campaign, connector, fingerprint, reserved_at, ceiling=args.ceiling
        )
        progress.emit("FETCH")
        return sequence

    manager = NewsCollectionManager(
        adapters={spec.connector_id: adapter},
        reservation=reserve,
    )
    job = NewsCollectionJob(spec.connector_id, request, target, fingerprint)
    progress.emit("FETCH")
    outcome = manager.collect(args.campaign_id, (job,), observed_at=now).results[0]
    result = outcome.result
    progress.received, progress.http_status = len(result.items), result.http_status
    progress.emit(
        "PERSIST"
        if result.status is NewsFetchStatus.COLLECTED
        else result.failure_stage or "FETCH"
    )
    receipts = persist_news_result(
        args.db, spec, result, target_ref=target, on_saved=progress.saved
    )
    counts = {value.value: 0 for value in SaveOutcome}
    revision = 0
    for receipt in receipts:
        if receipt.revision_evidence_id is not None:
            revision += 1
        else:
            counts[receipt.outcome.value] += 1
    cause = None
    if result.status in {NewsFetchStatus.COLLECTED, NewsFetchStatus.EMPTY}:
        progress.stage = "COMPLETE"
    else:
        cause = (
            safe_cause(result.failure_class)
            if result.failure_class
            else (
                f"HTTP_{result.http_status}"
                if result.http_status is not None
                else safe_cause(result.status.value)
            )
        )
    print(
        json.dumps(
            {
                "status": result.status.value,
                "source": args.source,
                "campaign_sequence": 1,
                "items": len(result.items),
                "bytes_received": result.bytes_received,
                "http_status": result.http_status,
                "inserted": counts["INSERTED"],
                "duplicate": counts["DUPLICATE"],
                "conflict": counts["CONFLICT"],
                "revision": revision,
                "diagnostics": progress.snapshot(cause),
                "credential_values_exposed": False,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return (
        0 if result.status in {NewsFetchStatus.COLLECTED, NewsFetchStatus.EMPTY} else 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
