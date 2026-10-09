"""Closed FRED request construction and bounded JSON parsing."""

import json
import re
from dataclasses import dataclass
from datetime import date

from tokmo.values.fred_macro import (
    FredOperation,
    FredRequest,
    IdentityProvenance,
    PageCompleteness,
    ReleaseClassification,
    canonical_fred_value,
)

PATHS = {
    FredOperation.SERIES: "/fred/series",
    FredOperation.SERIES_OBSERVATIONS: "/fred/series/observations",
    FredOperation.SERIES_RELEASE: "/fred/series/release",
    FredOperation.RELEASE_DATES: "/fred/release/dates",
    FredOperation.SERIES_VINTAGE_DATES: "/fred/series/vintagedates",
}


@dataclass(frozen=True)
class FredParsedPage:
    operation: FredOperation
    rows: tuple[dict, ...]
    count: int | None
    offset: int | None
    limit: int | None
    completeness: PageCompleteness
    shape_evidence: str = "CONTRACT_VERIFIED"
    completeness_evidence: str = "PAGINATED_ENVELOPE"


def build_query_pairs(request: FredRequest) -> tuple[tuple[str, str], ...]:
    pairs = []
    if request.series_id is not None:
        pairs.append(("series_id", request.series_id))
    if request.release_id is not None:
        pairs.append(("release_id", str(request.release_id)))
    if request.realtime_start is not None:
        pairs.extend(
            (
                ("realtime_start", request.realtime_start.isoformat()),
                ("realtime_end", request.realtime_end.isoformat()),
            )
        )
    if request.observation_start is not None:
        pairs.extend(
            (
                ("observation_start", request.observation_start.isoformat()),
                ("observation_end", request.observation_end.isoformat()),
            )
        )
    if request.operation is FredOperation.SERIES_OBSERVATIONS:
        pairs.extend(
            (
                ("units", "lin"),
                ("output_type", str(request.output_type or 1)),
                ("sort_order", "asc"),
            )
        )
        if request.vintage_dates:
            pairs.append(
                (
                    "vintage_dates",
                    ",".join(value.isoformat() for value in request.vintage_dates),
                )
            )
    elif request.operation is FredOperation.RELEASE_DATES:
        pairs.extend(
            (
                (
                    "include_release_dates_with_no_data",
                    str(request.include_release_dates_with_no_data).lower(),
                ),
                ("sort_order", "desc"),
            )
        )
    elif request.operation is FredOperation.SERIES_VINTAGE_DATES:
        pairs.append(("sort_order", "asc"))
    if request.limit is not None:
        pairs.append(("limit", str(request.limit)))
    if request.operation in (
        FredOperation.SERIES_OBSERVATIONS,
        FredOperation.RELEASE_DATES,
        FredOperation.SERIES_VINTAGE_DATES,
    ):
        pairs.append(("offset", str(request.offset)))
    pairs.append(("file_type", "json"))
    return tuple(pairs)


def _object(body: bytes) -> dict:
    if type(body) is not bytes or len(body) > 1_048_576:
        raise ValueError("invalid_body")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid_json") from error
    if type(value) is not dict:
        raise ValueError("invalid_envelope")
    return value


def _closed(value: dict, required: set[str], optional: set[str] = set()) -> None:
    if not required <= value.keys() or not value.keys() <= required | optional:
        raise ValueError("closed_keys_mismatch")


def _strict_date(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"invalid_{name}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid_{name}") from error
    if parsed.isoformat() != value:
        raise ValueError(f"invalid_{name}")
    return value


def _page(
    payload: dict, rows: list, request: FredRequest
) -> tuple[int, int, int, PageCompleteness]:
    count, offset, limit = payload["count"], payload["offset"], payload["limit"]
    if any(type(value) is not int or value < 0 for value in (count, offset, limit)):
        raise ValueError("invalid_page_counts")
    if offset != request.offset or (
        request.limit is not None and limit != request.limit
    ):
        raise ValueError("page_echo_mismatch")
    return count, offset, limit, PageCompleteness.from_counts(offset, len(rows), count)


