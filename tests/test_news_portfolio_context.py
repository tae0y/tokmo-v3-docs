import json
import sqlite3
from datetime import timedelta

from test_news_decision_engine import (
    Engine,
    NewsDecisionStore,
    decision,
    observation,
    screen,
)

from tokmo.programs.news_portfolio_context import (
    price_lines,
    read_news_portfolio,
    read_quotes,
)
from tokmo.records.news_decision_store import timestamp

AT = timestamp("2026-09-08T13:30:00+09:00")


def setup(root):
    (root / "stores").mkdir(parents=True)
    (root / "ops/quant-inputs").mkdir(parents=True)
    (root / "stores/current_holdings.json").write_text(
        json.dumps(
            dict(
                captured_at=AT.isoformat(),
                accounts=[
                    dict(
                        account_scope="toss-acct-test",
                        holdings=[
                            dict(
                                symbol="329180",
                                quantity="2",
                                avg_price="100",
                                currency="KRW",
                                market_raw="KR",
                            )
                        ],
                    ),
                    dict(
                        account_scope="pension-excel-test",
                        holdings=[
                            dict(
                                symbol="161510",
                                quantity="7",
                                avg_price="90",
                                currency="KRW",
                                market_raw="KR",
                            )
                        ],
                    ),
                ],
            )
        )
    )
    (root / "ops/quant-inputs/latest.json").write_text(
        json.dumps(
            dict(
                captured_at=(AT - timedelta(days=1)).isoformat(),
                instruments=[
                    dict(
                        symbol="329180", market="KR", name="HD현대중공업", kind="stock"
                    ),
                    dict(
                        symbol="161510", market="KR", name="PLUS 고배당주", kind="etf"
                    ),
                ],
            )
        )
    )
    c = sqlite3.connect(root / "stores/pension_registry.sqlite3")
    c.executescript(
        "CREATE TABLE pension_revisions(revision_id,source_as_of); CREATE TABLE pension_active_pointer(active_revision_id);"
        "INSERT INTO pension_revisions VALUES('r','2026-08-13T00:00:00+09:00'); INSERT INTO pension_active_pointer VALUES('r');"
    )
    c.close()


def test_names_types_and_file_based_pension_survive_time_without_new_balances(tmp_path):
    setup(tmp_path)
    before = (tmp_path / "stores/current_holdings.json").read_bytes()
    context = read_news_portfolio(tmp_path, tmp_path, at=AT, followup=False)
    company, pension = context["holdings"]
    assert company["name"] == "HD현대중공업" and company["quantity"] == "2"
    assert pension["name"] == "PLUS 고배당주" and pension["kind"] == "etf"
    assert pension["quantity"] == "7" and pension["avg_price"] == "90"
    assert pension["observed_at"] == "2026-08-13T00:00:00+09:00"
    later = read_news_portfolio(
        tmp_path, tmp_path, at=AT + timedelta(days=3), followup=False
    )
    assert later["portfolio_status"] == "STALE"
    assert later["holdings"][1] == pension  # No five-minute renewal of the user file.
    assert (tmp_path / "stores/current_holdings.json").read_bytes() == before


def test_missing_or_future_catalog_does_not_guess_a_code_or_block_context(tmp_path):
    setup(tmp_path)
    path = tmp_path / "ops/quant-inputs/latest.json"
    data = json.loads(path.read_text())
    data["captured_at"] = (AT + timedelta(days=1)).isoformat()
    path.write_text(json.dumps(data))
    context = read_news_portfolio(tmp_path, tmp_path, at=AT, followup=False)
    assert context["holdings"][0]["name"] is None
    assert context["holdings"][1]["kind"] == "unknown"
    path.write_text("{broken")
    assert (
        len(read_news_portfolio(tmp_path, tmp_path, at=AT, followup=False)["holdings"])
        == 2
    )


def test_optional_quote_reader_excludes_future_versions_and_does_not_create_missing_store(
    tmp_path,
):
    path = tmp_path / "quotes.sqlite3"
    assert read_quotes(path, at=AT) == {} and not path.exists()
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE latest_quotes(source,symbol,last_price,currency_raw,provider_timestamp,fetched_at)"
    )
    c.executemany(
        "INSERT INTO latest_quotes VALUES(?,?,?,?,?,?)",
        [
            (
                "toss-current-price-v1",
                "A",
                "100",
                "USD",
                AT.isoformat(),
                AT.isoformat(),
            ),
            (
                "toss-current-price-v1",
                "B",
                "200",
                "USD",
                (AT + timedelta(seconds=1)).isoformat(),
                AT.isoformat(),
            ),
            (
                "toss-current-price-v1",
                "C",
                "NaN",
                "USD",
                AT.isoformat(),
                AT.isoformat(),
            ),
        ],
    )
    c.commit()
    c.close()
    assert list(read_quotes(path, at=AT)) == ["A"]


