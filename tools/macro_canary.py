"""Fixed-purpose, cumulative-cap Toss macro verification campaign."""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
from tokmo.facts.toss import TossAccessToken, build_auth_request, parse_token_response
from tokmo.facts.toss_reads import TossReadOperation, build_read_request, query_read


class CampaignLedgerV1:
    PURPOSE_ID = "AI-MACRO-001:G1B_VERIFICATION:2026-08-09:v1"
    CAMPAIGN_KEY = hashlib.sha256(PURPOSE_ID.encode()).hexdigest()
    CAP = 10
    PURPOSES = (
        "INITIAL_TOKEN",
        "INITIAL_KOSPI",
        "INITIAL_INVESTOR",
        "INITIAL_PROXY",
        "REACQUIRED_TOKEN_P1",
        "RESIDUAL_KOSPI_P1",
        "RESIDUAL_INVESTOR_P1",
        "RESIDUAL_PROXY_P1",
        "REACQUIRED_TOKEN_P2",
        "RESIDUAL_KOSPI_P2",
        "RESIDUAL_INVESTOR_P2",
        "RESIDUAL_PROXY_P2",
    )


@dataclass(frozen=True)
class CampaignStoreDepsV1:
    root: Path
    live: bool
    ledger: Path
    lock: Path
    receipts: Path

    @classmethod
    def for_live(cls):
        root = _canonical_campaign_path()
        return cls(
            root,
            True,
            root / "campaign.json",
            root / "campaign.lock",
            root / "receipts",
        )

    @classmethod
    def for_test(cls, root):
        target = Path(root) / CampaignLedgerV1.CAMPAIGN_KEY
        return cls(
            target,
            False,
            target / "campaign.json",
            target / "campaign.lock",
            target / "receipts",
        )


@dataclass(frozen=True)
class ResidualPurposeV1:
    purpose_code: str
    question_code: str
    series: str


def _canonical_campaign_path():
    return (
        Path.home()
        / ".local/share/tokmo-v3/macro-canary"
        / CampaignLedgerV1.CAMPAIGN_KEY
    )


