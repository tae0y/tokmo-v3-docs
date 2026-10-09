#!/usr/bin/env python3
"""Bounded one-shot V2 narrative importer. Default action is private preview only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

FROZEN_STRATEGY_SHA = "d379b31c6849a29cc75b983a08560ea7dbf45b7c"
FROZEN_TRADE_SHA256 = "016d11922aa032256f45d0a140ef4fe5ee81ed03f02df606465014a1e8ca94c7"

# User-approved current Toss identity corrections, verified live 2026-07-29.
# Keys preserve the exact legacy code/name pair; values are current Toss code/name.
CURRENT_TOSS_IDENTITY = {
    ("465110", "KODEX 현대차로보틱스밸류체인Top2플러스"): (
        "0204D0",
        "KODEX 현대차로보틱스밸류체인TOP3플러스",
    ),
    ("479850", "SOL 화장품TOP3플러스"): ("0008T0", "SOL 화장품TOP3플러스"),
}


class Narrative(NamedTuple):
    source_kind: str
    source_row: int
    name: str
    ticker: str
    market: str
    code: str
    scope: str | None
    payload: str
    event_date: str = "unknown"
    legacy_trade_key: str | None = None
    legacy_code: str | None = None
    legacy_name: str | None = None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def legacy_trade_key(source_sha: str, source_row: int) -> str:
    return hashlib.sha256(f"{source_sha}:{source_row}".encode()).hexdigest()


def normalize_identity(ticker: str, market: str) -> tuple[str, str]:
    ticker = ticker.strip().upper()
    market = market.strip().upper()
    if market == "KR":
        ticker = re.sub(r"\.(KS|KQ)$", "", ticker)
        if not re.fullmatch(r"[0-9A-Z]{6}", ticker):
            raise ValueError(f"unresolved KR ticker at source boundary: {ticker!r}")
    elif market == "US":
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]*", ticker):
            raise ValueError(f"unresolved US ticker at source boundary: {ticker!r}")
    else:
        raise ValueError(f"unsupported market: {market!r}")
    return market, ticker


def parse_strategy_playbooks(path: Path) -> list[Narrative]:
    lines = (
        path.read_text(encoding="utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .splitlines(keepends=True)
    )
    in_stocks = False
    rows: list[Narrative] = []
    i = 0
    current: dict[str, str | int] | None = None
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip("\n")
        if line == "stocks:":
            in_stocks = True
            i += 1
            continue
        if not in_stocks:
            i += 1
            continue
        match_item = re.match(r"^  - 종목명:\s*(.+?)\s*$", line)
        if match_item:
            current = {"name": match_item.group(1), "source_row": i + 1}
            i += 1
            continue
        if current is not None:
            field = re.match(r"^    (ticker|market|구분):\s*(.*?)\s*$", line)
            if field:
                current[field.group(1)] = field.group(2)
                i += 1
                continue
            if line == "    플레이북: |":
                i += 1
                payload_lines: list[str] = []
                while i < len(lines):
                    candidate = lines[i]
                    no_nl = candidate.rstrip("\n")
                    if no_nl and not no_nl.startswith("      "):
                        break
                    if no_nl.startswith("      "):
                        payload_lines.append(candidate[6:])
                    elif not no_nl:
                        payload_lines.append(candidate)
                    i += 1
                required = ("name", "ticker", "market", "구분", "source_row")
                if not all(key in current for key in required):
                    raise ValueError(
                        f"incomplete strategy item near row {current.get('source_row')}"
                    )
                market, code = normalize_identity(
                    str(current["ticker"]), str(current["market"])
                )
                kind = str(current["구분"])
                scope = {"보유": "personal", "퇴직": "retirement"}.get(kind)
                rows.append(
                    Narrative(
                        "v2_strategy_playbook",
                        int(current["source_row"]),
                        str(current["name"]),
                        str(current["ticker"]),
                        market,
                        code,
                        scope,
                        "".join(payload_lines),
                    )
                )
                continue
        i += 1
    return rows


def parse_trade_reasons(path: Path) -> tuple[list[Narrative], str]:
    source_sha = file_sha256(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("trade history top-level must be a list")
    rows: list[Narrative] = []
    for row_no, row in enumerate(payload, 1):
        if not isinstance(row, dict):
            raise ValueError(f"trade row {row_no} is not an object")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"trade row {row_no} has no narrative reason")
        ticker = str(row.get("ticker") or "")
        market = "KR" if re.search(r"\.(KS|KQ)$", ticker.upper()) else "US"
        market, code = normalize_identity(ticker, market)
        legacy_code = code
        legacy_name = str(row.get("name") or "").strip()
        code, current_name = CURRENT_TOSS_IDENTITY.get(
            (legacy_code, legacy_name), (legacy_code, legacy_name)
        )
        scope = {"퇴직연금": "retirement"}.get(str(row.get("account") or "").strip())
        rows.append(
            Narrative(
                "v2_trade_history_reason",
                row_no,
                current_name,
                ticker,
                market,
                code,
                scope,
                reason.replace("\r\n", "\n").replace("\r", "\n"),
                str(row.get("date") or "unknown"),
                legacy_trade_key(source_sha, row_no),
                legacy_code,
                legacy_name,
            )
        )
    return rows, source_sha


def proposed_path(row: Narrative) -> str | None:
    if row.scope is None:
        return None
    return f"{row.scope}/{row.market}/{row.code}.md"


def preview_records(
    strategy: list[Narrative],
    trades: list[Narrative],
    strategy_ref: str,
    trade_sha: str,
) -> list[dict]:
    records: list[dict] = []
    for row in [*strategy, *trades]:
        records.append(
            {
                "path": proposed_path(row),
                "resolved": proposed_path(row) is not None,
                "scope": row.scope,
                "market": row.market,
                "instrument_code": row.code,
                "instrument_name": row.name,
                "source_kind": row.source_kind,
                "source_row": row.source_row,
                "source_ref": strategy_ref
                if row.source_kind == "v2_strategy_playbook"
                else f"trade_history.json#row-{row.source_row}@{trade_sha}",
                "event_date": row.event_date,
                "legacy_trade_key": row.legacy_trade_key,
                "legacy_identity": (
                    {
                        "instrument_code": row.legacy_code,
                        "instrument_name": row.legacy_name,
                    }
                    if row.source_kind == "v2_trade_history_reason"
                    else None
                ),
                "payload": row.payload,
            }
        )
    return records


def write_preview(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "records": records}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"preview written: {path} ({len(records)} records; payload hidden from stdout)"
    )


def ensure_ignored(path: Path, repo_root: Path) -> None:
    absolute = path.resolve()
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(absolute)], cwd=repo_root.resolve()
    )
    if result.returncode != 0:
        raise ValueError(f"preview path is not gitignored: {absolute}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", type=Path, required=True)
    parser.add_argument("--trade-history", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--strategy-ref", default=f"strategy_advisor.yaml@{FROZEN_STRATEGY_SHA}"
    )
    parser.add_argument("--allow-source-sha-change", action="store_true")
    args = parser.parse_args(argv)
    trade_sha = file_sha256(args.trade_history)
    if trade_sha != FROZEN_TRADE_SHA256 and not args.allow_source_sha_change:
        raise SystemExit("trade-history SHA changed; STOP")
    ensure_ignored(args.preview, args.repo_root)
    strategy = parse_strategy_playbooks(args.strategy)
    trades, _ = parse_trade_reasons(args.trade_history)
    records = preview_records(strategy, trades, args.strategy_ref, trade_sha)
    write_preview(args.preview, records)
    resolved = sum(bool(item["resolved"]) for item in records)
    print(
        f"inventory: strategy={len(strategy)} trade_reasons={len(trades)} resolved={resolved} unresolved={len(records) - resolved}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
