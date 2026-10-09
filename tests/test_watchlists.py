"""Watchlist registry and holdings-separated quote target contract."""

import json
from pathlib import Path

import pytest

from tokmo.values.session_gate import Market
from tokmo.watchlists import (
    WatchlistError,
    load_watchlists,
    validate_watchlist_file,
    write_watchlist_file,
)

NOW = "2026-08-03T15:00:00+00:00"


def payload(market, entries):
    return {
        "schema_version": 1,
        "market": market,
        "entries": entries,
    }


def entry(symbol, name):
    return {
        "symbol": symbol,
        "name": name,
        "status": "active",
        "added_at": NOW,
        "source": "user",
    }


def write(path: Path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_loads_active_market_registries_without_holdings_fields(tmp_path):
    write(tmp_path / "KR.json", payload("KR", [entry("005930", "삼성전자")]))
    write(tmp_path / "US.json", payload("US", [entry("V", "Visa Inc.")]))

    loaded = load_watchlists(tmp_path)

    assert loaded == {
        Market.KR: frozenset({"005930"}),
        Market.US: frozenset({"V"}),
    }
    text = (tmp_path / "US.json").read_text()
    for forbidden in ("quantity", "avg_price", "account_id", "current_price"):
        assert forbidden not in text


def test_unsorted_valid_entries_are_normalized_at_read_boundary(tmp_path):
    write(tmp_path / "KR.json", payload("KR", []))
    write(
        tmp_path / "US.json",
        payload(
            "US",
            [
                entry("V", "Visa Inc."),
                entry("NVDA", "NVIDIA Corporation"),
            ],
        ),
    )

    assert load_watchlists(tmp_path)[Market.US] == frozenset({"NVDA", "V"})
    assert [
        row["symbol"]
        for row in validate_watchlist_file(
            payload(
                "US",
                [
                    entry("V", "Visa Inc."),
                    entry("NVDA", "NVIDIA Corporation"),
                ],
            ),
            expected_market="US",
        )
    ] == ["NVDA", "V"]


def test_write_boundary_persists_canonical_symbol_order(tmp_path):
    path = tmp_path / "US.json"
    write_watchlist_file(
        path,
        payload(
            "US",
            [
                entry("V", "Visa Inc."),
                entry("NVDA", "NVIDIA Corporation"),
            ],
        ),
        expected_market="US",
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [row["symbol"] for row in saved["entries"]] == ["NVDA", "V"]
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_write_boundary_rejects_invalid_payload_without_replacing_existing(tmp_path):
    path = tmp_path / "US.json"
    path.write_text("preserved\n", encoding="utf-8")
    invalid = payload("US", [entry("V", "Visa"), entry("V", "Duplicate")])

    with pytest.raises(WatchlistError):
        write_watchlist_file(path, invalid, expected_market="US")

    assert path.read_text(encoding="utf-8") == "preserved\n"


def test_retired_entries_are_not_materialized(tmp_path):
    retired = entry("OLD", "Old Corp") | {
        "status": "retired",
        "retired_at": "2026-08-04T00:00:00+00:00",
        "retire_reason": "user removed",
    }
    write(tmp_path / "KR.json", payload("KR", []))
    write(tmp_path / "US.json", payload("US", [retired, entry("V", "Visa Inc.")]))
    assert load_watchlists(tmp_path)[Market.US] == frozenset({"V"})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p | {"unknown": True},
        lambda p: p | {"market": "EU"},
        lambda p: p | {"entries": [entry("V", "Visa"), entry("V", "Duplicate")]},
        lambda p: p | {"entries": [entry("bad symbol!", "Bad")]},
    ],
)
def test_validator_rejects_unknown_market_duplicate_and_bad_symbol(mutate):
    with pytest.raises(WatchlistError):
        validate_watchlist_file(
            mutate(payload("US", [entry("V", "Visa Inc.")])), expected_market="US"
        )


def test_missing_or_malformed_current_registry_fails_closed(tmp_path):
    write(tmp_path / "KR.json", payload("KR", []))
    (tmp_path / "US.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(WatchlistError):
        load_watchlists(tmp_path)