def parse_fred_response(request: FredRequest, body: bytes) -> FredParsedPage:
    payload = _object(body)
    op = request.operation
    if op is FredOperation.SERIES:
        _closed(payload, {"realtime_start", "realtime_end", "seriess"})
        rows = payload["seriess"]
        if type(rows) is not list or len(rows) != 1:
            raise ValueError("singleton_page_mismatch")
        row = rows[0]
        required = {
            "id",
            "realtime_start",
            "realtime_end",
            "title",
            "observation_start",
            "observation_end",
            "frequency",
            "frequency_short",
            "units",
            "units_short",
            "seasonal_adjustment",
            "seasonal_adjustment_short",
            "last_updated",
            "popularity",
        }
        _closed(row, required, {"notes"})
        if row["id"] != request.series_id:
            raise ValueError("series_echo_mismatch")
        for key in (
            "realtime_start",
            "realtime_end",
            "observation_start",
            "observation_end",
        ):
            _strict_date(row[key], key)
        result = dict(row, identity_provenance=IdentityProvenance.PROVIDER.value)
        return FredParsedPage(
            op,
            (result,),
            None,
            None,
            None,
            PageCompleteness.COMPLETE,
            completeness_evidence="OFFICIAL_SINGLETON_ENVELOPE",
        )
    if op is FredOperation.SERIES_RELEASE:
        _closed(payload, {"realtime_start", "realtime_end", "releases"})
        rows = payload["releases"]
        if type(rows) is not list or len(rows) != 1:
            raise ValueError("singleton_page_mismatch")
        row = rows[0]
        _closed(
            row,
            {"id", "realtime_start", "realtime_end", "name", "press_release"},
            {"link", "notes"},
        )
        if (
            type(row["id"]) is not int
            or row["id"] <= 0
            or type(row["press_release"]) is not bool
        ):
            raise ValueError("invalid_release")
        result = dict(
            row,
            release_identity_provenance=IdentityProvenance.PROVIDER.value,
            series_id=request.series_id,
            series_identity_provenance=IdentityProvenance.REQUEST.value,
        )
        return FredParsedPage(
            op,
            (result,),
            None,
            None,
            None,
            PageCompleteness.COMPLETE,
            completeness_evidence="OFFICIAL_SINGLETON_ENVELOPE",
        )
    if op is FredOperation.RELEASE_DATES:
        required = {
            "realtime_start",
            "realtime_end",
            "order_by",
            "sort_order",
            "count",
            "offset",
            "limit",
            "release_dates",
        }
        _closed(payload, required)
        if payload["order_by"] != "release_date" or payload["sort_order"] != "desc":
            raise ValueError("page_echo_mismatch")
        rows = payload["release_dates"]
        if type(rows) is not list:
            raise ValueError("invalid_rows")
        normalized = []
        prior_dates = []
        for row in rows:
            _closed(row, {"release_id", "date"})
            if row["release_id"] != request.release_id:
                raise ValueError("release_echo_mismatch")
            day = _strict_date(row["date"], "release_date")
            prior_dates.append(day)
            normalized.append(
                dict(
                    row,
                    date=day,
                    release_identity_provenance=IdentityProvenance.PROVIDER.value,
                )
            )
        if prior_dates != sorted(prior_dates, reverse=True) or len(
            set(prior_dates)
        ) != len(prior_dates):
            raise ValueError("unordered_or_duplicate_rows")
        count, offset, limit, completeness = _page(payload, rows, request)
        return FredParsedPage(op, tuple(normalized), count, offset, limit, completeness)
    if op is FredOperation.SERIES_VINTAGE_DATES:
        required = {
            "realtime_start",
            "realtime_end",
            "order_by",
            "sort_order",
            "count",
            "offset",
            "limit",
            "vintage_dates",
        }
        _closed(payload, required)
        if payload["order_by"] != "vintage_date" or payload["sort_order"] != "asc":
            raise ValueError("page_echo_mismatch")
        if request.realtime_start is not None and (
            payload["realtime_start"] != request.realtime_start.isoformat()
            or payload["realtime_end"] != request.realtime_end.isoformat()
        ):
            raise ValueError("realtime_echo_mismatch")
        rows = payload["vintage_dates"]
        if type(rows) is not list or rows != sorted(set(rows)):
            raise ValueError("invalid_vintage_dates")
        normalized = tuple(
            {
                "series_id": request.series_id,
                "series_identity_provenance": IdentityProvenance.REQUEST.value,
                "vintage_date": _strict_date(value, "vintage_date"),
            }
            for value in rows
        )
        count, offset, limit, completeness = _page(payload, rows, request)
        return FredParsedPage(op, normalized, count, offset, limit, completeness)
    return _parse_observations(request, payload)


