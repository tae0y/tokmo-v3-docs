"""Read-only local context collectors for the Quant contextual decision card.

Product contract 2026-09-03 §4/§7 (SDS v0.1): the evidence pack's optional
``context_items`` are supplied by the caller; this module is the production
binding — a bounded projection of (a) the layered instrument playbook's most
recent Layer-3 trade rationale, (b) local NEWS documents, and (c) OpenDART
disclosures into ``ContextItemV1`` tuples.

Everything here is read-only and degrades to empty on any missing or
unreadable store — never raises to the caller. It writes nothing (no
playbook, Ledger, order, account, Telegram, or runtime-store writes), calls
no provider, imports no transport, and adds no schema, store or daemon: it
only reads existing canonical shapes through their own seams
(``instrument_document_path``, ``read_discovery_documents_v1``,
``DisclosureStore.open_readonly``) and the §O/§P instrument-matching
values layer shared with news admission.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from tokmo.programs.internal_evidence_query import (
    EvidenceQueryV1,
    expand_query_terms,
    run_internal_evidence_query,
)
from tokmo.programs.news_topic_discovery import (
    _contains_exact_sequence,
    _normalise,
    _tokens,
)
from tokmo.programs.quant_contextual_analysis import (
    MAX_SUMMARY_CHARS,
    NEWS_MAX_AGE_DAYS,
    ContextItemV1,
)
from tokmo.records.news_decision_store import timestamp
from tokmo.records.playbook_identity import scope_dir_document_path
from tokmo.values.news_instrument_match import (
    ETF_BRAND_TRANSLITERATIONS_V1,
    is_etf_name,
    match_etf_reference,
)

MAX_CONTEXT_ITEMS = 5
LAYER3_HEADING = "## 3층 — 매매이력"
HERMES_START, HERMES_END = (
    "<!-- HERMES_CURRENT_START -->",
    "<!-- HERMES_CURRENT_END -->",
)
_ANCHOR_RE = re.compile(r'<a id="entry-([^"]+)"></a>')
_SUMMARY_RE = re.compile(r"- `trade_summary`: `(BUY|SELL) ")
_EVENT_DATE_RE = re.compile(r"- `event_date`: `(\d{4}-\d{2}-\d{2})`")
_RAW_BLOCK_RE = re.compile(
    r'<!-- USER_RAW_START entry_id="(?P<id>[^"]+)" -->\n(?P<body>.*?)\n'
    r'<!-- USER_RAW_END entry_id="\1" -->',
    re.DOTALL,
)
_MARKUP_RE = re.compile(r"<!--.*?-->|\[[^\]]*\]\([^)]*\)|`")
# private numeric scrub: amounts/prices/quantities never reach rendered analysis
_NUMBERS_RE = re.compile(r"\d+(?:[,.]\d+)*")
# provenance ref cap: identifiers riding ``source_label`` stay bounded
_MAX_REF_CHARS = 48
_SIDE_KR = {"BUY": "매수", "SELL": "매도"}


def _bounded(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Whitespace-collapsed, markup-stripped, byte-safe clip."""
    cleaned = " ".join(_MARKUP_RE.sub("", str(text)).split())
    encoded = cleaned.encode("utf-8")[:limit]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def _with_ref(label: str, ref: str | None) -> str:
    """Bounded provenance suffix on ``source_label`` (evidence metadata
    only; renderers show item summaries and validated analysis statements,
    not context ``source_label`` values)."""
    if not ref:
        return label
    ref = " ".join(str(ref).split())[:_MAX_REF_CHARS]
    return f"{label} [{ref}]"


# --- playbook Layer-3 projection -------------------------------------------------


