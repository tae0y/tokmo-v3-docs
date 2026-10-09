"""SEC holdings-targeted filings collector: pure parsers and queries.

Separate from the one-shot AAPL tracer (sec.py).  This module resolves the
target universe from the canonical/latest COMPLETE holdings snapshot, maps
held US tickers to CIKs via the official ``company_tickers.json``, and
parses ALL aligned recent filings per resolved target with
``filing_date >= MIN_FILING_DATE``.

No ticker literals, no playbook scan, no account coupling.  The target
universe is derived exclusively from the injected CompleteHoldingsSnapshot.
Transport and clock are mandatory injections.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from tokmo.facts.eia import LiveAttemptOutcome
from tokmo.facts.sec import (
    _FAILURE_REASONS,
    CIK_DIGITS,
    SecRequest,
    SecTickerMapping,
    SecTransport,
    _aligned_recent,
    _SecTransportFailure,
    _SecTransportSuccess,
    build_submissions_request,
    build_tickers_request,
)
from tokmo.values import SEC_SOURCE, SecFilingFact, UnavailableFact
from tokmo.values.current_holdings import classify_market
from tokmo.values.holdings import CompleteHoldingsSnapshot
from tokmo.values.session_gate import Market

SOURCE = SEC_SOURCE
from tokmo.values.sec_provenance import (
    COMBINED_PROVENANCE,
    TARGET_PROVENANCE,
    VALID_TARGET_PROVENANCE,
    WATCHLIST_PROVENANCE,
)

MIN_FILING_DATE = date(2026, 8, 3)
MAX_ADMITTED_TARGETS = 20
MAX_REQUESTS_PER_TICK = MAX_ADMITTED_TARGETS + 1  # 1 registry + 20 submissions
MIN_START_INTERVAL_SECONDS = 2.0
STOP_CODES = frozenset({"auth_rejection", "http_429"})

Clock = Callable[[], datetime]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerRegistryResult:
    """All found ticker->CIK mappings plus officially unmapped tickers."""

    mappings: dict[str, SecTickerMapping]
    unmapped: frozenset[str]


@dataclass(frozen=True)
class SecTargetedQueryResult:
    """Closed query outcome: typed result plus optional live attempt."""

    result: TickerRegistryResult | tuple[SecFilingFact, ...] | UnavailableFact
    attempt: LiveAttemptOutcome | None


# ---------------------------------------------------------------------------
# Pure: target universe extraction from holdings snapshot
# ---------------------------------------------------------------------------


def extract_us_holdings_symbols(
    snapshot: CompleteHoldingsSnapshot,
) -> frozenset[str]:
    """Union of all US-market symbols across every account, deduplicated.

    Uses the canonical holdings market/currency contract (classify_market)
    already used by the codebase.  No symbol heuristics or suffixes.
    """
    symbols: set[str] = set()
    for account in snapshot.accounts:
        for holding in account.holdings:
            if classify_market(holding.market_raw, holding.market_field) is Market.US:
                symbols.add(holding.symbol)
    return frozenset(symbols)


def merge_sec_target_provenance(
    snapshot: CompleteHoldingsSnapshot,
    watchlist_symbols: frozenset[str],
) -> dict[str, str]:
    """Deduplicated US SEC target map with explicit intent provenance."""
    holdings = extract_us_holdings_symbols(snapshot)
    merged: dict[str, str] = {}
    for symbol in holdings | watchlist_symbols:
        if symbol in holdings and symbol in watchlist_symbols:
            merged[symbol] = COMBINED_PROVENANCE
        elif symbol in holdings:
            merged[symbol] = TARGET_PROVENANCE
        else:
            merged[symbol] = WATCHLIST_PROVENANCE
    return dict(sorted(merged.items()))


def merge_fallback_target_provenance(
    last_symbols: frozenset[str],
    last_provenance: dict[str, str],
    watchlist_symbols: frozenset[str],
) -> dict[str, str]:
    """Fallback union provenance when no fresh holdings snapshot exists.

    Prior labels are preserved exactly; membership in the *stored* union
    never implies HOLDING (the stored universe may contain watchlist-only
    symbols).  Current watchlist intent only widens a prior HOLDING to
    COMBINED — a prior WATCHLIST symbol still on the watchlist stays
    WATCHLIST, and COMBINED is terminal.
    """
    merged: dict[str, str] = {}
    for symbol in last_symbols | watchlist_symbols:
        if symbol not in last_symbols:
            merged[symbol] = WATCHLIST_PROVENANCE
            continue
        prior = last_provenance.get(symbol, TARGET_PROVENANCE)
        if prior not in VALID_TARGET_PROVENANCE:
            prior = TARGET_PROVENANCE
        if symbol in watchlist_symbols and prior == TARGET_PROVENANCE:
            merged[symbol] = COMBINED_PROVENANCE
        else:
            merged[symbol] = prior
    return dict(sorted(merged.items()))


# ---------------------------------------------------------------------------
# Pure: multi-ticker registry parser
# ---------------------------------------------------------------------------


def parse_company_tickers_multi(
    body: bytes,
    *,
    tickers: frozenset[str],
    observed_at: datetime,
) -> TickerRegistryResult | UnavailableFact:
    """Parse company_tickers.json and extract all requested ticker mappings.

    Every entry in the JSON is validated for structural correctness (same
    contract as the single-ticker parser).  Tickers not present in the
    response are returned in ``unmapped``.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if not isinstance(payload, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    mappings: dict[str, SecTickerMapping] = {}
    for entry in payload.values():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("ticker"), str)
            or not isinstance(entry.get("title"), str)
            or type(entry.get("cik_str")) is not int
        ):
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        ticker = entry["ticker"]
        if ticker not in tickers:
            continue
        cik_number = entry["cik_str"]
        if not 0 < cik_number < 10**CIK_DIGITS or not entry["title"]:
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        mappings[ticker] = SecTickerMapping(
            ticker=ticker,
            cik=f"{cik_number:0{CIK_DIGITS}d}",
            company_name=entry["title"],
        )
    unmapped = frozenset(tickers - set(mappings))
    return TickerRegistryResult(mappings=mappings, unmapped=unmapped)


