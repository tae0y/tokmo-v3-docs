import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from tokmo.facts.etf_issuer import constituent, parse_portfolio
from tokmo.programs.etf_portfolio_context import (
    compact_components,
    dated_nav_context,
    portfolio_exposure,
)

NOW = datetime.fromisoformat("2026-09-14T10:02:00+09:00")


def save(root, name, data):
    folder = root / "kis"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(json.dumps(data))


def navs(root, *, day="2026-09-14", price="101", tick_price="101"):
    common = dict(symbol="069500", received_at=NOW.isoformat())
    save(
        root,
        "nav_daily-069500.json",
        dict(
            common,
            operation="nav_daily",
            rows=[dict(source_date=day, nav="100", price=price)],
        ),
    )
    save(
        root,
        "nav-069500.json",
        dict(
            common,
            operation="nav",
            rows=[dict(source_time="10:01:00", nav="100", price=tick_price)],
        ),
    )


def test_date_and_time_join_only_on_matching_source_observation(tmp_path):
    navs(tmp_path)
    n = dated_nav_context(tmp_path, "069500", at=NOW)
    assert n["source_at"] == "2026-09-14T10:01:00+09:00"
    assert n["current_gap_pct"] == "1.00" and n["usable_for_price_decision"]
    navs(tmp_path, tick_price="102")
    n = dated_nav_context(tmp_path, "069500", at=NOW)
    assert n["source_at"] is None and n["usable_for_valuation"]
    assert not n["usable_for_price_decision"]


def test_old_and_future_nav_never_promoted_to_today(tmp_path):
    navs(tmp_path, day="2026-09-11")
    n = dated_nav_context(tmp_path, "069500", at=NOW)
    assert n["source_date"] == "2026-09-11" and not n["usable_for_price_decision"]
    navs(tmp_path, day="2026-09-15")
    assert dated_nav_context(tmp_path, "069500", at=NOW) is None
    navs(tmp_path)
    assert dated_nav_context(tmp_path, "069500", at=NOW - timedelta(minutes=1)) is None


def test_kodex_cash_is_in_denominator_and_partial_list_rejected():
    d = dict(
        pdf=dict(
            gijunYMD="20260914",
            totalCnt=2,
            list=[
                dict(itmNo="005930", secNm="삼성전자", ratio="100", evalA="9500"),
                dict(itmNo="KRD010010001", secNm="원화예금", ratio=None, evalA="500"),
            ],
        )
    )
    f = parse_portfolio(
        "KODEX", json.dumps(d), symbol="069500", received_at=NOW, url="official"
    )
    assert f["rows"][0]["weight_pct"] == "95.000000"
    assert f["rows"][1]["weight_pct"] == "5.000000"
    d["pdf"]["totalCnt"] = 200
    with pytest.raises(ValueError, match="incomplete"):
        parse_portfolio(
            "KODEX", json.dumps(d), symbol="069500", received_at=NOW, url="official"
        )


def test_plus_foreign_bonds_retained_and_truncated_pages_rejected():
    d = dict(
        last=True,
        totalElements=2,
        content=[
            dict(
                wkdate="20260914",
                krJmCd="US46436F1030",
                jmNm="iShares Gold Trust Micro",
                ratio="50",
            ),
            dict(wkdate="20260914", krJmCd="KR103501GF63", jmNm="국고채권", ratio="50"),
        ],
    )
    f = parse_portfolio(
        "PLUS", json.dumps(d), symbol="0138Y0", received_at=NOW, url="official"
    )
    assert [r["asset_kind"] for r in f["rows"]] == ["fund", "bond"]
    assert f["whole_fund_weights_usable"]
    d["last"] = False
    with pytest.raises(ValueError, match="pagination"):
        parse_portfolio(
            "PLUS", json.dumps(d), symbol="0138Y0", received_at=NOW, url="official"
        )


def test_cash_bond_and_local_isin_identity():
    assert constituent("KR7000660001", "SK하이닉스", "25")["key"] == "KR:000660"
    assert constituent("KR103501GF63", "국고채권", "50")["asset_kind"] == "bond"
    assert constituent("NVDA US", "NVIDIA CORP", "5")["key"] == "US:NVDA"


def test_full_rows_calculated_but_only_relevant_subset_goes_to_model():
    rows = [
        dict(
            key="KR:" + str(i).zfill(6),
            code=str(i).zfill(6),
            name="주식" + str(i),
            weight_pct="0.2",
            asset_kind="stock",
        )
        for i in range(500)
    ]
    result = compact_components(
        dict(rows=rows, whole_fund_weights_usable=True), keys={"KR:000499"}
    )
    assert len(result["rows"]) == 11 and result["full_row_count"] == 500
    assert result["asset_weights_pct"]["stock"] == "100.00"
    assert result["whole_fund_weights_usable"] and result["rows_truncated"]


