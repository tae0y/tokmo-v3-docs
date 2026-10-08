"""Reviewed classification contract for the broker account-status workbook.

There is exactly ONE canonical pension holdings authority in Tokmo: the
existing ``pension_registry.sqlite3`` / ``PensionRegistryStore`` /
``PensionHoldingsRevisionV1`` / ``HoldingItemV1`` path with its single ACTIVE
pointer. This module does NOT introduce a second one. It only supplies the
typed, bounded, operator-reviewed contract that lets a new broker workbook
shape be normalized into that existing revision type.

Scope decision (user-approved): the canonical pension holdings revision is
**ETF-only**. The broker account-status export also lists non-exchange assets
(corporate bonds, a mutual fund, a cash-like balance). Those rows are
deliberately EXCLUDED from the holdings revision rather than modelled, because
``HoldingItemV1`` cannot represent them without inventing an exchange symbol.
The user accepts that incompleteness. Their existence is preserved only as
sanitized private reconciliation evidence (counts + digest + disposition), and
no alternate canonical holdings source is created for them.

Classification is never inferred. A row becomes a holding only when the
reviewed plan explicitly names it a verified KRX ETF and supplies its short
code. A row that is absent from the plan, or whose declared counts do not
reconcile, fails closed — a bond must never be admitted because its code or
name happens to look tradable.

Privacy: ``instrument_repr`` is the private source product name. It is
``repr=False``, is excluded from the semantic digest, and appears only inside
the private reconciliation digest, which is never projected outward.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from tokmo.values import canonical_decimal
from tokmo.values.material_pack import canonical_encode
from tokmo.values.pension_holdings import PensionHoldingsRevisionV1

# The broker account-status workbook is a different INPUT shape from the
# original pension holdings sheet, recorded in the revision's
# ``sheet_schema_version``. The normalized OUTPUT contract — and therefore
# ``parser_version`` — is unchanged, which is what keeps the single canonical
# read path and the composite payload validator intact.
PENSION_ACCOUNT_STATUS_SHEET_SCHEMA_VERSION = 2

CLASSIFICATION_PLAN_VERSION = "pension-account-status-plan-v1"

DISPOSITION_VERIFIED_KRX_ETF = "VERIFIED_KRX_ETF"
DISPOSITION_EXCLUDED_NON_ETF = "EXCLUDED_NON_ETF"
DISPOSITIONS = frozenset({
    DISPOSITION_VERIFIED_KRX_ETF, DISPOSITION_EXCLUDED_NON_ETF,
})

EXCLUSION_CORPORATE_BOND = "CORPORATE_BOND"
EXCLUSION_MUTUAL_FUND = "MUTUAL_FUND"
EXCLUSION_CASH_LIKE = "CASH_LIKE"
EXCLUSION_KINDS = frozenset({
    EXCLUSION_CORPORATE_BOND, EXCLUSION_MUTUAL_FUND, EXCLUSION_CASH_LIKE,
})

# Same shape the existing pension parser already accepts for KR symbols.
KRX_SHORT_CODE_RE = re.compile(r"^0[0-9A-Z]{5}$|^[0-9]{6}$")

# Truthful coverage statements. Neither ever claims a full-account inventory.
COVERAGE_ETF_ONLY_PARTIAL = "ETF_ONLY_PARTIAL_ACCOUNT_COVERAGE"
COVERAGE_ETF_ONLY_WHOLE = "ETF_ONLY_WHOLE_ACCOUNT_COVERAGE"
COVERAGE_STATEMENTS = frozenset({
    COVERAGE_ETF_ONLY_PARTIAL, COVERAGE_ETF_ONLY_WHOLE,
})

AVERAGE_COST_DERIVED = "DERIVED_PURCHASE_AMOUNT_OVER_QUANTITY"
DERIVED_AVERAGE_COST_SCALE = 10
_DERIVED_AVERAGE_COST_EXPONENT = Decimal(1).scaleb(-DERIVED_AVERAGE_COST_SCALE)

MAX_INSTRUMENT_REPR_LENGTH = 256
MAX_ACCOUNT_STATUS_ROWS = 256

# The period summary is aggregate evidence, not execution truth. None of these
# field names may appear on a period-summary type: their presence would be the
# first step toward appending fabricated executions to the retirement ledger.
FORBIDDEN_EXECUTION_FIELDS = frozenset({
    "trade_date",
    "settlement_date",
    "order_id",
    "source_record_key",
    "supersedes_source_record_key",
    "event_kind",
    "execution_id",
    "transaction_type_raw",
})


def execution_field_offenders(dataclass_type) -> tuple[str, ...]:
    """Field names on *dataclass_type* that would imply execution truth."""
    return tuple(sorted(
        item.name for item in fields(dataclass_type)
        if item.name in FORBIDDEN_EXECUTION_FIELDS
    ))


class PensionClassificationError(ValueError):
    """Typed rejection of a classification plan. Carries no private name."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Decimal helpers — exact arithmetic, no binary float anywhere