def collect_playbook_context_items(
    playbook_root, *, market: str, symbol: str, scope_dirs=("personal", "retirement")
) -> tuple:
    """(trade_reason?, playbook_current?) for the canonical instrument doc.

    Reads only; the Layer-3 entry newest-first directly below the ``## 3층``
    heading supplies the trade rationale verbatim-but-bounded (numbers
    scrubbed: no private amounts/prices leak into rendered analysis), with
    provenance kept through ``event_date``; the Layer-1 current judgment
    supplies a structure summary (same numeric scrub applied — dates,
    prices, quantities, amounts never reach context summaries). Any
    unreadable/absent/malformed document degrades to an empty tuple.
    Scope resolution uses the pure scope-kind path API — no fabricated
    account identities.
    """
    items: list = []
    for scope_dir in scope_dirs:
        path = scope_dir_document_path(
            playbook_root, scope_dir=scope_dir, market=market, symbol=symbol
        )
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return ()
        break
    else:
        return ()
    if text.count(LAYER3_HEADING) == 1:
        layer3 = text.split(LAYER3_HEADING, 1)[1]
        # canonical order: the newest entry sits directly below the 3층
        # heading; the entry block ends at the next anchor (if any)
        anchors = _ANCHOR_RE.findall(layer3)
        if anchors:
            entry_id = anchors[0]
            first = _ANCHOR_RE.search(layer3)
            block_end = _ANCHOR_RE.search(layer3, first.end()) if first else None
            entry_block = layer3 if block_end is None else layer3[: block_end.start()]
            raw = _RAW_BLOCK_RE.search(entry_block)
            side = _SUMMARY_RE.search(entry_block)
            event_date = _EVENT_DATE_RE.search(entry_block)
            when = event_date.group(1) if event_date else None
            side_kr = _SIDE_KR.get(side.group(1), "거래") if side else "거래"
            reason = raw.group("body") if raw and raw.group("id") == entry_id else ""
            reason = _NUMBERS_RE.sub(" ", reason)
            summary = _bounded(
                f"{side_kr} 사유: {reason}"
                if reason
                else f"최근 {side_kr} 거래 사유 원문 미기록"
            )
            if summary:
                items.append(
                    ContextItemV1(
                        "trade_reason",
                        summary,
                        observed_on=when,
                        source_label="플레이북 3층",
                    )
                )
    if text.count(HERMES_START) == 1 and text.count(HERMES_END) == 1:
        layer1 = text.split(HERMES_START, 1)[1].split(HERMES_END, 1)[0]
        lines = [
            line.strip()
            for line in layer1.splitlines()
            if line.strip() and not line.strip().startswith((">", "#", "-"))
        ]
        if lines:
            summary = _bounded(_NUMBERS_RE.sub(" ", lines[0]))
            if summary:
                items.append(
                    ContextItemV1(
                        "playbook_current", summary, source_label="플레이북 1층"
                    )
                )
    return tuple(items)


# --- local NEWS binding ------------------------------------------------------------

# Explicit Stage-1 ``news_raw``/``opendart`` store-health → Quant status
# vocabulary (M2): OK passes through; every open/absent/not-configured
# degradation is STORE_UNAVAILABLE; schema, lock and corruption keep their
# distinct meanings; the RESULT_CAP_TRUNCATED limitation surfaces as
# CAP_EXCEEDED. Unknown codes fail closed to STORE_UNAVAILABLE.
_STAGE1_HEALTH_TO_STATUS = {
    "OK": "OK",
    "STORE_ABSENT": "STORE_UNAVAILABLE",
    "STORE_NOT_CONFIGURED": "STORE_UNAVAILABLE",
    "STORE_OPEN_FAILED": "STORE_UNAVAILABLE",
    "SCHEMA_MISMATCH": "SCHEMA_MISMATCH",
    "STORE_BUSY_OR_LOCKED": "BUSY_OR_LOCKED",
    "STORE_MALFORMED": "CORRUPT",
}


def _health_status(family):
    """Stage-1 store health → Quant status, or ``None`` when the family
    read OK (a cap truncation is reported separately, never as a store
    failure). Unknown codes fail closed to STORE_UNAVAILABLE."""
    health = family["store_health"]
    if health == "OK":
        return None
    return _STAGE1_HEALTH_TO_STATUS.get(health, "STORE_UNAVAILABLE")


