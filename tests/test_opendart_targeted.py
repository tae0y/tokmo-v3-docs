from datetime import UTC, datetime

import pytest

from tokmo.facts.opendart import OpenDartCorpMapping
from tokmo.facts.opendart_targeted import (
    OpenDartTargetError,
    resolve_kr_opendart_targets,
    resolve_kr_relevance,
)
from tokmo.values.holdings import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)


def holding(symbol, market):
    return TossHoldingFact(
        symbol=symbol,
        quantity="1",
        avg_price="1",
        avg_price_field="averagePrice",
        currency="KRW" if market == "KR" else "USD",
        market_raw=market,
        market_field="market",
        name=None,
        asset_type=None,
    )


def snapshot(*accounts):
    return CompleteHoldingsSnapshot(
        snapshot_id="fixture",
        source="toss-holdings-v1",
        captured_at=NOW,
        discovered_account_count=len(accounts),
        accounts=tuple(
            HoldingsSnapshotAccount(i + 1, tuple(items))
            for i, items in enumerate(accounts)
        ),
        content_hash="a" * 64,
    )


def mapping(symbol, corp_code):
    return OpenDartCorpMapping(
        stock_code=symbol,
        corp_code=corp_code,
        corp_name="fixture",
        modify_date=NOW.date(),
    )


def test_dynamic_union_provenance_dedupe_order_and_us_exclusion():
    state = snapshot(
        [holding("222222", "KR"), holding("111111", "KR"), holding("USONLY", "US")],
        [holding("222222", "KR")],
    )
    result = resolve_kr_opendart_targets(
        snapshot=state,
        watchlist_symbols=frozenset({"222222", "333333", "444444"}),
        mappings=(
            mapping("333333", "00000003"),
            mapping("111111", "00000001"),
            mapping("222222", "00000002"),
        ),
    )
    assert [(row.symbol, row.corp_code, row.provenance) for row in result.targets] == [
        ("111111", "00000001", "HOLDING"),
        ("222222", "00000002", "HOLDING_AND_WATCHLIST"),
        ("333333", "00000003", "WATCHLIST"),
    ]
    assert result.unmapped == ("444444",)
    assert "USONLY" not in repr(result)


def test_unmapped_etf_or_nonissuer_is_explicit_and_network_zero():
    result = resolve_kr_opendart_targets(
        snapshot=snapshot([holding("ETF001", "KR")]),
        watchlist_symbols=frozenset(),
        mappings=(),
    )
    assert result.targets == ()
    assert result.unmapped == ("ETF001",)


@pytest.mark.parametrize(
    "mappings",
    [
        (mapping("111111", "00000001"), mapping("111111", "00000001")),
        (mapping("111111", "00000001"), mapping("111111", "00000002")),
    ],
)
def test_duplicate_or_conflicting_mapping_refuses(mappings):
    with pytest.raises(OpenDartTargetError, match="duplicate_mapping"):
        resolve_kr_opendart_targets(
            snapshot=snapshot([holding("111111", "KR")]),
            watchlist_symbols=frozenset(),
            mappings=mappings,
        )


def test_relevance_unions_snapshots_with_provenance_and_excludes_us():
    toss = snapshot([holding("222222", "KR"), holding("USONLY", "US")])
    pension = snapshot([holding("111111", "KR")])
    resolution = resolve_kr_relevance(
        snapshots=(toss, pension),
        watchlist_symbols=frozenset({"222222", "333333"}),
    )
    assert resolution.members == (
        ("111111", "HOLDING"),
        ("222222", "HOLDING_AND_WATCHLIST"),
        ("333333", "WATCHLIST"),
    )
    assert resolution.provenance_for("111111") == "HOLDING"
    assert resolution.provenance_for("222222") == "HOLDING_AND_WATCHLIST"
    assert resolution.provenance_for("333333") == "WATCHLIST"
    assert resolution.provenance_for("USONLY") is None
    assert resolution.provenance_for("999999") is None


def test_relevance_with_no_sources_is_empty_and_fails_closed_on_bad_arguments():
    empty = resolve_kr_relevance(snapshots=(), watchlist_symbols=frozenset())
    assert empty.members == ()
    with pytest.raises(OpenDartTargetError, match="invalid_watchlist"):
        resolve_kr_relevance(snapshots=(), watchlist_symbols={"005930"})
    with pytest.raises(OpenDartTargetError, match="complete_holdings_required"):
        resolve_kr_relevance(snapshots=(object(),), watchlist_symbols=frozenset())


def test_source_has_no_portfolio_literals_or_side_effect_surfaces():
    from pathlib import Path

    source = (
        (Path(__file__).parents[1] / "src/tokmo/facts/opendart_targeted.py")
        .read_text()
        .lower()
    )
    for forbidden in (
        "000660",
        "005380",
        "urllib",
        "requests",
        "os.environ",
        "datetime.now",
        "telegram",
        "scheduler",
        "sqlite",
        "open(",
        "read_text",
        "write_text",
    ):
        assert forbidden not in source
