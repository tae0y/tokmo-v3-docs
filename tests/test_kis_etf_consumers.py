import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tokmo.programs.etf_context import (
    attach_quant_etf_context,
    news_etf_context,
    read_etf_context,
)

NOW = datetime(2026, 9, 11, 11, 20, tzinfo=UTC)


def saved(root, symbol="0138Y0", *, at=NOW):
    folder = root / "kis"
    folder.mkdir(exist_ok=True)
    base = dict(
        schema="kis-etf-v1",
        source="KIS",
        symbol=symbol,
        market="KR",
        currency="KRW",
        received_at=at.isoformat(),
        source_date=None,
        freshness="UNVERIFIED",
        status="OK",
        more_available=False,
    )
    components = dict(
        base,
        operation="components",
        coverage="PARTIAL",
        weight_basis="PROVIDER_UNVERIFIED",
        reported_component_count="14",
        returned_component_count=3,
        returned_weight_sum_pct="100",
        rows=[
            dict(
                component_code="298340",
                component_name="PLUS 국채선물3년",
                component_market=None,
                provider_weight_pct="94.67",
                valuation_amount="100",
            ),
            dict(
                component_code="114100",
                component_name="RISE 국고채3년",
                component_market=None,
                provider_weight_pct="2.68",
                valuation_amount="3",
            ),
            dict(
                component_code="114820",
                component_name="TIGER 국채3년",
                component_market=None,
                provider_weight_pct="2.65",
                valuation_amount="3",
            ),
        ],
    )
    nav = dict(
        base,
        operation="nav",
        rows=[
            dict(
                source_time="15:30:00",
                nav="9616.86",
                price="9635",
                provider_gap_pct="0.19",
                computed_gap_pct="0.19",
            )
        ],
    )
    for operation, data in [("components", components), ("nav", nav)]:
        (folder / f"{operation}-{symbol}.json").write_text(json.dumps(data))
    return components, nav


def test_partial_weights_never_become_whole_fund_or_live_gap(tmp_path):
    saved(tmp_path)
    context = read_etf_context(tmp_path, "0138Y0", at=NOW)
    assert context["components"]["coverage"] == "PARTIAL"
    assert context["components"]["whole_fund_weights_usable"] is False
    assert all(
        "provider_weight_pct" not in row for row in context["components"]["rows"]
    )
    assert context["nav"]["nav"] == "9616.86"
    assert context["nav"]["source_date"] is None
    assert context["nav"]["current_gap_pct"] is None
    assert context["nav"]["usable_for_price_decision"] is False


def test_future_malformed_stale_and_missing_are_distinct(tmp_path):
    assert (
        read_etf_context(tmp_path, "0138Y0", at=NOW)["components"]["status"]
        == "MISSING"
    )
    assert not (tmp_path / "kis").exists()
    saved(tmp_path, at=NOW + timedelta(seconds=1))
    assert (
        read_etf_context(tmp_path, "0138Y0", at=NOW)["nav"]["status"]
        == "UNAVAILABLE_AT_THIS_TIME"
    )
    saved(tmp_path, at=NOW - timedelta(days=2))
    value = read_etf_context(tmp_path, "0138Y0", at=NOW)
    assert value["components"]["status"] == "STALE"
    assert value["components"]["rows"] == []
    (tmp_path / "kis/nav-0138Y0.json").write_text("{bad")
    assert read_etf_context(tmp_path, "0138Y0", at=NOW)["nav"]["status"] == "INVALID"


def test_news_survives_compaction_without_price_or_observation_churn(tmp_path):
    from tokmo.programs.news_context_memory import compact_portfolio

    saved(tmp_path)
    one = news_etf_context(read_etf_context(tmp_path, "0138Y0", at=NOW))
    saved(tmp_path, at=NOW + timedelta(minutes=5))
    two = news_etf_context(
        read_etf_context(tmp_path, "0138Y0", at=NOW + timedelta(minutes=5))
    )
    assert one == two
    assert "9616.86" not in json.dumps(one)
    assert compact_portfolio({"etf_context": [one]})["etf_context"] == [one]


def test_quant_uses_metadata_and_exact_known_component_identity(tmp_path):
    saved(tmp_path)
    instruments = [
        dict(
            symbol="0138Y0",
            market="KR",
            kind="etf",
            name="PLUS 금채권혼합",
            evidence=[],
        ),
        dict(
            symbol="298340",
            market="KR",
            kind="etf",
            name="PLUS 국채선물3년",
            evidence=[],
        ),
        dict(
            symbol="298340",
            market="US",
            kind="stock",
            name="PLUS 국채선물3년",
            evidence=[],
        ),
    ]
    attach_quant_etf_context(tmp_path, instruments, at=NOW, held_keys={"KR:0138Y0"})
    evidence = instruments[0]["evidence"]
    assert {e["kind"] for e in evidence} == {"etf_components", "etf_nav"}
    assert all(e["available_at"] == NOW.isoformat() for e in evidence)
    assert any(e["kind"] == "etf_indirect" for e in instruments[1]["evidence"])
    assert instruments[2]["evidence"] == []
    assert not any("weight_pct" in json.dumps(e) for e in evidence)
    attach_quant_etf_context(tmp_path, instruments, at=NOW, held_keys={"KR:0138Y0"})
    assert len(instruments[0]["evidence"]) == 2