def collect_local_news_items(
    news_path,
    *,
    market: str,
    symbol: str,
    official_names,
    universe,
    completed_session_date,
    max_items: int = MAX_CONTEXT_ITEMS,
    status_out: dict | None = None,
) -> tuple:
    """Exact-instrument local NEWS titles from the n5 read-only seam.

    Company names bind through the same whole-sequence §O scan the news
    discovery lane uses — delegated (Stage 2) to
    ``run_internal_evidence_query``, which scans the WHOLE session-anchored
    date window (never a latest-N truncation) and expands caller-supplied
    aliases through the canonical public alias catalog (``하닉`` surfaces
    ``SK하이닉스``; no alias is ever invented); this function keeps only
    the per-card exact-instrument acceptance projection. ETF names bind
    through the §P brand-anchored coverage match, admissible only when the
    target symbol is the unique satisfying candidate in the supplied
    universe. Each item keeps a bounded non-secret canonical source
    reference (``news_document_id``) on ``source_label`` so a reviewer can
    trace the evidence back to the NEWS store; the reference is evidence
    metadata, never user-facing rendered analysis. Missing/unreadable
    store → empty, and the store's availability status is preserved in
    ``status_out``
    (``OK`` | ``STORE_UNAVAILABLE`` | ``CAP_EXCEEDED`` | ``NOT_REQUESTED``)
    so unavailable stays visibly distinct from a valid zero-result.
    """
    try:
        session = (
            completed_session_date
            if isinstance(completed_session_date, date)
            else date.fromisoformat(str(completed_session_date))
        )
    except ValueError:
        if status_out is not None:
            status_out["news"] = "NOT_REQUESTED"
        return ()
    names = tuple(str(name) for name in official_names or ())
    if not names or max_items <= 0:
        if status_out is not None:
            status_out["news"] = "NOT_REQUESTED"
        return ()
    if is_etf_name(names[0]):
        # Every §P-matching headline anchors on the ETF brand token (or
        # its Korean transliteration) — fetch on exactly those anchors.
        brand = _tokens(_normalise(names[0]))[0]
        fetch_terms = (brand,) + tuple(
            sorted(
                korean
                for korean, latin in ETF_BRAND_TRANSLITERATIONS_V1.items()
                if latin == brand
            )
        )
    else:
        fetch_terms = names
    etf_universe = [
        (key, str(name))
        for key, member_names in dict(universe or {}).items()
        for name in member_names
        if is_etf_name(str(name))
    ]
    # Same-entity acceptance names: caller names plus the public alias
    # catalog's whole alias set for any name that grounds exactly one
    # entity (``하닉`` admits verbatim ``SK하이닉스`` titles).
    accepted_names = tuple(
        dict.fromkeys(
            (*names, *expand_query_terms(terms=fetch_terms)["expanded_terms"])
        )
    )

    def _accept_row(text: str) -> bool:
        # Exact-instrument acceptance, applied INSIDE Stage-1 before the
        # result limit (M1), whole-sequence only — never a partial or
        # head match. ETF names bind through the §P brand-anchored
        # coverage match, admissible only when the target symbol is the
        # unique satisfying candidate in the supplied universe.
        tokens = _tokens(_normalise(text))
        if any(
            not is_etf_name(name) and _contains_exact_sequence(tokens, _normalise(name))
            for name in accepted_names
        ):
            return True
        if not is_etf_name(names[0]):
            return False
        hits = {
            key for key, name in etf_universe if match_etf_reference(text, name).matched
        }
        return len(hits) == 1 and symbol in hits

    query = EvidenceQueryV1(
        date_from=session - timedelta(days=NEWS_MAX_AGE_DAYS),
        date_to=session,
        terms=fetch_terms,
        families=("news_raw",),
        limit=max_items,
    )
    result = run_internal_evidence_query(
        query, news_db=news_path, news_row_acceptor=_accept_row
    )
    family = result["families"]["news_raw"]
    health_status = _health_status(family)
    if health_status is not None:
        if status_out is not None:
            status_out["news"] = health_status
        return ()
    if status_out is not None:
        status_out["news"] = (
            "CAP_EXCEEDED" if family.get("counts", {}).get("truncated") else "OK"
        )
    # The Stage-1 row acceptor already enforced the per-card exact-
    # instrument acceptance (whole window, before the result limit);
    # expansion terms stay for provenance/debugging only.
    items: list = []
    for document in family["documents"]:
        title = " ".join(str(document["title"]).split())
        observed = (
            str(document["published_at"] or document["first_seen_at"] or "")[:10]
            or None
        )
        available = document.get("first_seen_at")
        if (document.get("revision_no") or 1) > 1:
            # Only the latest revision body exists here. Do not place that
            # later text at the first version's original collection time.
            try:
                available = max(
                    (v for v in (available, document.get("last_seen_at")) if v),
                    key=timestamp,
                )
            except (ValueError, TypeError):
                available = None
        items.append(
            ContextItemV1(
                "news",
                _bounded(title),
                observed_on=observed,
                available_at=available,
                published_at=document.get("published_at"),
                source_label=_with_ref("로컬 NEWS", document["document_id"]),
            )
        )
        if len(items) >= max_items:
            break
    return tuple(items)