def _parse_observations(request: FredRequest, payload: dict) -> FredParsedPage:
    required = {
        "realtime_start",
        "realtime_end",
        "observation_start",
        "observation_end",
        "units",
        "output_type",
        "file_type",
        "order_by",
        "sort_order",
        "count",
        "offset",
        "limit",
        "observations",
    }
    _closed(payload, required)
    expected = {
        "units": "lin",
        "output_type": request.output_type or 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
    }
    if any(payload[key] != value for key, value in expected.items()):
        raise ValueError("observation_echo_mismatch")
    for key in (
        "realtime_start",
        "realtime_end",
        "observation_start",
        "observation_end",
    ):
        requested = getattr(request, key)
        if requested is not None and payload[key] != requested.isoformat():
            raise ValueError(f"{key}_echo_mismatch")
    rows = payload["observations"]
    if type(rows) is not list:
        raise ValueError("invalid_rows")
    normalized = []
    shape = "CONTRACT_VERIFIED"
    effective_output_type = request.output_type or 1
    if effective_output_type in (1, 4):
        prior_dates = []
        for row in rows:
            _closed(row, {"realtime_start", "realtime_end", "date", "value"})
            value, state = canonical_fred_value(row["value"])
            day = _strict_date(row["date"], "observation_date")
            prior_dates.append(day)
            normalized.append(
                {
                    "series_id": request.series_id,
                    "series_identity_provenance": IdentityProvenance.REQUEST.value,
                    "realtime_start": _strict_date(
                        row["realtime_start"], "realtime_start"
                    ),
                    "realtime_end": _strict_date(row["realtime_end"], "realtime_end"),
                    "date": day,
                    "value": value,
                    "value_state": state,
                    "classification": (
                        ReleaseClassification.INITIAL
                        if effective_output_type == 4
                        else ReleaseClassification.UNKNOWN
                    ).value,
                }
            )
        if prior_dates != sorted(prior_dates) or len(set(prior_dates)) != len(
            prior_dates
        ):
            raise ValueError("unordered_or_duplicate_rows")
    else:
        shape = "SHAPE_UNVERIFIED"
        pattern = re.compile(rf"^_?{re.escape(request.series_id or '')}_(\d{{8}})$")
        for row in rows:
            if type(row) is not dict or "date" not in row:
                raise ValueError("dynamic_shape_mismatch")
            day = _strict_date(row["date"], "observation_date")
            dynamic = {}
            for key in sorted(set(row) - {"date"}):
                match = pattern.fullmatch(key)
                if match is None:
                    raise ValueError("dynamic_shape_mismatch")
                vintage = date.fromisoformat(
                    f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}"
                ).isoformat()
                if date.fromisoformat(vintage) not in request.vintage_dates:
                    raise ValueError("unrequested_vintage")
                value, state = canonical_fred_value(row[key])
                dynamic[key] = row[key]
                normalized.append(
                    {
                        "series_id": request.series_id,
                        "date": day,
                        "vintage_date": vintage,
                        "value": value,
                        "value_state": state,
                        "series_identity_provenance": IdentityProvenance.REQUEST.value,
                    }
                )
    count, offset, limit, completeness = _page(payload, rows, request)
    return FredParsedPage(
        request.operation, tuple(normalized), count, offset, limit, completeness, shape
    )