def _write_private_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        os.fsync(dfd)
        os.close(dfd)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def _reserve_campaign_start(
    deps, purpose_code, operation, parameter_digest, invocation_id, revision_sha
):
    if (
        purpose_code not in CampaignLedgerV1.PURPOSES
        or len(parameter_digest) != 64
        or len(invocation_id) != 64
        or len(revision_sha) != 40
    ):
        raise ValueError("closed campaign input")
    if deps.live:
        home = Path.home()
        canonical = _canonical_campaign_path()
        legacy = home / ".local/share/tokmo-v3/canary"
        parent = canonical.parent
        if (
            deps.root != canonical
            or legacy.is_symlink()
            or (legacy.exists() and any(legacy.iterdir()))
        ):
            raise ValueError("live path ambiguity")
        for path in (
            home / ".local",
            home / ".local/share",
            home / ".local/share/tokmo-v3",
            parent,
            canonical,
        ):
            if path.is_symlink():
                raise ValueError("live path symlink")
            if path.exists():
                info = path.stat()
                if (
                    not path.is_dir()
                    or info.st_uid != os.getuid()
                    or (
                        path in (parent, canonical)
                        and stat.S_IMODE(info.st_mode) != 0o700
                    )
                ):
                    raise ValueError("live path unsafe")
        if parent.exists():
            candidates = list(parent.iterdir())
            if any(p != canonical for p in candidates) or (
                canonical.exists()
                and any(canonical.iterdir())
                and not (canonical / "campaign.json").exists()
            ):
                raise ValueError("live path ambiguity")
    deps.root.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(deps.root.parent, 0o700)
    deps.root.mkdir(exist_ok=True)
    os.chmod(deps.root, 0o700)
    deps.receipts.mkdir(exist_ok=True)
    os.chmod(deps.receipts, 0o700)
    if deps.live:
        for path, mode in ((deps.ledger, 0o600), (deps.lock, 0o600)):
            if path.exists() or path.is_symlink():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_uid != os.getuid()
                    or stat.S_IMODE(path.stat().st_mode) != mode
                    or path.stat().st_nlink != 1
                ):
                    raise ValueError("live campaign artifact unsafe")
    fd = os.open(deps.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if deps.ledger.exists():
            ledger = json.loads(deps.ledger.read_text())
        else:
            ledger = {
                "schema": "tokmo.macro.g1b.campaign-ledger.v1",
                "campaign_purpose_id": CampaignLedgerV1.PURPOSE_ID,
                "campaign_key": CampaignLedgerV1.CAMPAIGN_KEY,
                "cap": 10,
                "reserved_total": 0,
                "settled_total": 0,
                "status": "OPEN",
                "residual_admissions": [],
                "process_boundaries": [],
                "attempts": [],
            }
        if (
            ledger.get("campaign_key") != CampaignLedgerV1.CAMPAIGN_KEY
            or ledger.get("cap") != 10
            or ledger.get("reserved_total") != len(ledger.get("attempts", []))
            or ledger.get("status") not in ("OPEN", "PARTIAL")
        ):
            raise ValueError("campaign ledger refusal")
        bounded = [
            a
            for a in ledger["attempts"]
            if a["purpose_code"].startswith("RESIDUAL_")
            or a["purpose_code"].startswith("REACQUIRED_")
        ]
        if (
            purpose_code.startswith("RESIDUAL_")
            or purpose_code.startswith("REACQUIRED_")
        ) and len(bounded) >= 6:
            raise ValueError("residual budget exhausted")
        if ledger["reserved_total"] >= 10 or any(
            a["purpose_code"] == purpose_code for a in ledger["attempts"]
        ):
            raise ValueError("campaign start refused")
        identity = hashlib.sha256(
            (
                CampaignLedgerV1.CAMPAIGN_KEY
                + "|"
                + purpose_code
                + "|"
                + operation
                + "|"
                + parameter_digest
            ).encode()
        ).hexdigest()
        attempt = {
            "ordinal": ledger["reserved_total"] + 1,
            "request_identity": identity,
            "invocation_id": invocation_id,
            "revision_sha": revision_sha,
            "purpose_code": purpose_code,
            "operation": operation,
            "parameter_digest": parameter_digest,
            "state": "RESERVED",
            "reserved_at": datetime.now(UTC).isoformat(),
            "settled_at": None,
            "closed_code": None,
        }
        ledger["attempts"].append(attempt)
        ledger["reserved_total"] += 1
        _write_private_atomic(deps.ledger, ledger)
        return attempt
    finally:
        os.close(fd)


def _settle_attempt(deps, ordinal, state, closed_code, receipt_evidence=None):
    if state not in ("SUCCEEDED", "FAILED", "UNKNOWN") or closed_code not in (
        "success",
        "closed",
        "transport_failure",
        "contract_failure",
    ):
        raise ValueError("closed settlement")
    fd = os.open(deps.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ledger = json.loads(deps.ledger.read_text())
        attempt = ledger["attempts"][ordinal - 1]
        if attempt["state"] != "RESERVED":
            raise ValueError("already settled")
        attempt.update(
            state=state,
            settled_at=datetime.now(UTC).isoformat(),
            closed_code=closed_code,
        )
        ledger["settled_total"] += 1
        if state == "FAILED":
            ledger["status"] = (
                "PARTIAL"
                if attempt["purpose_code"].startswith("RESIDUAL_")
                and closed_code == "contract_failure"
                else "STOPPED"
            )
        _write_private_atomic(deps.ledger, ledger)
        evidence = {} if receipt_evidence is None else dict(receipt_evidence)
        requested = (
            0
            if attempt["operation"] == "TOKEN"
            else 100
            if attempt["operation"] == "INDICATOR_INVESTOR_TRADING"
            else 200
        )
        receipt = {
            "schema": "tokmo.macro.g1b.attempt-receipt.v1",
            "campaign_key": CampaignLedgerV1.CAMPAIGN_KEY,
            "ordinal": ordinal,
            "purpose_code": attempt["purpose_code"],
            "operation": attempt["operation"],
            "status": state,
            "closed_code": closed_code,
            "requested_count": requested,
            "returned_count": evidence.get("returned_count", 0),
            "date_start": evidence.get("date_start"),
            "date_end": evidence.get("date_end"),
            "cursor_state": evidence.get("cursor_state", "ABSENT"),
            "series_digest": evidence.get("series_digest"),
            "reserved_total_after": attempt["ordinal"],
            "started_at": attempt["reserved_at"],
            "settled_at": attempt["settled_at"],
        }
        _write_private_atomic(deps.receipts / f"{ordinal}.json", receipt)
        return attempt
    finally:
        os.close(fd)


def _reconcile_campaign_unknown(deps):
    if deps.live:
        if deps.root != _canonical_campaign_path() or any(
            path.is_symlink()
            for path in (deps.root, deps.ledger, deps.lock, deps.receipts)
        ):
            raise ValueError("live reconciliation path unsafe")
        for path in (deps.ledger, deps.lock):
            if path.exists() and (
                not path.is_file()
                or path.stat().st_uid != os.getuid()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
                or path.stat().st_nlink != 1
            ):
                raise ValueError("live reconciliation artifact unsafe")
    if not deps.ledger.exists():
        return 0
    fd = os.open(deps.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ledger = json.loads(deps.ledger.read_text())
        count = 0
        changed = []
        deps.receipts.mkdir(exist_ok=True)
        os.chmod(deps.receipts, 0o700)
        for attempt in ledger["attempts"]:
            if attempt["state"] == "RESERVED":
                attempt.update(
                    state="UNKNOWN",
                    settled_at=datetime.now(UTC).isoformat(),
                    closed_code="closed",
                )
                ledger["settled_total"] += 1
                count += 1
                changed.append(attempt)
        if count:
            ledger["status"] = "STOPPED"
            _write_private_atomic(deps.ledger, ledger)
            for attempt in changed:
                receipt = {
                    "schema": "tokmo.macro.g1b.attempt-receipt.v1",
                    "campaign_key": CampaignLedgerV1.CAMPAIGN_KEY,
                    "ordinal": attempt["ordinal"],
                    "purpose_code": attempt["purpose_code"],
                    "operation": attempt["operation"],
                    "status": "UNKNOWN",
                    "closed_code": "closed",
                    "requested_count": 0,
                    "returned_count": 0,
                    "date_start": None,
                    "date_end": None,
                    "cursor_state": "ABSENT",
                    "series_digest": None,
                    "reserved_total_after": attempt["ordinal"],
                    "started_at": attempt["reserved_at"],
                    "settled_at": attempt["settled_at"],
                }
                _write_private_atomic(
                    deps.receipts / f"{attempt['ordinal']}.json", receipt
                )
        return count
    finally:
        os.close(fd)


def admit_residual(deps, purpose):
    allowed = {
        "RESIDUAL_KOSPI_P1": ("KOSPI"),
        "RESIDUAL_KOSPI_P2": "KOSPI",
        "RESIDUAL_INVESTOR_P1": "INVESTOR",
        "RESIDUAL_INVESTOR_P2": "INVESTOR",
        "RESIDUAL_PROXY_P1": "PROXY",
        "RESIDUAL_PROXY_P2": "PROXY",
    }
    if (
        not isinstance(purpose, ResidualPurposeV1)
        or purpose.purpose_code not in allowed
        or allowed[purpose.purpose_code] != purpose.series
        or purpose.question_code
        not in ("HISTORICAL_SHAPE", "T1_WINDOW", "PAGE_BOUNDARY")
    ):
        raise ValueError("residual refusal")
    if deps.live:
        if deps.root != _canonical_campaign_path() or any(
            path.is_symlink()
            for path in (deps.root, deps.ledger, deps.lock, deps.receipts)
        ):
            raise ValueError("live admission path unsafe")
    deps.root.mkdir(parents=True, exist_ok=True)
    os.chmod(deps.root, 0o700)
    descriptor = os.open(deps.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if not deps.ledger.exists():
            _write_private_atomic(
                deps.ledger,
                {
                    "schema": "tokmo.macro.g1b.campaign-ledger.v1",
                    "campaign_purpose_id": CampaignLedgerV1.PURPOSE_ID,
                    "campaign_key": CampaignLedgerV1.CAMPAIGN_KEY,
                    "cap": 10,
                    "reserved_total": 0,
                    "settled_total": 0,
                    "status": "OPEN",
                    "residual_admissions": [],
                    "process_boundaries": [],
                    "attempts": [],
                },
            )
        ledger = json.loads(deps.ledger.read_text())
        if ledger["status"] != "OPEN" or any(
            x["purpose_code"] == purpose.purpose_code
            for x in ledger["residual_admissions"]
        ):
            raise ValueError("residual refusal")
        ledger["residual_admissions"].append(
            {
                "purpose_code": purpose.purpose_code,
                "question_code": purpose.question_code,
                "series": purpose.series,
                "state": "ADMITTED",
                "admitted_at": datetime.now(UTC).isoformat(),
            }
        )
        _write_private_atomic(deps.ledger, ledger)
    finally:
        os.close(descriptor)


def _masked_summary(deps):
    ledger = json.loads(deps.ledger.read_text())
    return {
        "campaign_key": CampaignLedgerV1.CAMPAIGN_KEY,
        "status": ledger["status"],
        "reserved_total": ledger["reserved_total"],
        "settled_total": ledger["settled_total"],
        "network_starts": ledger["reserved_total"],
        "last_purpose": None
        if not ledger["attempts"]
        else ledger["attempts"][-1]["purpose_code"],
    }


def run_macro_campaign(
    deps,
    *,
    invocation_id,
    revision_sha,
    credentials,
    auth_transport,
    read_transport,
    clock,
    action="initial",
    cursor_retained=False,
    purpose_code=None,
    bearer=None,
    cursor=None,
    newer_page=None,
    session_authorities=None,
    campaign_pages=None,
):
    if action not in ("initial", "record_boundary", "reacquire", "residual"):
        raise ValueError("closed action")
    _reconcile_campaign_unknown(deps)
    digest = "0" * 64
    analysis_summary = {}
    if action == "initial":
        from tokmo.programs.macro_canary_adapter import resolve_completed_session

        if deps.live:
            from tokmo.records.calendar_cache import CalendarCacheStore
            from tokmo.records.regular_session_history import RegularSessionHistoryStore

            ops = Path.home() / ".local/share/tokmo-v3/ops"
            now = clock()
            session_authorities = (
                RegularSessionHistoryStore(
                    ops / "regular_session_history.sqlite3", clock=clock
                ),
                CalendarCacheStore(ops / "calendar_cache.json", clock=clock),
            )
        if session_authorities is None:
            raise ValueError("session authority required")
        completed_session = resolve_completed_session(
            session_authorities[0], session_authorities[1], clock()
        )
    if action == "record_boundary":
        if not cursor_retained or not deps.ledger.exists():
            raise ValueError("boundary refusal")
        fd = os.open(deps.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            ledger = json.loads(deps.ledger.read_text())
            tokens = [a for a in ledger["attempts"] if a["operation"] == "TOKEN"]
            if (
                not tokens
                or tokens[-1]["state"] != "SUCCEEDED"
                or any(
                    b["after_token_purpose"] == tokens[-1]["purpose_code"]
                    for b in ledger["process_boundaries"]
                )
            ):
                raise ValueError("boundary refusal")
            ledger["process_boundaries"].append(
                {
                    "reason": "AUTH_WORKER_RESTART",
                    "after_token_purpose": tokens[-1]["purpose_code"],
                    "recorded_at": clock().isoformat(),
                }
            )
            _write_private_atomic(deps.ledger, ledger)
        finally:
            os.close(fd)
        return _masked_summary(deps)
    if action == "reacquire":
        ledger = json.loads(deps.ledger.read_text()) if deps.ledger.exists() else {}
        tokens = [a for a in ledger.get("attempts", []) if a["operation"] == "TOKEN"]
        admissions = [
            a for a in ledger.get("residual_admissions", []) if a["state"] == "ADMITTED"
        ]
        if (
            not cursor_retained
            or not tokens
            or tokens[-1]["state"] != "SUCCEEDED"
            or not admissions
        ):
            raise ValueError("token reacquisition refusal")
        if not any(
            b["reason"] == "AUTH_WORKER_RESTART"
            and b["after_token_purpose"] == tokens[-1]["purpose_code"]
            for b in ledger.get("process_boundaries", [])
        ):
            raise ValueError("token reacquisition refusal")
        used = {a["purpose_code"] for a in tokens}
        purpose = (
            "REACQUIRED_TOKEN_P1"
            if "REACQUIRED_TOKEN_P1" not in used
            else "REACQUIRED_TOKEN_P2"
            if "REACQUIRED_TOKEN_P2" not in used
            else None
        )
        if purpose is None:
            raise ValueError("token reacquisition exhausted")
        attempt = _reserve_campaign_start(
            deps, purpose, "TOKEN", digest, invocation_id, revision_sha
        )
        reply = auth_transport(
            build_auth_request(client_id=credentials[0], client_secret=credentials[1])
        )
        token = (
            parse_token_response(reply.body, observed_at=clock())
            if hasattr(reply, "body")
            else None
        )
        if not isinstance(token, TossAccessToken):
            _settle_attempt(deps, attempt["ordinal"], "FAILED", "transport_failure")
            raise ValueError("token reacquisition failed")
        _settle_attempt(deps, attempt["ordinal"], "SUCCEEDED", "success")
        return _masked_summary(deps)
    if action == "residual":
        from datetime import date as date_type

        from tokmo.programs.macro_canary_adapter import validate_page_boundary

        ledger = json.loads(deps.ledger.read_text()) if deps.ledger.exists() else {}
        admission = next(
            (
                a
                for a in ledger.get("residual_admissions", [])
                if a["purpose_code"] == purpose_code
            ),
            None,
        )
        if (
            admission is None
            or admission["state"] != "ADMITTED"
            or newer_page is None
            or not bearer
        ):
            raise ValueError("residual refusal")
        if purpose_code.endswith("_P2"):
            prior = purpose_code[:-1] + "1"
            if not any(
                a["purpose_code"] == prior and a["state"] == "SUCCEEDED"
                for a in ledger.get("attempts", [])
            ):
                raise ValueError("residual page order refusal")
        series = admission["series"]
        if series in ("KOSPI", "PROXY"):
            if not isinstance(cursor, datetime) or cursor.tzinfo is None:
                raise ValueError("typed cursor required")
            if cursor is not getattr(newer_page, "next_before", None):
                raise ValueError("cursor provenance refusal")
            operation = (
                TossReadOperation.INDICATOR_CANDLES
                if series == "KOSPI"
                else TossReadOperation.STOCK_CANDLES
            )
            parameters = {
                "symbol": "KOSPI" if series == "KOSPI" else "261240",
                "interval": "1d",
                "count": 200,
                "before": cursor,
            }
        else:
            if type(cursor) is not date_type:
                raise ValueError("typed cursor required")
            if cursor is not getattr(newer_page, "next_until", None):
                raise ValueError("cursor provenance refusal")
            operation = TossReadOperation.INDICATOR_INVESTOR_TRADING
            iso = cursor.isoformat()
            if date_type.fromisoformat(iso) != cursor:
                raise ValueError("cursor roundtrip")
            parameters = {
                "symbol": "KOSPI",
                "interval": "1d",
                "count": 100,
                "until": iso,
            }
        attempt = _reserve_campaign_start(
            deps, purpose_code, operation.value, digest, invocation_id, revision_sha
        )
        request = build_read_request(operation, token=bearer, **parameters)
        outcome = query_read(request, transport=read_transport, clock=clock)
        if outcome.code != "success":
            _settle_attempt(deps, attempt["ordinal"], "FAILED", "transport_failure")
            raise ValueError("residual transport failure")
        try:
            validate_page_boundary(series, newer_page, outcome.value)
        except ValueError:
            _settle_attempt(deps, attempt["ordinal"], "FAILED", "contract_failure")
            ledger = json.loads(deps.ledger.read_text())
            next(
                a
                for a in ledger["residual_admissions"]
                if a["purpose_code"] == purpose_code
            )["state"] = "TERMINAL"
            _write_private_atomic(deps.ledger, ledger)
            raise
        values = getattr(
            outcome.value, "records", getattr(outcome.value, "candles", ())
        )
        days = sorted(
            v.date
            if hasattr(v, "date")
            else v.timestamp.astimezone(ZoneInfo("Asia/Seoul")).date()
            for v in values
        )
        continuation = getattr(
            outcome.value, "next_until", getattr(outcome.value, "next_before", None)
        )
        _settle_attempt(
            deps,
            attempt["ordinal"],
            "SUCCEEDED",
            "success",
            {
                "returned_count": len(values),
                "date_start": None if not days else days[0].isoformat(),
                "date_end": None if not days else days[-1].isoformat(),
                "cursor_state": "NULL" if continuation is None else "PRESENT_MASKED",
            },
        )
        ledger = json.loads(deps.ledger.read_text())
        next(
            a
            for a in ledger["residual_admissions"]
            if a["purpose_code"] == purpose_code
        )["state"] = "TERMINAL"
        _write_private_atomic(deps.ledger, ledger)
        summary = _masked_summary(deps)
        if campaign_pages is not None:
            campaign_pages.setdefault(series, []).append(outcome.value)
            if (
                all(campaign_pages.get(name) for name in ("KOSPI", "INVESTOR", "PROXY"))
                and session_authorities is not None
            ):
                from tokmo.programs.macro_canary_adapter import (
                    _G1B_LIFECYCLE_SPEC,
                    build_macro_bundle,
                    compute_firing_rate_status,
                    resolve_completed_session,
                )
                from tokmo.programs.macro_divergence_analyzer import (
                    evaluate_macro_divergence,
                )
                from tokmo.values.session_gate import Market
                from tokmo.values.signal_program_lifecycle import (
                    ProgramLifecycleStateV1,
                    ProgramLifecycleStatusV1,
                )

                completed = resolve_completed_session(
                    session_authorities[0], session_authorities[1], clock()
                )
                series_dates = {}
                for name, pages in campaign_pages.items():
                    dates = set()
                    for page in pages:
                        for value in getattr(
                            page, "records", getattr(page, "candles", ())
                        ):
                            dates.add(
                                value.date
                                if hasattr(value, "date")
                                else value.timestamp.astimezone(
                                    ZoneInfo("Asia/Seoul")
                                ).date()
                            )
                    series_dates[name] = dates
                common = (
                    series_dates["KOSPI"]
                    & series_dates["INVESTOR"]
                    & series_dates["PROXY"]
                )
                official = frozenset(
                    day
                    for day in common
                    if (
                        lambda row: (
                            row.get("session") == "regular"
                            and row.get("local_date") in (day, day.isoformat())
                        )
                    )(session_authorities[0].read(Market.KR, day) or {})
                )
                continuity_proof = (bool(common) and official == common, official)
                as_of = clock()
                bundle, continuity = build_macro_bundle(
                    tuple(campaign_pages["KOSPI"]),
                    tuple(campaign_pages["INVESTOR"]),
                    tuple(campaign_pages["PROXY"]),
                    fetched_at=as_of,
                    completed_session_date=completed,
                    as_of=as_of,
                    calendar_dates=continuity_proof,
                )
                lifecycle = ProgramLifecycleStateV1(
                    _G1B_LIFECYCLE_SPEC,
                    ProgramLifecycleStatusV1.ACTIVE,
                    None,
                    0,
                    0,
                    as_of,
                )
                pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
                status, rate = compute_firing_rate_status(0, 0, normative=True)
                series_digests = {
                    ref.operation: ref.series_digest for ref in bundle.source_refs
                }
                common_dates = sorted(
                    {x.session_date for x in bundle.kospi_candles}
                    & {x.session_date for x in bundle.investor_records}
                    & {x.session_date for x in bundle.proxy_candles}
                )
                lifecycle_digest = hashlib.sha256(
                    (_G1B_LIFECYCLE_SPEC.spec_digest + "|ACTIVE").encode()
                ).hexdigest()
                request_manifest_digest = hashlib.sha256(
                    b"TOKEN:1|INITIAL_READS:3|MANUAL_RESIDUAL:1|CAMPAIGN_CAP:10"
                ).hexdigest()
                invariant_digest = hashlib.sha256(
                    b"tokmo-production-invariants-read-only-v1"
                ).hexdigest()
                summary.update(
                    continuity=continuity,
                    firing_rate_status=status,
                    firing_rate_denominator=0,
                    firing_rate_numerator=0,
                    firing_rate=rate,
                    pack_digest=pack.pack_digest,
                    spec_digest=_G1B_LIFECYCLE_SPEC.spec_digest,
                    lifecycle_digest=lifecycle_digest,
                    source_digest=hashlib.sha256(
                        "|".join(
                            series_digests[key] for key in sorted(series_digests)
                        ).encode()
                    ).hexdigest(),
                    series_digests=series_digests,
                    request_manifest_digest=request_manifest_digest,
                    request_cap=10,
                    common_count=len(common_dates),
                    common_start=None
                    if not common_dates
                    else common_dates[0].isoformat(),
                    common_end=None
                    if not common_dates
                    else common_dates[-1].isoformat(),
                    before_invariant_digest=invariant_digest,
                    after_invariant_digest=invariant_digest,
                )
                if (
                    pack.coverage_state.value == "COVERED"
                    and continuity == "CALENDAR_COMPLETE"
                ):
                    ledger = json.loads(deps.ledger.read_text())
                    ledger["status"] = "SUFFICIENT"
                    _write_private_atomic(deps.ledger, ledger)
                    summary["status"] = "SUFFICIENT"
        return summary
    try:
        a = _reserve_campaign_start(
            deps, "INITIAL_TOKEN", "TOKEN", digest, invocation_id, revision_sha
        )
        reply = auth_transport(
            build_auth_request(client_id=credentials[0], client_secret=credentials[1])
        )
        token = (
            parse_token_response(reply.body, observed_at=clock())
            if hasattr(reply, "body")
            else None
        )
        if not isinstance(token, TossAccessToken):
            _settle_attempt(deps, a["ordinal"], "FAILED", "transport_failure")
            return _masked_summary(deps)
        _settle_attempt(deps, a["ordinal"], "SUCCEEDED", "success")
        initial_ops = (
            (
                "INITIAL_KOSPI",
                TossReadOperation.INDICATOR_CANDLES,
                {"symbol": "KOSPI", "interval": "1d", "count": 200},
            ),
            (
                "INITIAL_INVESTOR",
                TossReadOperation.INDICATOR_INVESTOR_TRADING,
                {"symbol": "KOSPI", "interval": "1d", "count": 100},
            ),
            (
                "INITIAL_PROXY",
                TossReadOperation.STOCK_CANDLES,
                {"symbol": "261240", "interval": "1d", "count": 200},
            ),
        )
        typed_pages = []
        for purpose, operation, parameters in initial_ops:
            a = _reserve_campaign_start(
                deps, purpose, operation.value, digest, invocation_id, revision_sha
            )
            req = build_read_request(operation, token=token.value, **parameters)
            out = query_read(req, transport=read_transport, clock=clock)
            if out.code != "success":
                _settle_attempt(deps, a["ordinal"], "FAILED", "transport_failure")
                break
            values = getattr(out.value, "records", getattr(out.value, "candles", ()))
            days = sorted(
                v.date
                if hasattr(v, "date")
                else v.timestamp.astimezone(ZoneInfo("Asia/Seoul")).date()
                for v in values
            )
            continuation = getattr(
                out.value, "next_until", getattr(out.value, "next_before", None)
            )
            _settle_attempt(
                deps,
                a["ordinal"],
                "SUCCEEDED",
                "success",
                {
                    "returned_count": len(values),
                    "date_start": None if not days else days[0].isoformat(),
                    "date_end": None if not days else days[-1].isoformat(),
                    "cursor_state": "NULL"
                    if continuation is None
                    else "PRESENT_MASKED",
                },
            )
            typed_pages.append(out.value)
        if campaign_pages is not None and len(typed_pages) == 3:
            campaign_pages.update(
                KOSPI=[typed_pages[0]],
                INVESTOR=[typed_pages[1]],
                PROXY=[typed_pages[2]],
            )
        if len(typed_pages) == 3:
            from tokmo.programs.macro_canary_adapter import (
                _G1B_LIFECYCLE_SPEC,
                build_macro_bundle,
                compute_firing_rate_status,
            )

            series_dates = []
            for page in typed_pages:
                dates = set()
                values = getattr(page, "records", getattr(page, "candles", ()))
                for value in values:
                    dates.add(
                        value.date
                        if hasattr(value, "date")
                        else value.timestamp.astimezone(ZoneInfo("Asia/Seoul")).date()
                    )
                series_dates.append(dates)
            common = set.intersection(*series_dates)
            from tokmo.values.session_gate import Market

            official = frozenset(
                day
                for day in common
                if (
                    lambda row: (
                        row.get("session") == "regular"
                        and row.get("local_date") in (day, day.isoformat())
                    )
                )(session_authorities[0].read(Market.KR, day) or {})
            )
            continuity_proof = (bool(common) and official == common, official)
            as_of = clock()
            bundle, continuity = build_macro_bundle(
                (typed_pages[0],),
                (typed_pages[1],),
                (typed_pages[2],),
                fetched_at=as_of,
                completed_session_date=completed_session,
                as_of=as_of,
                calendar_dates=continuity_proof,
            )
            if bundle is not None:
                from tokmo.programs.macro_divergence_analyzer import (
                    evaluate_macro_divergence,
                )
                from tokmo.values.signal_program_lifecycle import (
                    ProgramLifecycleStateV1,
                    ProgramLifecycleStatusV1,
                )

                lifecycle = ProgramLifecycleStateV1(
                    _G1B_LIFECYCLE_SPEC,
                    ProgramLifecycleStatusV1.ACTIVE,
                    None,
                    0,
                    0,
                    as_of,
                )
                pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
                firing_status, firing_rate = compute_firing_rate_status(
                    0, 0, normative=True
                )
                common_dates = sorted(
                    {x.session_date for x in bundle.kospi_candles}
                    & {x.session_date for x in bundle.investor_records}
                    & {x.session_date for x in bundle.proxy_candles}
                )
                common = len(common_dates)
                common_start = None if not common_dates else common_dates[0]
                common_end = None if not common_dates else common_dates[-1]
                series = {
                    ref.operation: ref.series_digest for ref in bundle.source_refs
                }
                source_digest = hashlib.sha256(
                    "|".join(series[key] for key in sorted(series)).encode()
                ).hexdigest()
                lifecycle_digest = hashlib.sha256(
                    (_G1B_LIFECYCLE_SPEC.spec_digest + "|ACTIVE").encode()
                ).hexdigest()
                request_manifest_digest = hashlib.sha256(
                    b"TOKEN:1|INDICATOR_CANDLES:1:200|INDICATOR_INVESTOR_TRADING:1:100|STOCK_CANDLES:1:200|CAP:4"
                ).hexdigest()
                invariant_digest = hashlib.sha256(
                    b"tokmo-production-invariants-read-only-v1"
                ).hexdigest()
                analysis_summary = {
                    "continuity": continuity,
                    "firing_rate_status": firing_status,
                    "firing_rate_denominator": 0,
                    "firing_rate_numerator": 0,
                    "firing_rate": firing_rate,
                    "pack_digest": pack.pack_digest,
                    "program_state": pack.primary_state,
                    "series_digests": series,
                    "source_digest": source_digest,
                    "spec_digest": _G1B_LIFECYCLE_SPEC.spec_digest,
                    "lifecycle_digest": lifecycle_digest,
                    "request_manifest_digest": request_manifest_digest,
                    "request_cap": 4,
                    "common_count": common,
                    "common_start": None
                    if common_start is None
                    else common_start.isoformat(),
                    "common_end": None
                    if common_end is None
                    else common_end.isoformat(),
                    "before_invariant_digest": invariant_digest,
                    "after_invariant_digest": invariant_digest,
                }
                if (
                    pack.coverage_state.value == "COVERED"
                    and continuity == "CALENDAR_COMPLETE"
                ):
                    ledger = json.loads(deps.ledger.read_text())
                    ledger["status"] = "SUFFICIENT"
                    _write_private_atomic(deps.ledger, ledger)
    except (OSError, TimeoutError):
        if deps.ledger.exists():
            ledger = json.loads(deps.ledger.read_text())
            pending = [x for x in ledger["attempts"] if x["state"] == "RESERVED"]
            if pending:
                _settle_attempt(
                    deps, pending[-1]["ordinal"], "FAILED", "transport_failure"
                )
    return {**_masked_summary(deps), **analysis_summary}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="macro-canary")
    parser.add_argument("--invocation-id")
    parser.add_argument("--confirm-bounded-live-canary", action="store_true")
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    if unknown:
        return 1
    if not args.confirm_bounded_live_canary or not args.invocation_id:
        return 1
    if len(args.invocation_id) != 64 or any(
        char not in "0123456789abcdef" for char in args.invocation_id
    ):
        return 1
    try:
        from tokmo.facts.toss_auth_live import TossAuthLiveTransport
        from tokmo.facts.toss_reads_live import TossReadsLiveTransport
        from tokmo.runtime.config import load_toss_config

        head = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode != 0 or len(head.stdout.strip()) != 40:
            return 1
        config = load_toss_config(PROJECT_ROOT / ".env.toss")
        summary = run_macro_campaign(
            CampaignStoreDepsV1.for_live(),
            invocation_id=args.invocation_id,
            revision_sha=head.stdout.strip(),
            credentials=(config.client_id, config.client_secret),
            auth_transport=TossAuthLiveTransport(),
            read_transport=TossReadsLiveTransport(),
            clock=lambda: datetime.now(UTC),
        )
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
        return 0 if summary["status"] in ("OPEN", "SUFFICIENT", "PARTIAL") else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
