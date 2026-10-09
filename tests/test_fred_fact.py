import json
from datetime import date

import pytest

from tokmo.facts.fred import build_query_pairs, parse_fred_response
from tokmo.values.fred_macro import FredOperation, FredRequest, PageCompleteness


def _body(value):
    return json.dumps(value, separators=(",", ":")).encode()


def test_five_closed_builders_and_provider_echo_parsers():
    requests = (
        FredRequest(FredOperation.SERIES, series_id="PAYEMS"),
        FredRequest(
            FredOperation.SERIES_OBSERVATIONS,
            series_id="PAYEMS",
            output_type=4,
            realtime_start=date(2026, 1, 1),
            realtime_end=date(2026, 1, 2),
            observation_start=date(2025, 12, 1),
            observation_end=date(2025, 12, 31),
            limit=2,
        ),
        FredRequest(FredOperation.SERIES_RELEASE, series_id="PAYEMS"),
        FredRequest(FredOperation.RELEASE_DATES, release_id=50, limit=2),
        FredRequest(
            FredOperation.SERIES_VINTAGE_DATES,
            series_id="PAYEMS",
            realtime_start=date(2026, 1, 1),
            realtime_end=date(2026, 1, 2),
            limit=2,
        ),
    )
    assert tuple(request.operation for request in requests) == tuple(FredOperation)
    for request in requests:
        pairs = build_query_pairs(request)
        assert pairs[-1] == ("file_type", "json")
        assert "api_key" not in dict(pairs)

    series = parse_fred_response(
        requests[0],
        _body(
            {
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "seriess": [
                    {
                        "id": "PAYEMS",
                        "realtime_start": "2026-01-01",
                        "realtime_end": "2026-01-02",
                        "title": "All Employees",
                        "observation_start": "1939-01-01",
                        "observation_end": "2025-12-01",
                        "frequency": "Monthly",
                        "frequency_short": "M",
                        "units": "Thousands of Persons",
                        "units_short": "Thous. of Persons",
                        "seasonal_adjustment": "Seasonally Adjusted",
                        "seasonal_adjustment_short": "SA",
                        "last_updated": "2026-01-01 00:00:00-06",
                        "popularity": 90,
                    }
                ],
            }
        ),
    )
    assert series.completeness is PageCompleteness.COMPLETE
    assert series.rows[0]["identity_provenance"] == "PROVIDER"

    observations = parse_fred_response(
        requests[1],
        _body(
            {
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "observation_start": "2025-12-01",
                "observation_end": "2025-12-31",
                "units": "lin",
                "output_type": 4,
                "file_type": "json",
                "order_by": "observation_date",
                "sort_order": "asc",
                "count": 3,
                "offset": 0,
                "limit": 2,
                "observations": [
                    {
                        "realtime_start": "2026-01-01",
                        "realtime_end": "2026-01-02",
                        "date": "2025-12-01",
                        "value": "159000.0",
                    }
                ],
            }
        ),
    )
    assert observations.completeness is PageCompleteness.TRUNCATED
    assert observations.rows[0]["classification"] == "INITIAL"
    assert observations.rows[0]["series_identity_provenance"] == "REQUEST"

    release = parse_fred_response(
        requests[2],
        _body(
            {
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "releases": [
                    {
                        "id": 50,
                        "realtime_start": "2026-01-01",
                        "realtime_end": "2026-01-02",
                        "name": "Employment Situation",
                        "press_release": True,
                    }
                ],
            }
        ),
    )
    assert release.rows[0]["series_identity_provenance"] == "REQUEST"

    dates = parse_fred_response(
        requests[3],
        _body(
            {
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "order_by": "release_date",
                "sort_order": "desc",
                "count": 2,
                "offset": 0,
                "limit": 2,
                "release_dates": [{"release_id": 50, "date": "2026-01-02"}],
            }
        ),
    )
    assert dates.rows[0]["release_identity_provenance"] == "PROVIDER"

    vintages = parse_fred_response(
        requests[4],
        _body(
            {
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "order_by": "vintage_date",
                "sort_order": "asc",
                "count": 1,
                "offset": 0,
                "limit": 2,
                "vintage_dates": ["2026-01-01"],
            }
        ),
    )
    assert vintages.completeness is PageCompleteness.COMPLETE

    wrong = json.loads(
        _body(
            {
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "seriess": [dict(series.rows[0], id="WRONG")],
            }
        )
    )
    wrong["seriess"][0].pop("identity_provenance")
    with pytest.raises(ValueError, match="series_echo_mismatch"):
        parse_fred_response(requests[0], _body(wrong))


def test_wrong_release_and_correct_looking_wrong_vintage_are_rejected():
    release_request = FredRequest(FredOperation.RELEASE_DATES, release_id=50, limit=1)
    with pytest.raises(ValueError, match="release_echo_mismatch"):
        parse_fred_response(
            release_request,
            _body(
                {
                    "realtime_start": "2026-01-01",
                    "realtime_end": "2026-01-02",
                    "order_by": "release_date",
                    "sort_order": "desc",
                    "count": 1,
                    "offset": 0,
                    "limit": 1,
                    "release_dates": [{"release_id": 51, "date": "2026-01-02"}],
                }
            ),
        )
    vintage_request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS,
        series_id="PAYEMS",
        output_type=2,
        vintage_dates=(date(2026, 1, 1),),
        limit=1,
    )
    payload = {
        "realtime_start": "2026-01-01",
        "realtime_end": "2026-01-02",
        "observation_start": "2025-12-01",
        "observation_end": "2025-12-31",
        "units": "lin",
        "output_type": 2,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": 1,
        "offset": 0,
        "limit": 1,
        "observations": [{"date": "2025-12-01", "PAYEMS_20260102": "1"}],
    }
    with pytest.raises(ValueError, match="unrequested_vintage"):
        parse_fred_response(vintage_request, _body(payload))


def test_output_type_none_is_standard_and_release_dates_are_unique():
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS, series_id="PAYEMS", limit=1
    )
    payload = {
        "realtime_start": "2026-01-01",
        "realtime_end": "2026-01-02",
        "observation_start": "2025-12-01",
        "observation_end": "2025-12-31",
        "units": "lin",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": 1,
        "offset": 0,
        "limit": 1,
        "observations": [
            {
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "date": "2025-12-01",
                "value": "1",
            }
        ],
    }
    assert parse_fred_response(request, _body(payload)).rows[0]["value"] == 1
    release_request = FredRequest(FredOperation.RELEASE_DATES, release_id=50, limit=2)
    duplicate = {
        "realtime_start": "2026-01-01",
        "realtime_end": "2026-01-02",
        "order_by": "release_date",
        "sort_order": "desc",
        "count": 2,
        "offset": 0,
        "limit": 2,
        "release_dates": [
            {"release_id": 50, "date": "2026-01-02"},
            {"release_id": 50, "date": "2026-01-02"},
        ],
    }
    with pytest.raises(ValueError, match="unordered_or_duplicate_rows"):
        parse_fred_response(release_request, _body(duplicate))