def test_foreign_constituent_keeps_its_market_in_indirect_evidence(tmp_path):
    from tokmo.programs.etf_context import attach_quant_etf_context

    save(
        tmp_path,
        "issuer-components-490090.json",
        dict(
            schema="etf-issuer-v1",
            symbol="490090",
            source="TIGER",
            received_at=NOW.isoformat(),
            source_date="2026-09-14",
            coverage="COMPLETE",
            whole_fund_weights_usable=True,
            rows=[constituent("NVDA US", "NVIDIA CORP", "100")],
        ),
    )
    items = [
        dict(symbol="490090", market="KR", evidence=[]),
        dict(symbol="NVDA", market="US", evidence=[]),
    ]
    attach_quant_etf_context(tmp_path, items, at=NOW, held_keys={"KR:490090"})
    evidence = next(e for e in items[1]["evidence"] if e["kind"] == "etf_indirect")
    assert evidence["metadata"]["component"] == "US:NVDA"


def test_direct_and_etf_exposure_uses_quantity_weight_and_fx():
    fund = NS(
        key="KR:0162Z0",
        name="혼합",
        price=Decimal(100),
        currency="KRW",
        evidence=[
            NS(
                kind="etf_components",
                metadata=dict(
                    whole_fund_weights_usable=True,
                    source_date="2026-09-14",
                    rows=[dict(key="KR:000660", name="SK하이닉스", weight_pct="25")],
                ),
            )
        ],
    )
    stock = NS(
        key="KR:000660",
        name="SK하이닉스",
        price=Decimal(100),
        currency="KRW",
        evidence=[],
    )
    foreign = NS(key="US:V", name="V", price=Decimal(1), currency="USD", evidence=[])
    s = NS(
        instruments=[fund, stock, foreign],
        positions=[
            NS(key=k, quantity=Decimal(q), scope="personal")
            for k, q in [("KR:0162Z0", 4), ("KR:000660", 1), ("US:V", 1)]
        ],
        fx={"rate": Decimal(1000)},
    )
    result = portfolio_exposure(s, scope="personal", focus_key="KR:000660")
    r = next(r for r in result["overlaps"] if r["key"] == "KR:000660")
    assert (
        r["direct_krw"] == "100"
        and r["via_etf_krw"] == "100"
        and r["combined_krw"] == "200"
    )
    assert result["priced_holdings_krw"] == "1500"
    s.positions.append(NS(key="KR:000660", quantity=Decimal(1), scope="pension"))
    combined = portfolio_exposure(
        s, scope=None, focus_key="KR:0162Z0", related_keys={"KR:000660"}
    )
    row = next(r for r in combined["overlaps"] if r["key"] == "KR:000660")
    assert row["combined_krw"] == "300" and combined["priced_holdings_krw"] == "1600"


def test_etf_amounts_are_validated_in_their_own_currency():
    from tokmo.programs.quant_hypotheses import unit_values

    packet = dict(
        currency="USD",
        key="US:NVDA",
        price="100",
        etf_exposure=dict(
            overlaps=[dict(combined_krw="170000", key="KR:005930")],
            source_date="20260914",
        ),
    )
    values = unit_values(packet)
    assert Decimal(170000) in values["원"] and Decimal(170000) not in values["달러"]
    assert Decimal(20260914) not in values["원"] and Decimal(5930) not in values["원"]
    packet.update(
        currency="KRW",
        key="KR:0162Z0",
        etf_context=dict(nav=dict(nav="13261.83", provider_price="13220")),
    )
    assert {Decimal("13261.83"), Decimal(13220)} <= unit_values(packet)["원"]


def test_missing_issuer_does_not_block_nav_or_starve_other_etfs(tmp_path, monkeypatch):
    from tokmo.programs import etf_portfolio_refresh as mod

    symbols = [str(x).zfill(6) for x in range(8)]
    monkeypatch.setattr(mod, "held_etfs", lambda *a, **k: symbols)
    calls = []

    class Client:
        requests = 0
        auth_requests = 0

        def fetch(self, operation, symbol):
            calls.append((operation, symbol))
            self.requests += 1
            if operation == "nav_daily":
                save(
                    tmp_path,
                    "nav_daily-" + symbol + ".json",
                    dict(source_date="2026-09-14", received_at=NOW.isoformat()),
                )

    def fail(symbol, **kwargs):
        raise ValueError("issuer unavailable")

    config = dict(credential_project_root=str(tmp_path))
    for _ in range(3):
        result = mod.refresh_portfolios(
            tmp_path,
            config,
            at=NOW,
            client_factory=lambda **k: Client(),
            portfolio_fetch=fail,
        )
        assert result["requests"] <= 6 and result["status"] == "DEGRADED"
    assert {s for op, s in calls} == set(symbols)
    before = len(calls)
    mod.refresh_portfolios(
        tmp_path,
        config,
        at=NOW,
        client_factory=lambda **k: Client(),
        portfolio_fetch=fail,
    )
    assert len(calls) == before
