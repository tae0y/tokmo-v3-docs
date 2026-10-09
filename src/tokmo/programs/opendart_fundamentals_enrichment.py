"""Bulk-triggered KR fundamentals enrichment (fundamentals SDS §3–§8).

Pure orchestration over injected transport/stores/clock: after the bulk
metadata collection settles inside one natural tick, newly persisted KR
annual business reports of HOLDING ∪ active KR WATCHLIST issuers are looked
up once against the pinned official fundamentals endpoints and the sanitized
closed-set rows are persisted as reusable evidence. This module imports no
live transport; the composition root wires the real adapter.

The closed field-set constants are re-declared source-locally (the EP-1
precedent); an equality test against the reviewed source-contract sets makes
drift refuse.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from tokmo.facts.opendart import (
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
)
from tokmo.facts.opendart_source_contract import OpenDartTraceJsonRequest
from tokmo.programs.opendart_pilot import TICK_DEADLINE
from tokmo.records.opendart_fundamentals_store import (
    NON_TERMINAL_STATUSES,
    STATUS_FAILED_EXHAUSTED,
    STATUS_FAILED_RETRYABLE,
    STATUS_FAILED_ROW_BOUND,
    STATUS_IN_ATTEMPT,
    STATUS_NO_DATA,
    STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR,
    STATUS_SKIPPED_YEAR_UNPARSEABLE,
    STATUS_SUCCESS,
    TERMINAL_STATUSES,
    CapReachedError,
)

__all__ = [
    "NON_TERMINAL_STATUSES",
    "TERMINAL_STATUSES",
    "PeriodicReportClass",
    "classify_periodic_report",
    "resolves_major_statement",
    "run_fundamentals_enrichment",
    "sanitize_fundamentals_rows",
]

# SDS §5 cardinality and §3.3/§7 bounds.
MAX_REPORTS_PER_TICK = 2
MAX_STARTS_PER_REPORT = 2
MAX_ATTEMPTS_PER_REPORT = 3
REATTEMPT_SPACING_HOURS = 24
SCAN_WINDOW_DAYS = 14
SCAN_LIMIT = 200
MAX_PERSISTED_ROWS = 1024
STALE_ATTEMPT_MINUTES = 60
# SDS §5 (m-1) execution budget: the floor an exhausted bulk deadline still
# owes enrichment, and the hard cap that keeps the worst case safely inside
# the 30-minute cadence.
ENRICHMENT_MIN_BUDGET_SECONDS = 120
TICK_HARD_CAP_MINUTES = 25

# Source-local re-declaration of the reviewed EP-1 closed field sets (§6):
# the 18 sanitized row fields plus the observed official amount fields.
PERSISTED_FIELD_NAMES = frozenset(
    {
        "corp_code",
        "corp_name",
        "stock_code",
        "corp_cls",
        "rcept_no",
        "reprt_code",
        "bsns_year",
        "sj_div",
        "account_id",
        "account_nm",
        "fs_div",
        "thstrm_nm",
        "frmtrm_nm",
        "bfefrmtrm_nm",
        "se",
        "stock_knd",
        "istc_totqy",
        "distb_stock_co",
        "thstrm_amount",
        "frmtrm_amount",
        "bfefrmtrm_amount",
        "thstrm_add_amount",
        "frmtrm_add_amount",
    }
)

CLASS_ANNUAL = "annual"
CLASS_NONSTANDARD_FISCAL_YEAR = "nonstandard_fiscal_year"
CLASS_YEAR_UNPARSEABLE = "year_unparseable"
CLASS_NOT_A_CANDIDATE = "not_candidate"

# SDS §3.1: leading bracketed tags (e.g. corrections) strip, then the
# remainder must be exactly the annual business report grammar.
_TAG_PREFIX_PATTERN = re.compile(r"^(?:\[[^\[\]]{1,20}\])*")
ANNUAL_REPORT_PATTERN = re.compile(r"사업보고서\s*\((\d{4})\.(\d{2})\)")

_SINGLE_ENDPOINT = "fnlttSinglAcnt"
_ALL_ENDPOINT = "fnlttSinglAcntAll"


def enrichment_deadline(now: datetime, enrichment_start: datetime) -> datetime:
    """SDS §5 (m-1) execution budget: even an exhausted bulk deadline owes
    enrichment a bounded floor, and the hard cap keeps the worst case safely
    inside the 30-minute cadence."""
    return min(
        now + timedelta(minutes=TICK_HARD_CAP_MINUTES),
        max(
            now + TICK_DEADLINE,
            enrichment_start + timedelta(seconds=ENRICHMENT_MIN_BUDGET_SECONDS),
        ),
    )


@dataclass(frozen=True)
class PeriodicReportClass:
    kind: str
    bsns_year: str | None = None


def classify_periodic_report(
    report_nm: str,
    rcept_dt: str,
) -> PeriodicReportClass:
    """Deterministic §3.1 trigger matcher; mismatches settle, never guess."""
    remainder = _TAG_PREFIX_PATTERN.sub("", report_nm or "", count=1).strip()
    match = ANNUAL_REPORT_PATTERN.fullmatch(remainder)
    if match is None:
        return PeriodicReportClass(CLASS_NOT_A_CANDIDATE)
    year_text, month_text = match.groups()
    if month_text != "12":
        # (YYYY.MM != 12) → bsns_year mapping is UNVERIFIED (gate G-U1);
        # the parsed year is kept as evidence, never as a request value.
        return PeriodicReportClass(CLASS_NONSTANDARD_FISCAL_YEAR, year_text)
    if len(rcept_dt or "") != 8 or not rcept_dt.isdigit():
        return PeriodicReportClass(CLASS_YEAR_UNPARSEABLE)
    rcept_year = int(rcept_dt[:4])
    if not rcept_year - 2 <= int(year_text) <= rcept_year:
        return PeriodicReportClass(CLASS_YEAR_UNPARSEABLE)
    return PeriodicReportClass(CLASS_ANNUAL, year_text)


def sanitize_fundamentals_rows(body_rows) -> tuple[str, ...]:
    """Keep only string-valued keys of the closed field set; provider
    message text never persists (§6)."""
    sanitized = []
    for row in body_rows:
        if not isinstance(row, dict):
            continue
        kept = {
            key: value
            for key, value in row.items()
            if key in PERSISTED_FIELD_NAMES and isinstance(value, str)
        }
        if kept:
            sanitized.append(
                json.dumps(
                    kept, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )
            )
    return tuple(sanitized)


def resolves_major_statement(rows) -> bool:
    """Proven EP-1 predicate: a body is major-resolved iff some row carries
    fs_div=="CFS" with a truthy account_id (§2)."""
    return any(
        isinstance(row, dict) and row.get("fs_div") == "CFS" and row.get("account_id")
        for row in rows
    )


def _request_identity(endpoint: str, corp_code: str, bsns_year: str) -> str:
    suffix = f"corp_code={corp_code}&bsns_year={bsns_year}&reprt_code=11011"
    if endpoint == _ALL_ENDPOINT:
        suffix += "&fs_div=CFS"
    return f"GET opendart.fss.or.kr/api/{endpoint}.json?{suffix}"


def _judge_outcome(outcome, corp_code: str):
    """Map one transport outcome onto the closed dispositions.

    Returns (kind, code, body_sha256, rows) with kind in
    {transport_failure, schema_drift, provider_stop, no_data,
    identity_disagreement, official_rows}.
    """
    if isinstance(outcome, _OpenDartTransportFailure):
        return "transport_failure", outcome.code, None, ()
    if not isinstance(outcome, _OpenDartTransportSuccess):
        return "transport_failure", "protocol_failure", None, ()
    digest = hashlib.sha256(outcome.body).hexdigest()
    try:
        payload = json.loads(outcome.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "schema_drift", "schema_drift", digest, ()
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return "schema_drift", "schema_drift", digest, ()
    status = payload["status"]
    rows = payload.get("list", [])
    if not isinstance(rows, list):
        return "schema_drift", "schema_drift", digest, ()
    if status == "013":
        return "no_data", "provider_no_data", digest, ()
    if status != "000":
        if len(status) == 3 and status.isdigit():
            return "provider_stop", f"provider_{status}", digest, ()
        return "schema_drift", "schema_drift", digest, ()
    for row in rows:
        if isinstance(row, dict) and row.get("corp_code") not in (None, corp_code):
            # §4.2 response identity guard, proven EP-1 pattern.
            return (
                "identity_disagreement",
                "request_identity_disagreement",
                digest,
                (),
            )
    return "official_rows", "success", digest, tuple(rows)


def _summary(
    status: str, reports_processed: int, starts: int, scope_note: str | None
) -> dict:
    return {
        "status": status,
        "reports_processed": reports_processed,
        "starts": starts,
        "scope_note": scope_note,
    }


def _attempt_report(
    store,
    transport,
    api_key,
    *,
    rcept_no,
    corp_code,
    stock_code,
    corp_name,
    bsns_year,
    provenance,
    prior_attempts,
    clock,
) -> tuple[int, bool]:
    """One bounded §5 report attempt. Returns (starts_used, cap_tripped).

    A CapReachedError from the very first reservation propagates to the
    caller before any durable per-report row exists (m-2).
    """
    attempts_now = prior_attempts + 1
    exhausted = attempts_now >= MAX_ATTEMPTS_PER_REPORT

    def resting_status(*, no_data: bool = False) -> str:
        if exhausted:
            return STATUS_FAILED_EXHAUSTED
        return STATUS_NO_DATA if no_data else STATUS_FAILED_RETRYABLE

    def settle(
        start_id, start_outcome, last_outcome, digest, *, no_data=False, terminal=None
    ):
        store.settle_start(start_id, start_outcome, body_sha256=digest)
        store.settle_report_terminal(
            rcept_no=rcept_no,
            status=terminal if terminal else resting_status(no_data=no_data),
            last_outcome=last_outcome,
            now=clock(),
        )

    def persist(start_id, endpoint, digest, rows) -> None:
        if len(rows) > MAX_PERSISTED_ROWS:
            # Gate G-U3: the true row-count upper bound is UNKNOWN; refuse
            # terminally rather than truncate.
            settle(
                start_id,
                "success",
                "row_bound_exceeded",
                digest,
                terminal=STATUS_FAILED_ROW_BOUND,
            )
            return
        sanitized = sanitize_fundamentals_rows(rows)
        if not sanitized:
            settle(start_id, "success", "empty_official_rows", digest, no_data=True)
            return
        store.settle_start(start_id, "success", body_sha256=digest)
        store.settle_report_terminal(
            rcept_no=rcept_no,
            status=STATUS_SUCCESS,
            last_outcome="success",
            now=clock(),
            endpoint=endpoint,
            payload_rows=sanitized,
        )

    first_id = store.begin_attempt(
        rcept_no=rcept_no,
        corp_code=corp_code,
        stock_code=stock_code,
        corp_name=corp_name,
        bsns_year=bsns_year,
        provenance=provenance,
        endpoint=_SINGLE_ENDPOINT,
        request_identity=_request_identity(_SINGLE_ENDPOINT, corp_code, bsns_year),
        now=clock(),
    )
    outcome = transport.trace_json(
        OpenDartTraceJsonRequest(
            _SINGLE_ENDPOINT, corp_code, bsns_year, api_key=api_key
        )
    )
    kind, code, digest, rows = _judge_outcome(outcome, corp_code)
    if kind == "transport_failure":
        settle(first_id, code, code, None)
        return 1, False
    if kind in ("schema_drift", "provider_stop", "identity_disagreement"):
        start_outcome = "schema_drift" if kind == "schema_drift" else code
        settle(first_id, start_outcome, code, digest)
        return 1, False
    if kind == "no_data":
        settle(first_id, "provider_013", code, digest, no_data=True)
        return 1, False
    if resolves_major_statement(rows):
        persist(first_id, _SINGLE_ENDPOINT, digest, rows)
        return 1, False

    # Major-unresolved single body: the proven single→All fallback (§5).
    try:
        second_id = store.settle_start(
            first_id,
            "success",
            body_sha256=digest,
            reserve_next=(
                rcept_no,
                _ALL_ENDPOINT,
                _request_identity(_ALL_ENDPOINT, corp_code, bsns_year),
            ),
            now=clock(),
        )
    except CapReachedError:
        store.settle_report_terminal(
            rcept_no=rcept_no,
            status=STATUS_FAILED_RETRYABLE,
            last_outcome="cap_reached",
            now=clock(),
        )
        return 1, True
    outcome = transport.trace_json(
        OpenDartTraceJsonRequest(
            _ALL_ENDPOINT, corp_code, bsns_year, fs_div="CFS", api_key=api_key
        )
    )
    kind, code, digest, rows = _judge_outcome(outcome, corp_code)
    if kind == "transport_failure":
        settle(second_id, code, code, None)
        return 2, False
    if kind in ("schema_drift", "provider_stop", "identity_disagreement"):
        start_outcome = "schema_drift" if kind == "schema_drift" else code
        settle(second_id, start_outcome, code, digest)
        return 2, False
    if kind == "no_data":
        settle(second_id, "provider_013", code, digest, no_data=True)
        return 2, False
    if any(
        isinstance(row, dict) and "fs_div" in row and row.get("fs_div") != "CFS"
        for row in rows
    ):
        # 4405f10 contract: missing fs_div is admitted, an explicit
        # non-CFS row refuses.
        settle(second_id, "schema_drift", "schema_drift", digest)
        return 2, False
    persist(second_id, _ALL_ENDPOINT, digest, rows)
    return 2, False


def run_fundamentals_enrichment(
    *,
    bulk_store,
    enrichment_store,
    relevance,
    api_key,
    transport,
    now,
    clock,
    deadline,
    scope_note: str | None = None,
) -> dict:
    """One natural-tick enrichment pass. Returns the closed summary the
    composition root nests under the operate result's ``enrichment`` key."""
    store = enrichment_store
    hard_cap = now + timedelta(minutes=TICK_HARD_CAP_MINUTES)
    if clock() >= hard_cap:
        # §5 deferral: candidates need no durable row — the §3.3 scan
        # re-finds them while they remain inside the window.
        store.append_tick_event(
            at=clock(),
            outcome="deferred_deadline",
            reason="tick_hard_cap_spent",
            reports_processed=0,
            starts=0,
        )
        return _summary("deferred_deadline", 0, 0, scope_note)
    store.reconcile_stale(
        now=clock(), stale_before=clock() - timedelta(minutes=STALE_ATTEMPT_MINUTES)
    )
    epoch = store.meta("activation_epoch") or now.isoformat()
    window_floor = (now - timedelta(days=SCAN_WINDOW_DAYS)).isoformat()
    candidates = bulk_store.recent_first_observed(max(epoch, window_floor), SCAN_LIMIT)

    reports_processed = 0
    starts = 0
    skip_settlements = 0
    cap_tripped = False
    for (
        rcept_no,
        corp_code,
        corp_name,
        stock_code,
        report_nm,
        rcept_dt,
        _first_observed_at,
    ) in candidates:
        if reports_processed >= MAX_REPORTS_PER_TICK:
            break
        provenance = relevance.provenance_for(stock_code)
        if provenance is None:
            continue
        decided = classify_periodic_report(report_nm, rcept_dt)
        if decided.kind == CLASS_NOT_A_CANDIDATE:
            continue
        existing = store.report(rcept_no)
        if existing is not None and existing["status"] in TERMINAL_STATUSES:
            # §3.2: same report identity already enriched → starts 0 forever.
            continue
        if decided.kind != CLASS_ANNUAL:
            status = (
                STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR
                if decided.kind == CLASS_NONSTANDARD_FISCAL_YEAR
                else STATUS_SKIPPED_YEAR_UNPARSEABLE
            )
            store.settle_report_terminal(
                rcept_no=rcept_no,
                status=status,
                last_outcome=decided.kind,
                now=clock(),
                corp_code=corp_code,
                stock_code=stock_code,
                corp_name=corp_name,
                # "0000" is the explicit no-parseable-year sentinel; skip
                # rows never become request values.
                bsns_year=decided.bsns_year or "0000",
                provenance=provenance,
            )
            skip_settlements += 1
            continue
        if existing is not None:
            if existing["status"] == STATUS_IN_ATTEMPT:
                # Fresh in-flight row (§7 belt-and-suspenders); stale ones
                # were already reconciled above.
                continue
            if existing["attempts"] >= MAX_ATTEMPTS_PER_REPORT:
                continue
            last_attempt_at = existing["last_attempt_at"]
            if last_attempt_at is not None and now < (
                datetime.fromisoformat(last_attempt_at)
                + timedelta(hours=REATTEMPT_SPACING_HOURS)
            ):
                continue
        if clock() >= deadline:
            break
        try:
            used, cap_tripped = _attempt_report(
                store,
                transport,
                api_key,
                rcept_no=rcept_no,
                corp_code=corp_code,
                stock_code=stock_code,
                corp_name=corp_name,
                bsns_year=decided.bsns_year,
                provenance=provenance,
                prior_attempts=existing["attempts"] if existing else 0,
                clock=clock,
            )
        except CapReachedError:
            cap_tripped = True
            break
        starts += used
        reports_processed += 1
        if cap_tripped:
            break

    at = clock()
    if cap_tripped:
        store.append_tick_event(
            at=at,
            outcome="refused_cap_reached",
            reason="cumulative_start_cap",
            reports_processed=reports_processed,
            starts=starts,
        )
        return _summary("refused_cap_reached", reports_processed, starts, scope_note)
    if reports_processed or skip_settlements:
        store.append_tick_event(
            at=at,
            outcome="processed",
            reason=scope_note,
            reports_processed=reports_processed,
            starts=starts,
        )
        return _summary("processed", reports_processed, starts, scope_note)
    return _summary("idle", 0, 0, scope_note)
