"""Strict query/parser boundary for one official KRX daily-trade row."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from tokmo.facts.eia import LIVE_TRANSPORT_FAILURE_CODES
from tokmo.values import KRX_SOURCE, KrxDailyTradeFact, UnavailableFact

SOURCE = KRX_SOURCE
ENDPOINT_ID = "krx_kospi_daily_trade"
TARGET_STOCK_CODE = "000660"
TARGET_MARKET = "KOSPI"
REQUEST_TIMEOUT_SECONDS = 10.0
FIELDS = (
    "BAS_DD",
    "ISU_CD",
    "ISU_NM",
    "MKT_NM",
    "SECT_TP_NM",
    "TDD_CLSPRC",
    "CMPPREVDD_PRC",
    "FLUC_RT",
    "TDD_OPNPRC",
    "TDD_HGPRC",
    "TDD_LWPRC",
    "ACC_TRDVOL",
    "ACC_TRDVAL",
    "MKTCAP",
    "LIST_SHRS",
)


@dataclass(frozen=True)
class KrxRequest:
    endpoint_id: str
    base_date: str
    timeout_seconds: float
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class _KrxTransportSuccess:
    body: bytes = field(repr=False)
    status: int = 200


@dataclass(frozen=True)
class _KrxTransportFailure:
    code: str

    def __post_init__(self):
        if self.code not in LIVE_TRANSPORT_FAILURE_CODES:
            raise ValueError("live_transport_code_outside_contract")


@dataclass(frozen=True)
class KrxAttemptOutcome:
    code: str


@dataclass(frozen=True)
class KrxQueryResult:
    result: KrxDailyTradeFact | UnavailableFact
    attempt: KrxAttemptOutcome


def build_daily_request(*, base_date: str, api_key: str) -> KrxRequest:
    return KrxRequest(ENDPOINT_ID, base_date, REQUEST_TIMEOUT_SECONDS, api_key)


def parse_daily_trade(
    body: bytes, *, requested_date: str, observed_at: datetime
) -> KrxDailyTradeFact | UnavailableFact:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if not isinstance(payload, dict) or set(payload) != {"OutBlock_1"}:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    rows = payload["OutBlock_1"]
    if not isinstance(rows, list):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if not rows:
        return UnavailableFact(SOURCE, "no_published_data", observed_at)
    targets = []
    for row in rows:
        if isinstance(row, dict) and row.get("ISU_CD") == TARGET_STOCK_CODE:
            targets.append(row)
    if len(targets) != 1:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    row = targets[0]
    if not _valid_wire_row(row):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if row["BAS_DD"] != requested_date or row["MKT_NM"].strip() != TARGET_MARKET:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if not row["ISU_NM"].strip():
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    try:
        base_date = datetime.strptime(row["BAS_DD"], "%Y%m%d").date()
        decimals = [_decimal(row[name]) for name in FIELDS[5:11]]
        integers = [_integer(row[name]) for name in FIELDS[11:]]
    except (ValueError, InvalidOperation):
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    return KrxDailyTradeFact(
        SOURCE,
        base_date,
        TARGET_STOCK_CODE,
        row["ISU_NM"].strip(),
        TARGET_MARKET,
        row["SECT_TP_NM"].strip(),
        *decimals,
        *integers,
        observed_at,
    )


def _valid_wire_row(row) -> bool:
    if (
        not isinstance(row, dict)
        or set(row) != set(FIELDS)
        or any(not isinstance(row[name], str) for name in FIELDS)
    ):
        return False
    try:
        datetime.strptime(row["BAS_DD"], "%Y%m%d")
        for name in FIELDS[5:11]:
            _decimal(row[name])
        for name in FIELDS[11:]:
            _integer(row[name])
    except (ValueError, InvalidOperation):
        return False
    return all(row[name].strip() for name in ("ISU_CD", "ISU_NM", "MKT_NM"))


def _decimal(value: str) -> Decimal:
    parsed = Decimal(value.replace(",", ""))
    if not parsed.is_finite():
        raise InvalidOperation
    return parsed


def _integer(value: str) -> int:
    cleaned = value.replace(",", "")
    if not cleaned or cleaned.startswith("+") or not cleaned.isascii():
        raise ValueError
    return int(cleaned)


def query_daily_trade(
    *, base_date: str, api_key: str, transport: Callable, clock: Callable[[], datetime]
) -> KrxQueryResult:
    observed_at = clock()
    request = build_daily_request(base_date=base_date, api_key=api_key)
    try:
        outcome = transport(request)
    except Exception:
        outcome = _KrxTransportFailure("protocol_failure")
    if isinstance(outcome, _KrxTransportFailure):
        reason = (
            "rate_limited"
            if outcome.code == "http_429"
            else (
                "contract_mismatch"
                if outcome.code in {"auth_rejection", "bad_request"}
                else "transport_error"
            )
        )
        return KrxQueryResult(
            UnavailableFact(SOURCE, reason, observed_at),
            KrxAttemptOutcome(outcome.code),
        )
    if not isinstance(outcome, _KrxTransportSuccess):
        return KrxQueryResult(
            UnavailableFact(SOURCE, "transport_error", observed_at),
            KrxAttemptOutcome("protocol_failure"),
        )
    fact = parse_daily_trade(
        outcome.body, requested_date=base_date, observed_at=observed_at
    )
    code = (
        "success"
        if isinstance(fact, KrxDailyTradeFact)
        else "provider_no_data"
        if fact.reason == "no_published_data"
        else "response_contract_failure"
    )
    return KrxQueryResult(fact, KrxAttemptOutcome(code))
