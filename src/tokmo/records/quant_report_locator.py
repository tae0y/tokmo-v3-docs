"""Locate the latest receipt-verified Quant MARKET_DISCOVERY report per market.

Read-only over the existing preview report root. The family ledger's
``sessions`` map is the authority for "latest completed session"; each
candidate report is accepted only when its sibling receipt binds the exact
report bytes (``report_sha256``) and a success outcome. Nothing is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path

from tokmo.records.quant_reports import decode_report
from tokmo.values.quant_discovery import QuantRunReportV1

MARKETS = ("KR", "US")
_SUCCESS_OUTCOMES = frozenset({"SUCCEEDED_WITH_CARDS", "SUCCEEDED_SILENT", "NO_DATA"})
_MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class LocatedReportV1:
    market: str
    completed_session_date: str
    identity_sha256: str
    report_sha256: str
    report_path: Path
    outcome: str
    campaign_id: str | None
    report: QuantRunReportV1


def _verified(report_path: Path, market: str) -> LocatedReportV1 | None:
    receipt_path = Path(str(report_path).replace(".report.json", ".receipt.json"))
    try:
        if report_path.is_symlink() or receipt_path.is_symlink():
            return None
        if report_path.stat().st_size > _MAX_BYTES:
            return None
        report_raw = report_path.read_bytes()
        receipt = json.loads(receipt_path.read_bytes())
        report_json = json.loads(report_raw)
        identity = report_json["identity"]
        digest = sha256(report_raw).hexdigest()
        if (
            receipt.get("outcome") not in _SUCCESS_OUTCOMES
            or receipt.get("report_sha256") != digest
            or receipt.get("identity") != identity
            or identity.get("market") != market
            or identity.get("product_kind") != "MARKET_DISCOVERY"
            or type(identity.get("completed_session_date")) is not str
        ):
            return None
        session = identity["completed_session_date"]
        date.fromisoformat(session)
        report = decode_report(report_raw)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return LocatedReportV1(
        market,
        session,
        identity["identity_sha256"],
        digest,
        report_path,
        receipt["outcome"],
        None,
        report,
    )


def _scan(leaf: Path, market: str) -> tuple[LocatedReportV1, ...]:
    if not leaf.is_dir():
        return ()
    found = []
    for report_path in sorted(leaf.glob("*.report.json")):
        located = _verified(report_path, market)
        if located is not None:
            found.append(located)
    return tuple(found)


def _latest(candidates) -> LocatedReportV1 | None:
    if not candidates:
        return None
    # Latest completed session wins; a card-bearing report outranks an empty
    # one for the same session; identity digest is the final deterministic tie.
    return max(
        candidates,
        key=lambda item: (
            item.completed_session_date,
            len(item.report.cards),
            item.identity_sha256,
        ),
    )


def locate_latest_verified_reports(
    report_root: str | Path,
) -> dict[str, LocatedReportV1 | None]:
    """Latest receipt-verified report per market, or None when absent."""
    root = Path(report_root)
    result: dict[str, LocatedReportV1 | None] = {}
    ledger_path = root / "family-ledger.json"
    sessions: dict = {}
    try:
        if ledger_path.is_file():
            ledger = json.loads(ledger_path.read_bytes())
            if type(ledger) is dict and type(ledger.get("sessions")) is dict:
                sessions = ledger["sessions"]
    except (OSError, ValueError, json.JSONDecodeError):
        sessions = {}
    for market in MARKETS:
        candidates: list[LocatedReportV1] = []
        if sessions:
            by_date = {}
            for key, campaign_id in sessions.items():
                prefix, separator, session = key.partition(":")
                if separator != ":" or prefix != market or type(campaign_id) is not str:
                    continue
                try:
                    date.fromisoformat(session)
                except (TypeError, ValueError):
                    continue
                by_date[session] = campaign_id
            for session in sorted(by_date, reverse=True):
                campaign_id = by_date[session]
                leaf = (
                    root
                    / "sessions"
                    / campaign_id
                    / "report-store"
                    / "market_discovery"
                    / market.lower()
                )
                for located in _scan(leaf, market):
                    if located.completed_session_date == session:
                        candidates.append(
                            LocatedReportV1(
                                located.market,
                                located.completed_session_date,
                                located.identity_sha256,
                                located.report_sha256,
                                located.report_path,
                                located.outcome,
                                campaign_id,
                                located.report,
                            )
                        )
                if candidates:
                    break
        if not candidates:
            candidates.extend(_scan(root / "market_discovery" / market.lower(), market))
        result[market] = _latest(candidates)
    return result