# ---------------------------------------------------------------------------


def _decimal_or_none(value: str) -> Decimal | None:
    if type(value) is not str or not value:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _require_canonical_decimal(value: str, label: str) -> Decimal:
    number = _decimal_or_none(value)
    if number is None:
        raise ValueError(f"invalid {label}")
    if canonical_decimal(number) != value:
        raise ValueError(f"non-canonical {label}")
    return number


def derive_average_cost(purchase_amount: str, quantity: str) -> str | None:
    """Average cost derived as purchase_amount / quantity, exactly.

    The broker account-status export publishes a purchase amount, not an
    average cost, so ``HoldingItemV1.avg_price`` is always DERIVED here. The
    derivation is quantized to 10 decimal places with ROUND_HALF_UP inside a
    50-digit Decimal context; no ``float`` ever touches the value. Both
    operands are covered by the reconciliation digest, so the derivation stays
    auditable and re-computable.

    Returns ``None`` when quantity is not positive, in which case the row
    cannot become a holding (``HoldingItemV1`` requires a non-empty price and a
    placeholder would be fabrication).
    """
    amount = _decimal_or_none(purchase_amount)
    units = _decimal_or_none(quantity)
    if amount is None:
        raise ValueError("invalid purchase_amount")
    if units is None:
        raise ValueError("invalid quantity")
    if units <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        quotient = (amount / units).quantize(
            _DERIVED_AVERAGE_COST_EXPONENT, rounding=ROUND_HALF_UP,
        )
    return canonical_decimal(quotient)


# ---------------------------------------------------------------------------
# Parse-stage source row (never persisted, never canonical)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PensionSourceRowV1:
    """One raw account-status row as read, before classification.

    This is a parse-stage value only. It is never written to any store and is
    not a canonical record: the sole canonical holdings record remains
    ``PensionHoldingsRevisionV1``.
    """

    ordinal: int
    instrument_repr: str = field(repr=False)
    quantity: str
    purchase_amount: str
    # Optional source columns. ``None`` means the workbook did not supply the
    # value (column absent or cell blank); it is never substituted.
    evaluation_amount: str | None = field(default=None, repr=False)
    profit_loss: str | None = field(default=None, repr=False)
    return_rate: str | None = field(default=None, repr=False)
    weight: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("invalid ordinal")
        if type(self.instrument_repr) is not str or not self.instrument_repr.strip():
            raise ValueError("invalid instrument_repr")
        if len(self.instrument_repr) > MAX_INSTRUMENT_REPR_LENGTH:
            raise ValueError("oversized instrument_repr")
        for name in ("quantity", "purchase_amount"):
            _require_canonical_decimal(getattr(self, name), name)
        for name in ("evaluation_amount", "profit_loss", "return_rate", "weight"):
            value = getattr(self, name)
            if value is not None:
                _require_canonical_decimal(value, name)


# ---------------------------------------------------------------------------
# Reviewed classification plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PensionRowDispositionV1:
    """The reviewed decision for exactly one source row."""

    ordinal: int
    disposition: str
    symbol: str | None
    exclusion_kind: str | None

    def __post_init__(self):
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("invalid ordinal")
        if self.disposition not in DISPOSITIONS:
            raise ValueError("invalid disposition")
        if self.disposition == DISPOSITION_VERIFIED_KRX_ETF:
            if type(self.symbol) is not str:
                raise ValueError("verified ETF requires a symbol")
            if KRX_SHORT_CODE_RE.fullmatch(self.symbol) is None:
                raise ValueError("invalid KRX short code")
            if self.exclusion_kind is not None:
                raise ValueError("verified ETF must not carry an exclusion")
        else:
            if self.symbol is not None:
                raise ValueError("excluded row must not carry a symbol")
            if self.exclusion_kind not in EXCLUSION_KINDS:
                raise ValueError("excluded row requires an exclusion kind")


