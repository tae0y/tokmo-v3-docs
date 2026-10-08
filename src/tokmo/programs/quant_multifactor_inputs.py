"""Deterministic offline construction of frozen Quant multifactor inputs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from re import fullmatch

from tokmo.programs.quant_multifactor_core import as_of_market_date, canonical_bytes
from tokmo.programs.quant_membership import (
    describe_us_personal_membership_cohort,
    proof_is_admissible,
)
from tokmo.values.current_holdings import canonical_holdings_bytes
from tokmo.values.fundamental_materials import MetricSelection
from tokmo.values.quant_multifactor_core import AccountingRegimeV1, AdjustmentV1, CohortInputV1, DailyCloseV1, FiscalPeriodIdentityV1, FundamentalSnapshotV1, MarketV1, MemberObservationV1, MonthlyCloseV1, PeriodTypeV1, SessionDateRuleV1
from tokmo.values.quant_multifactor_provenance import ASSEMBLY_PROVENANCE_VERSION, US_MARKET_VOCABULARY_DERIVATION, US_STORED_MARKET_VOCABULARY, AssemblyInputV1, AssemblyOutcomeV1, CandleRequestIdentityV1, MemberReasonV1, MemberSelection, RawCandlePageV1


_METRICS = ("revenue_ttm", "net_income_ttm", "gross_profit_ttm", "total_equity", "total_assets", "diluted_shares_outstanding")
_MONETARY = _METRICS[:5]


def requires_page2(page1: RawCandlePageV1) -> bool:
    return page1.raw_row_count == 200


def page2_request_identity(page1: RawCandlePageV1) -> CandleRequestIdentityV1:
    aware = tuple(row.timestamp for row in page1.rows if row.timestamp.tzinfo is not None and row.timestamp.utcoffset() is not None)
    if not requires_page2(page1) or not aware:
        raise ValueError("page2_not_admissible")
    return CandleRequestIdentityV1("qt", page1.request.symbol, page1.request.market, "1d", 200, False, 2, min(aware) - timedelta(seconds=1))


def _snapshot(material, currency):
    values = tuple(getattr(material, name) for name in _METRICS)
    return FundamentalSnapshotV1(material.source_ref, FiscalPeriodIdentityV1(PeriodTypeV1.ANNUAL, material.fiscal_year), material.period_end, material.available_at, material.revision_of, currency, *(item.value if item is not None and item.selection is MetricSelection.EXACT else None for item in values))


def _currency(material):
    units = {getattr(material, name).unit for name in _MONETARY if getattr(material, name) is not None}
    if len(units) != 1:
        return None
    value = next(iter(units))
    if fullmatch(r"[A-Z]{3}", value) is None:
        return None
    return value


def _decimal_valid(value, policy):
    if value is None:
        return True
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    plain = format(value, "f")
    digits = plain.lstrip("-").replace(".", "").lstrip("0") or "0"
    fractional = len(plain.partition(".")[2])
    if len(digits) > 30 or fractional > 12 or len(plain.encode("ascii")) > 64:
        return False
    if policy == "nonnegative" and value < 0:
        return False
    if policy == "positive" and value <= 0:
        return False
    return True


def _material_valid(material, market_date, as_of):
    if material.available_at.tzinfo is None or material.available_at.utcoffset() is None or not 2007 <= material.available_at.year <= 9999 or material.available_at > as_of:
        return False
    if not 2007 <= material.fiscal_year <= 9999 or not 2007 <= material.period_end.year <= 9999 or material.period_end > market_date:
        return False
    if fullmatch(r"[A-Za-z0-9._:/-]{1,128}", material.source_ref) is None:
        return False
    if material.revision_of is not None and fullmatch(r"[A-Za-z0-9._:/-]{1,128}", material.revision_of) is None:
        return False
    policies = ("nonnegative", "signed", "signed", "signed", "positive", "positive")
    for name, policy in zip(_METRICS, policies, strict=True):
        evidence = getattr(material, name)
        if evidence is not None and not _decimal_valid(evidence.value, policy):
            return False
    return True


def _holdings_identity(holdings):
    accounts = []
    for account in holdings.accounts:
        item_digests = tuple(sorted(sha256("|".join((item.symbol, item.quantity, item.avg_price, item.currency or "", item.market_raw or "", item.market_field or "")).encode("utf-8")).hexdigest() for item in account.holdings))
        accounts.append((account.account_scope, account.ordinal, item_digests))
    return {"schema_version": holdings.schema_version, "global_inventory": holdings.global_inventory, "captured_at": holdings.captured_at, "accounts": tuple(sorted(accounts))}


def _provenance_holdings_digest(holdings):
    return sha256(canonical_bytes(_holdings_identity(holdings))).hexdigest()


def _valid_as_of_utc(value):
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return False
        utc = value.astimezone(timezone.utc)
        return 2007 <= utc.year <= 9999
    except (OverflowError, ValueError, TypeError):
        return False


def _safe_as_of_text(value):
    try:
        if isinstance(value, datetime):
            return value.isoformat()
    except (OverflowError, ValueError, TypeError):
        pass
    return f"invalid:{type(value).__name__}"


def _normalize(member, as_of):
    if requires_page2(member.page1):
        expected_page2 = page2_request_identity(member.page1)
        if member.page2 is None or member.page2.request != expected_page2:
            raise ValueError("page2_identity")
    elif member.page2 is not None:
        raise ValueError("unexpected_page2")
    pages = (member.page1,) if member.page2 is None else (member.page1, member.page2)
    if member.page2 is not None and max(row.timestamp for row in member.page2.rows) >= min(row.timestamp for row in member.page1.rows):
        raise ValueError("page_order")
    rows = tuple(row for page in pages for row in page.rows)
    if any(row.timestamp.tzinfo is None or row.timestamp.utcoffset() is None or not _decimal_valid(row.close, "positive") or row.timestamp > as_of for row in rows):
        raise ValueError("invalid_candle")
    instants = tuple(row.timestamp.astimezone(timezone.utc) for row in rows)
    if len(set(instants)) != len(instants):
        raise ValueError("invalid_candle")
    currencies = {row.currency for row in rows}
    if len(currencies) != 1:
        raise ValueError("invalid_candle")
    page1_rows = tuple(sorted(member.page1.rows, key=lambda row: row.timestamp))
    daily = tuple(DailyCloseV1(row.timestamp.date(), row.close, row.currency) for row in page1_rows[-252:])
    if len({item.date for item in daily}) != len(daily):
        raise ValueError("invalid_candle")
    if any(item.date.weekday() > 4 for item in daily):
        raise ValueError("weekday")
    by_month = {}
    for row in sorted(rows, key=lambda item: item.timestamp):
        key = row.timestamp.strftime("%Y-%m")
        by_month[key] = row
    monthly_rows = tuple(by_month[key] for key in sorted(by_month)[-13:])
    monthly = tuple(MonthlyCloseV1(row.timestamp.strftime("%Y-%m"), row.timestamp.date(), row.close, row.currency) for row in monthly_rows)
    if any(item.session_date.weekday() > 4 or item.month_id != item.session_date.strftime("%Y-%m") for item in monthly):
        raise ValueError("monthly")
    return daily, monthly, next(iter(currencies))


def assemble_quant_multifactor_input(input_value: AssemblyInputV1) -> AssemblyOutcomeV1:
    reasons = []
    observations = []
    holdings_digest = _provenance_holdings_digest(input_value.holdings)
    membership_holdings_digest = sha256(
        canonical_holdings_bytes(input_value.holdings)
    ).hexdigest()
    try:
        binding = describe_us_personal_membership_cohort(
            input_value.holdings,
            input_value.as_of,
        )
        expected_symbols = set(binding.expected_symbols)
    except ValueError:
        binding = None
        expected_symbols = set()
    ordered = tuple(sorted(input_value.members, key=lambda item: (item.proof is None, "" if item.proof is None else item.proof.symbol, item.page1.request.canonical_ref, tuple(sorted(material.source_ref for material in item.materials)))))
    if not _valid_as_of_utc(input_value.as_of):
        reasons.append(MemberReasonV1(MemberSelection.COHORT_UNAVAILABLE, "as_of_identity"))
        member_sources = tuple(sorted(("" if member.proof is None else member.proof.source_ref, "" if member.proof is None else member.proof.membership_as_of.isoformat(), member.page1.request.canonical_ref, member.page1.raw_row_count, "" if member.page2 is None else member.page2.request.canonical_ref, 0 if member.page2 is None else member.page2.raw_row_count, tuple(sorted((material.source_ref, material.available_at.isoformat(), material.observed_at.isoformat()) for material in member.materials))) for member in ordered))
        reason_payload = tuple((reason.selection.value, reason.detail, reason.metric_selections) for reason in reasons)
        digest_payload = {"grammar": ASSEMBLY_PROVENANCE_VERSION, "market_vocabulary": US_STORED_MARKET_VOCABULARY, "market_vocabulary_derivation": US_MARKET_VOCABULARY_DERIVATION, "as_of": _safe_as_of_text(input_value.as_of), "universe_id": input_value.universe_id, "universe_policy_version": input_value.universe_policy_version, "adjustment_version": input_value.adjustment_version, "holdings_digest": holdings_digest, "membership_holdings_digest": membership_holdings_digest, "member_sources": member_sources, "counts": (len(input_value.members), 0, len(expected_symbols)), "reasons": reason_payload, "success": False}
        digest = sha256(canonical_bytes(digest_payload)).hexdigest()
        return AssemblyOutcomeV1(None, tuple(reasons), digest, 0, US_STORED_MARKET_VOCABULARY, ASSEMBLY_PROVENANCE_VERSION, US_MARKET_VOCABULARY_DERIVATION)
    if binding is None or input_value.holdings.captured_at != input_value.as_of:
        reasons.append(MemberReasonV1(MemberSelection.MEMBER_UNAVAILABLE, "holdings_identity"))
    elif not expected_symbols:
        reasons.append(MemberReasonV1(MemberSelection.COHORT_UNAVAILABLE, "empty_cohort"))
    supplied_symbols = tuple(member.proof.symbol for member in ordered if member.proof is not None)
    if len(supplied_symbols) != len(set(supplied_symbols)):
        reasons.append(MemberReasonV1(MemberSelection.COHORT_UNAVAILABLE, "duplicate_member_symbol"))
    if set(supplied_symbols) != expected_symbols or len(supplied_symbols) != len(expected_symbols) or len(ordered) != len(expected_symbols):
        reasons.append(MemberReasonV1(MemberSelection.COHORT_UNAVAILABLE, "cohort_member_count"))
    market_date = as_of_market_date(MarketV1.US_NYSE_NASDAQ, input_value.as_of)
    observation_currencies = set()
    for member in ordered:
        proof = member.proof
        if (
            proof is None
            or binding is None
            or proof.symbol not in expected_symbols
            or not proof_is_admissible(
                proof,
                binding=binding,
                evaluation_as_of=input_value.as_of,
            )
        ):
            reasons.append(MemberReasonV1(MemberSelection.MEMBER_UNAVAILABLE, "membership_proof"))
            continue
        materials = tuple(sorted((item for item in member.materials if item.available_at.astimezone(timezone.utc) <= input_value.as_of.astimezone(timezone.utc)), key=lambda item: (-item.period_end.toordinal(), -item.available_at.astimezone(timezone.utc).timestamp(), item.source_ref)))
        if not materials:
            reasons.append(MemberReasonV1(MemberSelection.MEMBER_UNAVAILABLE, "fundamentals"))
            continue
        current = materials[0]
        prior = next((item for item in materials[1:] if 363 <= (current.period_end - item.period_end).days <= 372), None)
        if not _material_valid(current, market_date, input_value.as_of) or (prior is not None and not _material_valid(prior, market_date, input_value.as_of)):
            reasons.append(MemberReasonV1(MemberSelection.INVALID_FUNDAMENTALS, "fundamental_invariant"))
            continue
        selections = tuple((name, (getattr(current, name).selection.value if getattr(current, name) is not None else MetricSelection.UNAVAILABLE.value)) for name in _METRICS)
        for _, selection in selections:
            if selection == MetricSelection.AMBIGUOUS.value:
                reasons.append(MemberReasonV1(MemberSelection.AMBIGUOUS_METRIC_PRESERVED, "metric_selection", selections))
            elif selection == MetricSelection.UNAVAILABLE.value:
                reasons.append(MemberReasonV1(MemberSelection.UNAVAILABLE_METRIC_PRESERVED, "metric_selection", selections))
        currency = _currency(current)
        prior_currency = None if prior is None else _currency(prior)
        if currency is None or (prior is not None and prior_currency != currency):
            reasons.append(MemberReasonV1(MemberSelection.INVALID_CURRENCY, "currency"))
            continue
        try:
            daily, monthly, candle_currency = _normalize(member, input_value.as_of)
        except ValueError:
            reasons.append(MemberReasonV1(MemberSelection.INVALID_CANDLES, "candles"))
            continue
        if candle_currency != currency:
            reasons.append(MemberReasonV1(MemberSelection.INVALID_CURRENCY, "currency"))
            continue
        observation_currencies.add(currency)
        if prior is not None and prior.fiscal_year != current.fiscal_year - 1:
            reasons.append(MemberReasonV1(MemberSelection.NONCONSECUTIVE_PRIOR_PRESERVED, "fiscal_year"))
        observations.append(MemberObservationV1(proof.symbol, proof.sector, proof.source_ref, True, _snapshot(current, currency), None if prior is None else _snapshot(prior, currency), monthly, daily))
    if len(observations) != len(expected_symbols) and not any(reason.detail == "cohort_member_count" for reason in reasons):
        reasons.append(MemberReasonV1(MemberSelection.COHORT_UNAVAILABLE, "admissible_member_count"))
    if len(observation_currencies) > 1:
        reasons.append(MemberReasonV1(MemberSelection.INVALID_CURRENCY, "cohort_currency"))
    cohort = None
    fatal = (MemberSelection.MEMBER_UNAVAILABLE, MemberSelection.INVALID_CURRENCY, MemberSelection.INVALID_CANDLES, MemberSelection.INVALID_FUNDAMENTALS, MemberSelection.COHORT_UNAVAILABLE)
    if not any(reason.selection in fatal for reason in reasons) and len(observations) == len(expected_symbols):
        price_refs = tuple(member.page1.request.canonical_ref for member in ordered)
        price_ref = "qcpackv1:sha256:" + sha256("|".join(price_refs).encode("ascii")).hexdigest()
        cohort_currency = next(iter(observation_currencies))
        cohort = CohortInputV1(MarketV1.US_NYSE_NASDAQ, cohort_currency, AccountingRegimeV1.US_GAAP, input_value.as_of, tuple(observations), price_ref, AdjustmentV1.RAW, input_value.adjustment_version, input_value.as_of, SessionDateRuleV1.US_NYSE_NASDAQ_SESSION, input_value.universe_id, input_value.universe_policy_version, market_date)
    member_sources = tuple(sorted(("" if member.proof is None else member.proof.source_ref, "" if member.proof is None else member.proof.membership_as_of.isoformat(), "" if member.proof is None else member.proof.membership_holdings_digest, () if member.proof is None else tuple((instant.source_kind.value, instant.observed_at.isoformat()) for instant in member.proof.observation_instants), member.page1.request.canonical_ref, member.page1.raw_row_count, "" if member.page2 is None else member.page2.request.canonical_ref, 0 if member.page2 is None else member.page2.raw_row_count, tuple(sorted((material.source_ref, material.available_at.isoformat(), material.observed_at.isoformat()) for material in member.materials))) for member in ordered))
    reason_payload = tuple(sorted((reason.selection.value, reason.detail, reason.metric_selections) for reason in reasons))
    digest_payload = {"grammar": ASSEMBLY_PROVENANCE_VERSION, "market_vocabulary": US_STORED_MARKET_VOCABULARY, "market_vocabulary_derivation": US_MARKET_VOCABULARY_DERIVATION, "as_of": input_value.as_of, "universe_id": input_value.universe_id, "universe_policy_version": input_value.universe_policy_version, "adjustment_version": input_value.adjustment_version, "holdings_digest": holdings_digest, "membership_holdings_digest": membership_holdings_digest, "member_sources": member_sources, "counts": (len(input_value.members), len(observations), len(expected_symbols)), "reasons": reason_payload, "success": cohort is not None}
    digest = sha256(canonical_bytes(digest_payload)).hexdigest()
    return AssemblyOutcomeV1(cohort, tuple(reasons), digest, len(observations), US_STORED_MARKET_VOCABULARY, ASSEMBLY_PROVENANCE_VERSION, US_MARKET_VOCABULARY_DERIVATION)