def price_context():
    quote_at = "2026-09-05T05:00:00+09:00"  # NY Sep 4. KST calendar date must not choose Sep 4 as previous.
    return dict(
        instruments=[
            dict(key="US:IREN", symbol="IREN", market="US", name="아이렌", kind="stock")
        ],
        holdings=[
            dict(
                key="US:IREN",
                symbol="IREN",
                account="a",
                scope="personal",
                currency="USD",
                quantity="2",
                avg_price="30",
            ),
            dict(
                key="US:B",
                symbol="B",
                account="a",
                scope="personal",
                currency="USD",
                quantity="1",
                avg_price="40",
            ),
            dict(
                key="KR:C",
                symbol="C",
                account="a",
                scope="personal",
                currency="KRW",
                quantity="1",
                avg_price="10000",
            ),
        ],
        _market_context=dict(
            calendar=dict(
                days=[
                    dict(
                        market="US",
                        trade_date="2026-09-04",
                        sessions=[
                            dict(
                                kind="regularMarket",
                                start="2026-09-04T09:30:00-04:00",
                                end="2026-09-04T16:00:00-04:00",
                            )
                        ],
                    )
                ],
                history={"US": {"2026-09-03": True}},
            ),
            captured_at="2026-09-05T06:00:00+09:00",
            quotes={
                "IREN": dict(
                    price="45", currency="USD", price_at=quote_at, age_seconds=100
                ),
                "B": dict(
                    price="90", currency="USD", price_at=quote_at, age_seconds=100
                ),
            },
            instruments=[
                dict(
                    market="US",
                    symbol="IREN",
                    bars=[
                        dict(
                            date="2026-09-03",
                            close="40",
                            available_at="2026-09-04T05:00:00+09:00",
                        ),
                        dict(
                            date="2026-09-04",
                            close="44.68",
                            available_at="2026-09-05T05:00:00+09:00",
                        ),
                    ],
                )
            ],
        ),
    )


def test_price_change_personal_profit_and_weight_have_distinct_bases():
    lines = price_lines(["US:IREN"], price_context(), at=AT)
    text = "\n".join(lines)
    assert "IREN(아이렌)\n$45.00 / +12.5%\n09-05 05:00 기준\n보유" in text
    assert "등락은" not in text and "미갱신" not in text
    assert "보유 2주 · 평단가 $30.00\n수익 $30.00 (+50.0%)" in text
    assert "미국 보유종목 중 50.0%" in text
    assert "전체 계좌" not in text and "오늘" not in text


def test_missing_peer_price_omits_weight_without_losing_the_holding_price():
    context = price_context()
    del context["_market_context"]["quotes"]["B"]
    text = "\n".join(price_lines(["US:IREN"], context, at=AT))
    assert "수익 $30.00 (+50.0%)" in text and "보유종목 중" not in text
    assert price_lines(["KR:UNVERIFIED"], context, at=AT) == []


def test_prices_do_not_enter_model_context_and_urgent_alert_needs_no_price(tmp_path):
    calls = []

    def analyze(request):
        calls.append(request)
        assert "_market_context" not in request["context"]
        return decision(subject_keys=["US:IREN"])

    store = NewsDecisionStore(tmp_path / "news.db")
    context = price_context()
    context["_market_context"]["quotes"] = {}
    engine = Engine(store, screen, analyze, context_reader=lambda _: context)
    result = engine.tick([observation(at=AT)], AT)
    assert len(result["alerts"]) == 1 and result["alerts"][0]["urgency"] == "IMMEDIATE"
    body = result["alerts"][0]["body"]
    assert "왜 지금:" not in body and "판단 시각:" not in body
    assert "수집: 2026-09-08 13:30" in body
    assert "_market_context" not in store.events()[0]["context_snapshot"]
    store.close()


def test_selected_news_appends_computed_quote_without_sending_prices_to_model(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")

    def analyze(request):
        assert "_market_context" not in request["context"]
        return decision(subject_keys=["US:IREN"])

    engine = Engine(store, screen, analyze, context_reader=lambda _: price_context())
    result = engine.tick([observation(at=AT)], AT)
    body = result["alerts"][0]["body"]
    assert "보유 2주 · 평단가 $30.00\n수익 $30.00 (+50.0%)" in body
    assert "$45.00 / +12.5%" in body and "등락은" not in body
    store.close()