@dataclass(frozen=True)
class PensionClassificationPlanV1:
    """The bounded, reviewed classification of every account-status row.

    Every source row must appear exactly once. The declared counts are a
    reconciliation contract: the parser refuses to build a candidate when the
    workbook does not match what the reviewer approved.
    """

    plan_version: str
    expected_row_count: int
    expected_etf_count: int
    expected_excluded_count: int
    rows: tuple[PensionRowDispositionV1, ...] = field(repr=False)

    def __post_init__(self):
        if self.plan_version != CLASSIFICATION_PLAN_VERSION:
            raise PensionClassificationError("invalid_plan_version")
        for name in ("expected_row_count", "expected_etf_count",
                     "expected_excluded_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise PensionClassificationError(f"invalid_{name}")
        if self.expected_row_count > MAX_ACCOUNT_STATUS_ROWS:
            raise PensionClassificationError("plan_too_large")
        if type(self.rows) is not tuple or not all(
                isinstance(row, PensionRowDispositionV1) for row in self.rows):
            raise PensionClassificationError("invalid_rows")
        if len(self.rows) != self.expected_row_count:
            raise PensionClassificationError("plan_row_count_mismatch")
        if [row.ordinal for row in self.rows] != list(
                range(1, self.expected_row_count + 1)):
            raise PensionClassificationError("plan_ordinals_not_contiguous")
        verified = [row for row in self.rows
                    if row.disposition == DISPOSITION_VERIFIED_KRX_ETF]
        excluded = [row for row in self.rows
                    if row.disposition == DISPOSITION_EXCLUDED_NON_ETF]
        if len(verified) != self.expected_etf_count:
            raise PensionClassificationError("plan_etf_count_mismatch")
        if len(excluded) != self.expected_excluded_count:
            raise PensionClassificationError("plan_excluded_count_mismatch")
        symbols = [row.symbol for row in verified]
        if len(set(symbols)) != len(symbols):
            raise PensionClassificationError("plan_duplicate_symbol")

    def disposition_for(self, ordinal: int) -> PensionRowDispositionV1:
        """The reviewed decision for *ordinal*, or a typed failure."""
        for row in self.rows:
            if row.ordinal == ordinal:
                return row
        raise PensionClassificationError("unclassified_row")

    def excluded_kind_counts(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for row in self.rows:
            if row.disposition == DISPOSITION_EXCLUDED_NON_ETF:
                counts[row.exclusion_kind] = counts.get(row.exclusion_kind, 0) + 1
        return tuple(sorted(counts.items()))


def classification_plan_digest(plan: PensionClassificationPlanV1) -> str:
    """Stable digest of the reviewed plan, for provenance. No private name."""
    payload = {
        "v": 1,
        "domain": "tokmo-v3:pension-account-status-plan:v1",
        "plan_version": plan.plan_version,
        "expected_row_count": plan.expected_row_count,
        "expected_etf_count": plan.expected_etf_count,
        "expected_excluded_count": plan.expected_excluded_count,
        "rows": [
            {
                "ordinal": row.ordinal,
                "disposition": row.disposition,
                "symbol": row.symbol,
                "exclusion_kind": row.exclusion_kind,
            }
            for row in sorted(plan.rows, key=lambda item: item.ordinal)
        ],
    }
    return hashlib.sha256(canonical_encode(payload)).hexdigest()


def account_status_reconciliation_digest(
    source_rows: tuple[PensionSourceRowV1, ...],
    plan: PensionClassificationPlanV1,
    average_costs: dict[int, str | None],
) -> str:
    """PRIVATE reconciliation evidence over EVERY source row.

    This is the field that preserves the excluded non-ETF assets. It covers all
    source rows — included and excluded alike — together with their reviewed
    disposition and the derived-average-cost provenance, so the fact that the
    workbook held more rows than the revision holds is tamper-evident without
    any schema change and without a second canonical store.

    Never projected to any public DTO, payload, or log line.
    """
    payload = {
        "v": 1,
        "domain": "tokmo-v3:pension-account-status-reconciliation:v1",
        "plan_digest": classification_plan_digest(plan),
        "source_row_count": len(source_rows),
        "rows": [
            {
                "ordinal": row.ordinal,
                "instrument_name": row.instrument_repr,
                "quantity": row.quantity,
                "purchase_amount": row.purchase_amount,
                "evaluation_amount": row.evaluation_amount,
                "profit_loss": row.profit_loss,
                "return_rate": row.return_rate,
                "weight": row.weight,
                "disposition": plan.disposition_for(row.ordinal).disposition,
                "symbol": plan.disposition_for(row.ordinal).symbol,
                "exclusion_kind": plan.disposition_for(row.ordinal).exclusion_kind,
                "derived_average_cost": average_costs.get(row.ordinal),
                "average_cost_provenance": (
                    AVERAGE_COST_DERIVED
                    if average_costs.get(row.ordinal) is not None else None
                ),
            }
            for row in sorted(source_rows, key=lambda item: item.ordinal)
        ],
    }
    return hashlib.sha256(canonical_encode(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Parse result — a truthful coverage statement alongside the V1 revision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PensionAccountStatusResultV1:
    """The V1 candidate revision plus what it does and does not cover.

    ``revision`` is an ordinary ``PensionHoldingsRevisionV1`` destined for the
    one existing registry. The remaining fields exist so no caller can read the
    revision as a whole-account inventory: ``source_row_count`` stays the full
    workbook row count while ``revision.holding_count`` is ETF-only.
    """

    revision: PensionHoldingsRevisionV1 = field(repr=False)
    source_row_count: int
    included_etf_count: int
    excluded_row_count: int
    excluded_kind_counts: tuple[tuple[str, int], ...]
    plan_digest: str
    coverage_statement: str

    def __post_init__(self):
        if not isinstance(self.revision, PensionHoldingsRevisionV1):
            raise ValueError("invalid revision")
        if self.revision.holding_count != self.included_etf_count:
            raise ValueError("included_etf_count mismatch")
        if self.included_etf_count + self.excluded_row_count != self.source_row_count:
            raise ValueError("row accounting mismatch")
        if sum(count for _kind, count in self.excluded_kind_counts) != (
                self.excluded_row_count):
            raise ValueError("excluded_kind_counts mismatch")
        if len(self.plan_digest) != 64:
            raise ValueError("invalid plan_digest")
        if self.coverage_statement not in COVERAGE_STATEMENTS:
            raise ValueError("invalid coverage_statement")
        expected = (
            COVERAGE_ETF_ONLY_WHOLE if self.excluded_row_count == 0
            else COVERAGE_ETF_ONLY_PARTIAL
        )
        if self.coverage_statement != expected:
            raise ValueError("coverage_statement mismatch")


# ---------------------------------------------------------------------------
# Period summary — aggregate evidence, never holdings and never an execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PensionPeriodSummaryRowV1:
    """One aggregate instrument row of the broker period summary.

    Deliberately carries no trade date and no order identity: the source has
    neither, and inventing either would fabricate executions.
    """

    ordinal: int
    instrument_repr: str = field(repr=False)
    buy_quantity: str
    sell_quantity: str
    buy_average_price: str = field(repr=False)
    sell_average_price: str = field(repr=False)
    buy_amount: str = field(repr=False)
    sell_amount: str = field(repr=False)
    cost_amount: str = field(repr=False)
    profit_loss: str = field(repr=False)
    return_rate: str = field(repr=False)

    def __post_init__(self):
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("invalid ordinal")
        if type(self.instrument_repr) is not str or not self.instrument_repr.strip():
            raise ValueError("invalid instrument_repr")
        for name in ("buy_quantity", "sell_quantity", "buy_average_price",
                     "sell_average_price", "buy_amount", "sell_amount",
                     "cost_amount", "profit_loss", "return_rate"):
            _require_canonical_decimal(getattr(self, name), name)


@dataclass(frozen=True)
class PensionPeriodSummaryEvidenceV1:
    """The period summary as NON-HOLDINGS, NON-EXECUTION evidence.

    It is not persisted by this slice — there is no canonical store for it and
    none is created, because a second canonical family is forbidden. It cannot
    reach the holdings revision (wrong type for ``PensionHoldingsRevisionV1``)
    and it cannot reach the execution ledger (no date, no order identity).
    Durable persistence, if ever wanted, is a separate later decision.
    """

    source_content_sha256: str = field(repr=False)
    rows: tuple[PensionPeriodSummaryRowV1, ...] = field(repr=False)
    row_count: int
    semantic_digest: str = field(repr=False)
    aggregate_only: bool

    def __post_init__(self):
        if len(self.source_content_sha256) != 64:
            raise ValueError("invalid source_content_sha256")
        if type(self.rows) is not tuple or not all(
                isinstance(row, PensionPeriodSummaryRowV1) for row in self.rows):
            raise ValueError("invalid rows")
        if len(self.rows) != self.row_count:
            raise ValueError("row_count mismatch")
        if [row.ordinal for row in self.rows] != list(
                range(1, self.row_count + 1)):
            raise ValueError("row ordinals must be contiguous from 1")
        if len(self.semantic_digest) != 64:
            raise ValueError("invalid semantic_digest")
        if self.aggregate_only is not True:
            raise ValueError("period summary is always an aggregate fact")


def period_summary_semantic_digest(rows) -> str:
    """PUBLIC-SAFE digest over the aggregate rows, without product names."""
    payload = {
        "v": 1,
        "domain": "tokmo-v3:pension-period-summary-semantic:v1",
        "rows": [
            {
                "ordinal": row.ordinal,
                "buy_quantity": row.buy_quantity,
                "sell_quantity": row.sell_quantity,
                "buy_average_price": row.buy_average_price,
                "sell_average_price": row.sell_average_price,
                "buy_amount": row.buy_amount,
                "sell_amount": row.sell_amount,
                "cost_amount": row.cost_amount,
                "profit_loss": row.profit_loss,
                "return_rate": row.return_rate,
            }
            for row in sorted(rows, key=lambda item: item.ordinal)
        ],
    }
    return hashlib.sha256(canonical_encode(payload)).hexdigest()