def test_news_real_input_entry_keeps_etf_relation(tmp_path):
    from tokmo.programs.news_portfolio_context import read_news_portfolio

    saved(tmp_path)
    (tmp_path / "stores").mkdir()
    (tmp_path / "stores/current_holdings.json").write_text(
        json.dumps(
            dict(
                captured_at=NOW.isoformat(),
                accounts=[
                    dict(
                        account_scope="pension-excel-test",
                        holdings=[
                            dict(
                                symbol="0138Y0",
                                quantity="1",
                                market_raw="KR",
                                currency="KRW",
                            )
                        ],
                    )
                ],
            )
        )
    )
    context = read_news_portfolio(tmp_path, tmp_path, at=NOW, followup=False)
    assert context["etf_context"][0]["symbol"] == "0138Y0"
    assert len(context["etf_context"][0]["components"]["rows"]) == 3
    assert "nav" in context["_etf_context"][0]


def test_quant_real_loader_to_model_packet(tmp_path, monkeypatch):

    from tokmo.programs import quant_decision_inputs as inputs
    from tokmo.programs.quant_decision_analysis import make_packet
    from tokmo.values.quant_decisions import parse_snapshot

    saved(tmp_path)
    monkeypatch.setattr(
        inputs.CurrentHoldingsStore,
        "read",
        lambda self: SimpleNamespace(captured_at=NOW, accounts=[]),
    )
    result = inputs.load_local_snapshot(
        tmp_path,
        tmp_path,
        as_of=NOW,
        supplement={
            "instruments": [
                dict(
                    symbol="0138Y0",
                    market="KR",
                    name="PLUS 금채권혼합",
                    kind="etf",
                    price="9635",
                    price_at=NOW.isoformat(),
                )
            ]
        },
    )
    assert any(e["kind"] == "etf_nav" for e in result["instruments"][0]["evidence"])
    snapshot = parse_snapshot(result)
    packet = make_packet(
        snapshot,
        snapshot.instruments[0],
        account=None,
        scope="retirement",
        scenario="new",
    )
    assert packet["etf_context"]["nav"]["usable_for_price_decision"] is False
    assert packet["etf_context"]["components"]["reported_component_count"] == 14


@pytest.mark.parametrize("symbol", ["../token", "token", "US:0138Y0", "ABCDEF/.."])
def test_bad_identity_never_reads_private_token(tmp_path, symbol):
    with pytest.raises(ValueError):
        read_etf_context(tmp_path, symbol, at=NOW)


def test_consumer_does_not_trust_forged_complete_or_fresh_flags(tmp_path):
    saved(tmp_path)
    path = tmp_path / "kis/components-0138Y0.json"
    data = json.loads(path.read_text())
    data.update(coverage="COMPLETE", weight_basis="WHOLE_FUND", freshness="FRESH")
    path.write_text(json.dumps(data))
    context = read_etf_context(tmp_path, "0138Y0", at=NOW)
    assert context["components"]["coverage"] == "PARTIAL"
    assert context["components"]["whole_fund_weights_usable"] is False
    assert context["components"]["freshness"] == "UNVERIFIED"


def test_invalid_identity_and_symlink_never_expose_unrelated_json(tmp_path):
    saved(tmp_path)
    path = tmp_path / "kis/nav-0138Y0.json"
    path.unlink()
    private = tmp_path / "token.json"
    private.write_text('{"access_token":"secret"}')
    path.symlink_to(private)
    assert read_etf_context(tmp_path, "0138Y0", at=NOW)["nav"]["status"] == "INVALID"
    path.unlink()
    path.write_text(json.dumps(dict(symbol="OTHER", access_token="secret")))
    assert "secret" not in json.dumps(read_etf_context(tmp_path, "0138Y0", at=NOW))


def test_connected_etf_is_reference_when_not_in_file_based_holdings(
    tmp_path, monkeypatch
):
    from tokmo.programs import quant_decision_inputs as inputs
    from tokmo.programs.news_portfolio_context import read_news_portfolio

    saved(tmp_path)
    (tmp_path / "kis/integration.json").write_text(
        json.dumps(
            dict(enabled=True, symbols=["0138Y0"], credential_project_root="/private")
        )
    )
    monkeypatch.setattr(
        inputs.CurrentHoldingsStore,
        "read",
        lambda self: SimpleNamespace(captured_at=NOW, accounts=[]),
    )
    context = read_news_portfolio(tmp_path, tmp_path, at=NOW, followup=False)
    assert context["etf_context"][0]["holding_status"] == "NOT_CONFIRMED"
    data = inputs.load_local_snapshot(
        tmp_path, tmp_path, as_of=NOW, include_context=False
    )
    assert data["positions"] == []
    assert data["instruments"][0]["symbol"] == "0138Y0"
    assert data["instruments"][0]["price"] is None
    assert "보유 여부 미확인" in str(data["instruments"][0]["sources"])