# --- OpenDART disclosure binding ------------------------------------------------------


def collect_dart_disclosure_items(
    dart_path,
    *,
    market: str,
    symbol: str,
    completed_session_date,
    max_items: int = MAX_CONTEXT_ITEMS,
    status_out: dict | None = None,
) -> tuple:
    """Recent disclosures for one KR stock_code from the bulk evidence store.

    Read-only and delegated (Stage 2) to ``run_internal_evidence_query``,
    which owns the stock-code + receipt-date window filtering (whole
    window, deterministic order, bounded batches); this function keeps
    only the ``ContextItemV1`` projection. The receipt window matches the
    pack's NEWS_MAX_AGE_DAYS freshness contract, and each item keeps the
    OpenDART ``rcept_no`` (bounded) on ``source_label`` as a non-secret
    canonical source reference a reviewer can trace back to DART —
    evidence metadata only, never user-facing rendered analysis. Missing
    or unsafe store → empty, never raises, and the availability status is
    preserved in ``status_out`` (``OK`` | ``STORE_UNAVAILABLE`` |
    ``NOT_REQUESTED``) so unavailable stays visibly distinct from a valid
    zero-result.
    """
    if market != "KR" or max_items <= 0:
        if status_out is not None:
            status_out["dart"] = "NOT_REQUESTED"
        return ()
    try:
        session = (
            completed_session_date
            if isinstance(completed_session_date, date)
            else date.fromisoformat(str(completed_session_date))
        )
    except ValueError:
        if status_out is not None:
            status_out["dart"] = "NOT_REQUESTED"
        return ()
    query = EvidenceQueryV1(
        date_from=session - timedelta(days=NEWS_MAX_AGE_DAYS),
        date_to=session,
        symbols=(symbol,),
        families=("opendart",),
        limit=max_items,
    )
    result = run_internal_evidence_query(query, opendart_db=dart_path)
    family = result["families"]["opendart"]
    health_status = _health_status(family)
    if health_status is not None:
        if status_out is not None:
            status_out["dart"] = health_status
        return ()
    if status_out is not None:
        status_out["dart"] = (
            "CAP_EXCEEDED" if family.get("counts", {}).get("truncated") else "OK"
        )
    items = []
    for row in family["disclosures"]:
        summary = _bounded(f"{row['corp_name']} {row['report_nm']} 접수")
        if summary:
            items.append(
                ContextItemV1(
                    "disclosure",
                    summary,
                    observed_on=row["rcept_dt"],
                    published_at=row.get("rcept_dt"),
                    source_label=_with_ref("OpenDART 공시", row["rcept_no"]),
                )
            )
    return tuple(items)