# ---------------------------------------------------------------------------
# Pure: multi-filing submissions parser (ALL rows >= min_filing_date)
# ---------------------------------------------------------------------------


def parse_submissions_all(
    body: bytes,
    *,
    ticker: str,
    cik: str,
    company_name: str,
    min_filing_date: date,
    observed_at: datetime,
) -> tuple[SecFilingFact, ...] | UnavailableFact:
    """Parse ALL aligned recent filings with filing_date >= min_filing_date.

    Validates every present column for identical length via the shared
    ``_aligned_recent`` helper, then filters rows by date.  Rows before
    the cutoff are skipped; their filing_date is still validated.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)

    recent = _aligned_recent(payload, cik)
    if recent is None:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    accession_numbers = recent["accessionNumber"]
    forms = recent["form"]
    primary_documents = recent["primaryDocument"]
    raw_filing_dates = recent["filingDate"]
    raw_report_dates = recent.get("reportDate")
    raw_is_xbrls = recent.get("isXBRL")

    count = len(accession_numbers)
    filings: list[SecFilingFact] = []
    for i in range(count):
        raw_date = raw_filing_dates[i]
        if not isinstance(raw_date, str) or not raw_date:
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        try:
            filing_date = date.fromisoformat(raw_date)
        except ValueError:
            return UnavailableFact(SOURCE, "parse_error", observed_at)
        if filing_date < min_filing_date:
            continue

        accession = accession_numbers[i]
        form = forms[i]
        primary_document = primary_documents[i]
        for value in (accession, form, primary_document):
            if not isinstance(value, str) or not value:
                return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

        raw_report_date = raw_report_dates[i] if raw_report_dates is not None else None
        raw_is_xbrl = raw_is_xbrls[i] if raw_is_xbrls is not None else None

        if raw_is_xbrl is not None and raw_is_xbrl not in (0, 1):
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        if raw_report_date is not None and not isinstance(raw_report_date, str):
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

        try:
            report_date = (
                date.fromisoformat(raw_report_date) if raw_report_date else None
            )
        except ValueError:
            return UnavailableFact(SOURCE, "parse_error", observed_at)

        filings.append(
            SecFilingFact(
                source=SOURCE,
                ticker=ticker,
                cik=cik,
                company_name=company_name,
                accession_number=accession,
                form=form,
                filing_date=filing_date,
                report_date=report_date,
                primary_document=primary_document,
                is_xbrl=None if raw_is_xbrl is None else bool(raw_is_xbrl),
                observed_at=observed_at,
            )
        )
    return tuple(filings)


# ---------------------------------------------------------------------------
# Query wrappers (transport + parse, one call each)
# ---------------------------------------------------------------------------


def query_company_tickers_multi(
    *,
    tickers: frozenset[str],
    user_agent: str,
    transport: SecTransport,
    clock: Clock,
) -> SecTargetedQueryResult:
    request = build_tickers_request(user_agent=user_agent)
    return _query(
        request,
        transport,
        clock,
        lambda body, observed_at: parse_company_tickers_multi(
            body, tickers=tickers, observed_at=observed_at
        ),
        success_type=TickerRegistryResult,
    )


def query_submissions_all(
    *,
    ticker: str,
    cik: str,
    company_name: str,
    min_filing_date: date,
    user_agent: str,
    transport: SecTransport,
    clock: Clock,
) -> SecTargetedQueryResult:
    request = build_submissions_request(cik=cik, user_agent=user_agent)
    return _query(
        request,
        transport,
        clock,
        lambda body, observed_at: parse_submissions_all(
            body,
            ticker=ticker,
            cik=cik,
            company_name=company_name,
            min_filing_date=min_filing_date,
            observed_at=observed_at,
        ),
        success_type=tuple,
    )


def _query(
    request: SecRequest,
    transport: SecTransport,
    clock: Clock,
    parse: Callable,
    *,
    success_type: type,
) -> SecTargetedQueryResult:
    """One transport call per invocation; escaping exceptions are sanitized."""
    observed_at = clock()
    try:
        outcome = transport(request)
    except Exception:
        outcome = None

    if isinstance(outcome, _SecTransportSuccess):
        parsed = parse(outcome.body, observed_at)
        del outcome
        if isinstance(parsed, success_type):
            return SecTargetedQueryResult(parsed, LiveAttemptOutcome(code="success"))
        return SecTargetedQueryResult(
            parsed, LiveAttemptOutcome(code="response_contract_failure")
        )

    if isinstance(outcome, _SecTransportFailure):
        reason = _FAILURE_REASONS[outcome.code]
        fact = UnavailableFact(SOURCE, reason, observed_at, outcome.retry_after)
        return SecTargetedQueryResult(
            fact,
            LiveAttemptOutcome(code=outcome.code, retry_after=outcome.retry_after),
        )

    return SecTargetedQueryResult(
        UnavailableFact(SOURCE, "transport_error", observed_at),
        LiveAttemptOutcome(code="protocol_failure"),
    )
