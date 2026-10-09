"""Hardened parser for the broker account-status workbook shape.

It normalizes into the EXISTING canonical pension type,
``PensionHoldingsRevisionV1``, so the single canonical authority stays
``pension_registry.sqlite3`` with its one ACTIVE pointer and its one existing
read path. No second store, schema, resolver or ACTIVE pointer is introduced,
and the registry DDL is not migrated.

Two workbook shapes are read here, and only one of them can become holdings:

  account status  — one visible ``Sheet1``, one header row, columns mapped by
                    header NAME: three required (종목명 / 보유량 / 매입금액),
                    the rest optional and non-blocking. Rows explicitly
                    classified as verified KRX ETFs by the reviewed plan become
                    ``HoldingItemV1``; every other row is excluded from the
                    revision by design.
  period summary  — one visible ``Sheet1``, two header rows, 10 exact columns.
                    Returns NON-HOLDINGS, NON-EXECUTION evidence only. It can
                    never become a holding and never reaches the ledger.

ETF status is never inferred. A row is admitted only when the reviewed plan
names it and supplies its short code, and the plan's declared counts must
reconcile against the workbook or the whole parse fails closed. A corporate
bond can therefore never be admitted because its code or name looked tradable.

File-system, ZIP-container and workbook-load hardening is REUSED from
``facts/pension_excel.py`` rather than forked, so those security checks live in
exactly one place. A test pins the object identity of the reused helpers.

Only typed values leave this module. The private source product name travels
inside typed DTOs and the private reconciliation digest, never in an error
reason or a log line.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tokmo.facts.pension_excel import (
    WORKBOOK_MAX_CELL_STRING_LENGTH,
    PensionExcelParseError,
)
from tokmo.facts.pension_excel import (
    _check_landing_file as check_landing_file,
)
from tokmo.facts.pension_excel import (
    _load_workbook_from_bytes as load_workbook_from_bytes,
)
from tokmo.facts.pension_excel import (
    _parse_cell_decimal as parse_cell_decimal,
)
from tokmo.facts.pension_excel import (
    _zip_entry_pass as zip_entry_pass,
)
from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.pension_account_status import (
    COVERAGE_ETF_ONLY_PARTIAL,
    COVERAGE_ETF_ONLY_WHOLE,
    DISPOSITION_VERIFIED_KRX_ETF,
    MAX_ACCOUNT_STATUS_ROWS,
    PENSION_ACCOUNT_STATUS_SHEET_SCHEMA_VERSION,
    PensionAccountStatusResultV1,
    PensionClassificationError,
    PensionClassificationPlanV1,
    PensionPeriodSummaryEvidenceV1,
    PensionPeriodSummaryRowV1,
    PensionSourceRowV1,
    account_status_reconciliation_digest,
    classification_plan_digest,
    derive_average_cost,
    period_summary_semantic_digest,
)
from tokmo.values.pension_holdings import (
    PENSION_HOLDINGS_SCHEMA_VERSION,
    PENSION_PARSER_VERSION,
    PensionHoldingsRevisionV1,
    pension_semantic_digest,
)

# --- account status contract -----------------------------------------------

ACCOUNT_STATUS_SHEET_NAME = "Sheet1"
ACCOUNT_STATUS_HEADER_ROW = 1
ACCOUNT_STATUS_DATA_START_ROW = 2

# User-approved contract: the three required columns are mapped BY NAME, in
# any order. The remaining recognised columns are optional and non-blocking;
# an unrecognised column is ignored. ``avg_price`` is always derived from
# purchase_amount / quantity, so nothing optional feeds a holding.
ACCOUNT_STATUS_REQUIRED_FIELDS = (
    "instrument_repr",
    "quantity",
    "purchase_amount",
)
ACCOUNT_STATUS_OPTIONAL_FIELDS = (
    "evaluation_amount",
    "profit_loss",
    "return_rate",
    "weight",
)
ACCOUNT_STATUS_FIELDS = ACCOUNT_STATUS_REQUIRED_FIELDS + ACCOUNT_STATUS_OPTIONAL_FIELDS

# OPEN-1: the exact broker header spellings are declared here and confirmed
# against the real workbook at the operational import gate. Anything outside
# this contract fails closed, so a mismatch is a loud failure at the gate and
# never a silent mis-parse. Adding a confirmed spelling is an alias addition
# with no schema change.
ACCOUNT_STATUS_HEADER_ALIASES: dict[str, frozenset[str]] = {
    "instrument_repr": frozenset({"instrument_repr", "상품명", "종목명"}),
    "quantity": frozenset({"quantity", "수량", "보유량", "보유수량", "잔고수량"}),
    "purchase_amount": frozenset({"purchase_amount", "매입금액", "매수금액"}),
    "evaluation_amount": frozenset({"evaluation_amount", "평가금액"}),
    "profit_loss": frozenset({"profit_loss", "평가손익", "손익금액"}),
    "return_rate": frozenset({"return_rate", "수익률", "수익율"}),
    "weight": frozenset({"weight", "비중", "구성비중"}),
}

# --- period summary contract -----------------------------------------------

PERIOD_SUMMARY_SHEET_NAME = "Sheet1"
PERIOD_SUMMARY_GROUP_HEADER_ROW = 1
PERIOD_SUMMARY_LEAF_HEADER_ROW = 2
PERIOD_SUMMARY_DATA_START_ROW = 3
PERIOD_SUMMARY_COLUMN_COUNT = 10
PERIOD_SUMMARY_MAX_ROWS = 64

PERIOD_SUMMARY_FIELDS = (
    "instrument_repr",
    "buy_quantity",
    "sell_quantity",
    "buy_average_price",
    "sell_average_price",
    "buy_amount",
    "sell_amount",
    "cost_amount",
    "profit_loss",
    "return_rate",
)

PERIOD_SUMMARY_HEADER_ALIASES: dict[str, frozenset[str]] = {
    "instrument_repr": frozenset({"instrument_repr", "상품명", "종목명"}),
    "buy_quantity": frozenset({"buy_quantity", "매수수량"}),
    "sell_quantity": frozenset({"sell_quantity", "매도수량"}),
    "buy_average_price": frozenset({"buy_average_price", "매수평균단가", "매수단가"}),
    "sell_average_price": frozenset({"sell_average_price", "매도평균단가", "매도단가"}),
    "buy_amount": frozenset({"buy_amount", "매수금액"}),
    "sell_amount": frozenset({"sell_amount", "매도금액"}),
    "cost_amount": frozenset({"cost_amount", "비용", "수수료"}),
    "profit_loss": frozenset({"profit_loss", "손익", "실현손익"}),
    "return_rate": frozenset({"return_rate", "수익률", "수익율"}),
}

# The market of an admitted row is asserted by the reviewed plan, not read from
# a source column, so this names the plan rather than a workbook field.
ADMITTED_MARKET_RAW = "KR"
ADMITTED_MARKET_FIELD = "classification_plan"
ADMITTED_CURRENCY = "KRW"


def _normalize_header(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold().strip()


def _parse_header_row(
    sheet,
    row_index: int,
    expected_fields: tuple[str, ...],
    aliases: dict[str, frozenset[str]],
) -> None:
    """Positionally exact header validation. Fails closed on anything else."""
    for position, canonical_field in enumerate(expected_fields, 1):
        value = sheet.cell(row=row_index, column=position).value
        if not isinstance(value, str) or not value.strip():
            raise PensionExcelParseError("empty_header")
        if len(value) > WORKBOOK_MAX_CELL_STRING_LENGTH:
            raise PensionExcelParseError("oversized_cell_string")
        normalized = _normalize_header(value)
        known = any(normalized in alias_set for alias_set in aliases.values())
        if not known:
            raise PensionExcelParseError("unmapped_header")
        if normalized not in aliases[canonical_field]:
            raise PensionExcelParseError("header_contract_mismatch")


def _map_header_row(
    sheet,
    row_index: int,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...],
    aliases: dict[str, frozenset[str]],
) -> dict[str, int]:
    """Name-based header mapping: canonical field -> 1-based column.

    Order carries no meaning. Every required field must appear exactly once;
    a recognised optional field may appear at most once; an unrecognised
    header is ignored rather than blocking. An empty or oversized header cell
    still fails closed because it is malformed, not merely unknown.
    """
    columns: dict[str, int] = {}
    for position in range(1, (sheet.max_column or 0) + 1):
        value = sheet.cell(row=row_index, column=position).value
        if not isinstance(value, str) or not value.strip():
            raise PensionExcelParseError("empty_header")
        if len(value) > WORKBOOK_MAX_CELL_STRING_LENGTH:
            raise PensionExcelParseError("oversized_cell_string")
        normalized = _normalize_header(value)
        for canonical_field in required_fields + optional_fields:
            if normalized in aliases[canonical_field]:
                if canonical_field in columns:
                    raise PensionExcelParseError("duplicate_header")
                columns[canonical_field] = position
                break
    for canonical_field in required_fields:
        if canonical_field not in columns:
            raise PensionExcelParseError("missing_required_header")
    return columns


def _validate_content(sheet, *, data_start_row: int, max_rows: int) -> int:
    """Formula / hidden-row / merged-cell checks over the data region.

    Merged cells are tolerated only when they lie ENTIRELY above
    *data_start_row*, so a broker group-header row may span columns while a
    merge that reaches into the data region is rejected. Testing ``max_row``
    rather than ``min_row`` matters: a merge anchored in the last header row but
    spilling into the first data row would otherwise pass, and the covered data
    cells would read as empty.
    Returns the last populated row index.
    """
    max_row = sheet.max_row or 0
    if max_row - data_start_row + 1 > max_rows:
        raise PensionExcelParseError("too_many_rows")
    max_col = sheet.max_column or 0
    for row in sheet.iter_rows(
        min_row=data_start_row, max_row=max_row, max_col=max_col
    ):
        for cell in row:
            if cell.data_type == "f":
                raise PensionExcelParseError("formula_detected")
    for index in range(data_start_row, max_row + 1):
        dimension = sheet.row_dimensions.get(index)
        if dimension is not None and dimension.hidden:
            raise PensionExcelParseError("hidden_row_detected")
    for merged in sheet.merged_cells.ranges:
        if merged.max_row >= data_start_row:
            raise PensionExcelParseError("merged_cell_detected")
    return max_row


def _require_exact_columns(sheet, expected: int) -> None:
    if (sheet.max_column or 0) != expected:
        raise PensionExcelParseError("wrong_column_count")


def _require_sheet_name(sheet, expected: str) -> None:
    if sheet.title != expected:
        raise PensionExcelParseError("wrong_sheet_name")


def _cell_text(sheet, row: int, column: int) -> str:
    value = sheet.cell(row=row, column=column).value
    if not isinstance(value, str) or not value.strip():
        raise PensionExcelParseError("invalid_instrument_repr")
    if len(value) > WORKBOOK_MAX_CELL_STRING_LENGTH:
        raise PensionExcelParseError("oversized_cell_string")
    return value.strip()


def _cell_decimal(sheet, row: int, column: int) -> str:
    value = sheet.cell(row=row, column=column).value
    if value is None or isinstance(value, bool):
        raise PensionExcelParseError("invalid_decimal")
    if not isinstance(value, (int, float, Decimal, str)):
        raise PensionExcelParseError("invalid_decimal")
    # parse_cell_decimal goes through Decimal(str(value)) — never
    # Decimal(float) — so no binary expansion can enter the record.
    return parse_cell_decimal(value)


def _optional_cell_decimal(sheet, row: int, column: int) -> str | None:
    """A blank optional cell is absent (``None``), never a substituted value.
    A populated one must still be a valid decimal."""
    value = sheet.cell(row=row, column=column).value
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _cell_decimal(sheet, row, column)


def _require_aware(value, label: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise PensionExcelParseError(f"invalid_{label}")


# ---------------------------------------------------------------------------
# Account status → the one canonical V1 revision (ETF-only)
# ---------------------------------------------------------------------------


def parse_pension_account_status(
    path: Path,
    *,
    now: datetime,
    source_as_of: datetime,
    plan: PensionClassificationPlanV1,
) -> PensionAccountStatusResultV1:
    """Normalize the account-status workbook into a V1 candidate revision.

    ``source_as_of`` is operator-supplied: the source columns carry no date,
    so the as-of instant is a confirmed fact, never inferred.

    ``plan`` is the reviewed, bounded classification of every row. Rows it
    declares verified KRX ETFs become ``HoldingItemV1``; all others are
    excluded from the revision by user-approved design and survive only inside
    the private reconciliation digest.

    Reads the file, mutates nothing, writes to no store, touches no network.
    """
    _require_aware(now, "now")
    _require_aware(source_as_of, "source_as_of")
    if not isinstance(plan, PensionClassificationPlanV1):
        raise PensionExcelParseError("invalid_classification_plan")

    raw = check_landing_file(path)
    source_content_sha256 = hashlib.sha256(raw).hexdigest()
    zip_entry_pass(raw)

    workbook, sheet = load_workbook_from_bytes(raw)
    try:
        _require_sheet_name(sheet, ACCOUNT_STATUS_SHEET_NAME)
        max_row = _validate_content(
            sheet,
            data_start_row=ACCOUNT_STATUS_DATA_START_ROW,
            max_rows=MAX_ACCOUNT_STATUS_ROWS,
        )
        columns = _map_header_row(
            sheet,
            ACCOUNT_STATUS_HEADER_ROW,
            ACCOUNT_STATUS_REQUIRED_FIELDS,
            ACCOUNT_STATUS_OPTIONAL_FIELDS,
            ACCOUNT_STATUS_HEADER_ALIASES,
        )

        row_count = max_row - ACCOUNT_STATUS_DATA_START_ROW + 1
        if row_count < 1:
            raise PensionExcelParseError("empty_account_status")
        # Reconciliation contract: the workbook must match what was reviewed.
        if row_count != plan.expected_row_count:
            raise PensionExcelParseError("row_count_contract_mismatch")

        source_rows: list[PensionSourceRowV1] = []
        seen_names: set[str] = set()
        for ordinal, row_index in enumerate(
            range(ACCOUNT_STATUS_DATA_START_ROW, max_row + 1), 1
        ):
            name = _cell_text(sheet, row_index, columns["instrument_repr"])
            if name in seen_names:
                raise PensionExcelParseError("duplicate_instrument_repr")
            seen_names.add(name)
            optional = {
                field_name: (
                    _optional_cell_decimal(sheet, row_index, columns[field_name])
                    if field_name in columns
                    else None
                )
                for field_name in ACCOUNT_STATUS_OPTIONAL_FIELDS
            }
            source_rows.append(
                PensionSourceRowV1(
                    ordinal=ordinal,
                    instrument_repr=name,
                    quantity=_cell_decimal(sheet, row_index, columns["quantity"]),
                    purchase_amount=_cell_decimal(
                        sheet, row_index, columns["purchase_amount"]
                    ),
                    **optional,
                )
            )

        holdings: list[HoldingItemV1] = []
        average_costs: dict[int, str | None] = {}
        excluded_count = 0
        for row in source_rows:
            try:
                decision = plan.disposition_for(row.ordinal)
            except PensionClassificationError as error:
                raise PensionExcelParseError(error.reason) from error
            average_cost = derive_average_cost(row.purchase_amount, row.quantity)
            average_costs[row.ordinal] = average_cost
            if decision.disposition != DISPOSITION_VERIFIED_KRX_ETF:
                excluded_count += 1
                continue
            if average_cost is None:
                # A verified ETF with a non-positive quantity cannot be priced,
                # and a placeholder price would be fabrication.
                raise PensionExcelParseError("non_positive_etf_quantity")
            holdings.append(
                HoldingItemV1(
                    symbol=decision.symbol,
                    quantity=row.quantity,
                    avg_price=average_cost,
                    currency=ADMITTED_CURRENCY,
                    market_raw=ADMITTED_MARKET_RAW,
                    market_field=ADMITTED_MARKET_FIELD,
                )
            )

        if len(holdings) != plan.expected_etf_count:
            raise PensionExcelParseError("etf_count_contract_mismatch")
        if excluded_count != plan.expected_excluded_count:
            raise PensionExcelParseError("excluded_count_contract_mismatch")

        frozen_holdings = tuple(holdings)
        frozen_rows = tuple(source_rows)
        revision = PensionHoldingsRevisionV1(
            revision_id=uuid4().hex,
            schema_version=PENSION_HOLDINGS_SCHEMA_VERSION,
            source_content_sha256=source_content_sha256,
            # The normalized OUTPUT contract is unchanged, which is what keeps
            # the one canonical read path and the composite payload validator
            # intact. The differing INPUT shape is recorded below.
            parser_version=PENSION_PARSER_VERSION,
            sheet_schema_version=PENSION_ACCOUNT_STATUS_SHEET_SCHEMA_VERSION,
            holdings=frozen_holdings,
            source_as_of=source_as_of,
            uploaded_at=now,
            validated_at=now,
            semantic_digest=pension_semantic_digest(frozen_holdings),
            reconciliation_digest=account_status_reconciliation_digest(
                frozen_rows, plan, average_costs
            ),
            holding_count=len(frozen_holdings),
            # An ETF-only revision with no ETFs is NOT a liquidation: the
            # account may still hold excluded non-ETF assets.
            liquidation_flag=0,
        )
        return PensionAccountStatusResultV1(
            revision=revision,
            source_row_count=len(frozen_rows),
            included_etf_count=len(frozen_holdings),
            excluded_row_count=excluded_count,
            excluded_kind_counts=plan.excluded_kind_counts(),
            plan_digest=classification_plan_digest(plan),
            coverage_statement=(
                COVERAGE_ETF_ONLY_WHOLE
                if excluded_count == 0
                else COVERAGE_ETF_ONLY_PARTIAL
            ),
        )
    finally:
        workbook.close()


# ---------------------------------------------------------------------------
# Period summary → non-holdings, non-execution evidence
# ---------------------------------------------------------------------------


def parse_pension_period_summary(
    path: Path,
    *,
    now: datetime,
) -> PensionPeriodSummaryEvidenceV1:
    """Read the broker period summary as aggregate evidence only.

    The result carries no trade date and no order identity because the source
    has neither, it is not a holdings type, and this slice persists it nowhere.
    """
    _require_aware(now, "now")
    raw = check_landing_file(path)
    source_content_sha256 = hashlib.sha256(raw).hexdigest()
    zip_entry_pass(raw)

    workbook, sheet = load_workbook_from_bytes(raw)
    try:
        _require_sheet_name(sheet, PERIOD_SUMMARY_SHEET_NAME)
        _require_exact_columns(sheet, PERIOD_SUMMARY_COLUMN_COUNT)
        max_row = _validate_content(
            sheet,
            data_start_row=PERIOD_SUMMARY_DATA_START_ROW,
            max_rows=PERIOD_SUMMARY_MAX_ROWS,
        )
        # Row 1 carries broker group labels; it is bounded-validated only.
        for column in range(1, PERIOD_SUMMARY_COLUMN_COUNT + 1):
            value = sheet.cell(row=PERIOD_SUMMARY_GROUP_HEADER_ROW, column=column).value
            if value is not None and not isinstance(value, str):
                raise PensionExcelParseError("invalid_group_header")
            if isinstance(value, str) and (
                len(value) > WORKBOOK_MAX_CELL_STRING_LENGTH
            ):
                raise PensionExcelParseError("oversized_cell_string")
        _parse_header_row(
            sheet,
            PERIOD_SUMMARY_LEAF_HEADER_ROW,
            PERIOD_SUMMARY_FIELDS,
            PERIOD_SUMMARY_HEADER_ALIASES,
        )

        rows: list[PensionPeriodSummaryRowV1] = []
        seen_names: set[str] = set()
        for ordinal, row_index in enumerate(
            range(PERIOD_SUMMARY_DATA_START_ROW, max_row + 1), 1
        ):
            name = _cell_text(sheet, row_index, 1)
            if name in seen_names:
                raise PensionExcelParseError("duplicate_instrument_repr")
            seen_names.add(name)
            rows.append(
                PensionPeriodSummaryRowV1(
                    ordinal=ordinal,
                    instrument_repr=name,
                    buy_quantity=_cell_decimal(sheet, row_index, 2),
                    sell_quantity=_cell_decimal(sheet, row_index, 3),
                    buy_average_price=_cell_decimal(sheet, row_index, 4),
                    sell_average_price=_cell_decimal(sheet, row_index, 5),
                    buy_amount=_cell_decimal(sheet, row_index, 6),
                    sell_amount=_cell_decimal(sheet, row_index, 7),
                    cost_amount=_cell_decimal(sheet, row_index, 8),
                    profit_loss=_cell_decimal(sheet, row_index, 9),
                    return_rate=_cell_decimal(sheet, row_index, 10),
                )
            )

        frozen = tuple(rows)
        return PensionPeriodSummaryEvidenceV1(
            source_content_sha256=source_content_sha256,
            rows=frozen,
            row_count=len(frozen),
            semantic_digest=period_summary_semantic_digest(frozen),
            aggregate_only=True,
        )
    finally:
        workbook.close()
