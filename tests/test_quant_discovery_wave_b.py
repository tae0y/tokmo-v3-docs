import ast
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokmo.values import MISSING
from tokmo.values.current_holdings import (
    CURRENT_HOLDINGS_SCHEMA_VERSION, AccountHoldingsV1, AccountScopeIdentityV1,
    CurrentHoldingsV1, HoldingItemV1,
)
from tokmo.values.pension_holdings import (
    PENSION_HOLDINGS_SCHEMA_VERSION, PENSION_PARSER_VERSION,
    PensionHoldingsRevisionV1,
)
from tokmo.values.quant_discovery import (
    PeerGroupV1, PeerRegistryV1, QuantMarketProfileV1, QuantProductKind,
    QuantUnavailableCode,
)

NOW = datetime(2026, 8, 25, 1, tzinfo=timezone.utc)
TOKEN = "fixture-token-1234567890"


class Reply:
    def __init__(self, body):
        self.body = json.dumps({"result": body}).encode()


def _ranking(symbols):
    return {"rankedAt": "2026-08-24T15:30:00+09:00", "rankings": [
        {"rank": i, "symbol": symbol, "currency": "KRW", "price": {"lastPrice": "100", "basePrice": "99", "changeRate": "0.01"}, "tradingVolume": "1000", "tradingAmount": str(10000-i)}
        for i, symbol in enumerate(symbols, 1)
    ]}


def _stocks(symbols):
    return [{"symbol": symbol, "name": f"Name-{symbol}", "englishName": symbol, "isinCode": "US123", "market": "NEW_MARKET", "securityType": "STOCK", "isCommonShare": True, "status": "ACTIVE", "currency": "KRW", "sharesOutstanding": "1000"} for symbol in symbols]


def _candles(last_day, count=65):
    first = last_day - timedelta(days=count - 1)
    return {"candles": [{"timestamp": f"{(first + timedelta(days=i)).isoformat()}T15:30:00+09:00", "openPrice": str(100+i), "highPrice": str(102+i), "lowPrice": str(99+i), "closePrice": str(101+i), "volume": str(1000+i), "currency": "KRW"} for i in range(count)]}


def _deps(tmp_path, starts, *, cap=2):
    def transport(request):
        starts.append((request.operation.value, request.market_country, request.symbols, request.symbol, request.count, request.adjusted))
        if request.operation.value == "RANKINGS":
            return Reply(_ranking(("005930", "000660", "035420")))
        if request.operation.value == "STOCKS":
            return Reply(_stocks(request.symbols))
        return Reply(_candles(date(2026, 8, 23) if request.symbol == "005930" else date(2026, 8, 24)))
    return SimpleNamespace(root=tmp_path, token=TOKEN, transport=transport, clock=lambda: NOW,
                           completed_sessions={QuantMarketProfileV1.KR: date(2026, 8, 24)},
                           profiles=(QuantMarketProfileV1.KR,), profile_revision="profile-1",
                           report_revision="report-1", shortlist_cap=2, candle_start_cap=cap,
                           public_symbols=(), personal_symbols=(), pension_revision=None, reviewed_peers=())


def test_typed_fixture_vertical_builds_cards_and_replay_is_zero(tmp_path):
    from tokmo.programs.quant_discovery_runtime import run_quant_once
    starts = []
    deps = _deps(tmp_path, starts)
    first = run_quant_once(deps=deps, now=NOW)
    assert [card.symbol for card in first.report.cards] == ["000660"]
    assert first.report.unavailable_codes == (QuantUnavailableCode.CANDLE_SESSION_NOT_READY,)
    assert first.symbol_unavailable == (("005930", QuantUnavailableCode.CANDLE_SESSION_NOT_READY),)
    assert [row[0] for row in starts] == ["RANKINGS", "STOCKS", "STOCK_CANDLES", "STOCK_CANDLES"]
    assert starts[1][2] == ("000660", "005930") and all(row[4] == 200 and row[5] is True for row in starts[2:])
    assert first.receipt.operation_counts == (("RANKINGS", 1), ("STOCKS", 1), ("STOCK_CANDLES", 2))
    starts.clear()
    replay = run_quant_once(deps=deps, now=NOW)
    assert replay.report == first.report and replay.replay_counts == (("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0))
    assert replay.receipt.operation_counts == first.receipt.operation_counts and starts == []


@pytest.mark.parametrize(("rankings", "stocks", "symbols", "exclusions", "change_rate"), [
    (((2, "B", "KRW", "0.2"), (1, "A", "KRW", MISSING)),
     (("A", "ACTIVE", "KRW"), ("B", "ACTIVE", "KRW")), ("A", "B"), (), None),
    (((0, "ZERO", "KRW", "0.1"), (1, "A", "KRW", "0.1"), (1, "B", "KRW", "0.1"),
      (2, "A", "KRW", "0.1"), (3, "USD", "USD", "0.1"), ("bad", "TYPE", "KRW", "0.1")),
     (("ZERO", "ACTIVE", "KRW"), ("A", "ACTIVE", "KRW"), ("B", "ACTIVE", "KRW"),
      ("USD", "ACTIVE", "KRW"), ("TYPE", "ACTIVE", "KRW")), ("A",),
     (("ZERO", QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH),
      ("B", QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH),
      ("A", QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH),
      ("USD", QuantUnavailableCode.STOCK_METADATA_UNAVAILABLE),
      ("TYPE", QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH)), "0.1"),
])
def test_ranking_funnel_is_deterministic_and_locally_excludes(rankings, stocks, symbols, exclusions, change_rate):
    from tokmo.programs.quant_discovery_runtime import admit_ranking_candidates
    from tokmo.values.toss_rankings import RankingItem, RankingPrice, RankingResponse
    from tokmo.values.toss_reference import StockInfo
    ranking = RankingResponse(tuple(RankingItem(rank, symbol, currency, RankingPrice("100", "99", rate), "1", "10")
                                     for rank, symbol, currency, rate in rankings))
    stock_values = tuple(StockInfo(symbol, symbol, symbol, "ISIN", "M", "STOCK", True, status, currency, "1")
                         for symbol, status, currency in stocks)
    result = admit_ranking_candidates(QuantMarketProfileV1.KR, ranking, stock_values)
    assert tuple(item.symbol for item in result.candidates) == symbols
    assert result.exclusions == exclusions
    assert (result.candidates[0].ranking_change_rate if result.candidates else None) == (None if change_rate is None else __import__("decimal").Decimal(change_rate))


def test_single_market_and_cap_refuse_before_effectful_candle_reads(tmp_path):
    from tokmo.programs.quant_discovery_runtime import run_quant_once
    starts = []
    with pytest.raises(ValueError, match="candle start cap"):
        run_quant_once(deps=_deps(tmp_path, starts, cap=1), now=NOW)
    assert [row[0] for row in starts] == ["RANKINGS", "STOCKS"]
    deps = _deps(tmp_path / "multi", starts)
    deps.profiles = (QuantMarketProfileV1.KR, QuantMarketProfileV1.US)
    starts.clear()
    with pytest.raises(ValueError, match="single market"):
        run_quant_once(deps=deps, now=NOW)
    assert starts == [] and not (tmp_path / "multi").exists()


@pytest.mark.parametrize("tamper", ["semantic", "whitespace"])
def test_receipt_digest_rejects_well_formed_report_tampering(tmp_path, tamper):
    from tokmo.programs.quant_discovery_runtime import run_quant_once
    starts = []
    first = run_quant_once(deps=_deps(tmp_path, starts), now=NOW)
    report_path = next(tmp_path.rglob("*.report.json"))
    payload = json.loads(report_path.read_text())
    if tamper == "semantic":
        payload["cards"] = []
        changed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    else:
        changed = json.dumps(payload, sort_keys=True, indent=2)
    report_path.write_text(changed)
    report_path.chmod(0o600)
    with pytest.raises(ValueError, match="receipt-bound report digest"):
        run_quant_once(deps=_deps(tmp_path, []), now=NOW)
    assert first.receipt.report_sha256


@pytest.mark.parametrize(("ranking_body", "candle_body", "outcome", "codes", "expected_counts"), [
    (_ranking(()), None, "NO_DATA", (QuantUnavailableCode.RANKING_NO_DATA,), (("RANKINGS", 1), ("STOCKS", 0), ("STOCK_CANDLES", 0))),
    ({"rankedAt": "bad", "rankings": "bad"}, None, "FAILED", (QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH,), (("RANKINGS", 1), ("STOCKS", 0), ("STOCK_CANDLES", 0))),
    ({**_ranking(("005930",)), "rankings": [{**_ranking(("005930",))["rankings"][0], "rank": 0}]}, None, "FAILED", (QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH,), (("RANKINGS", 1), ("STOCKS", 1), ("STOCK_CANDLES", 0))),
    (_ranking(("005930",)), {"unexpected": True}, "FAILED", (QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH,), (("RANKINGS", 1), ("STOCKS", 1), ("STOCK_CANDLES", 1))),
])
def test_terminal_outcome_reports_body_failure_and_replays_zero(tmp_path, ranking_body, candle_body, outcome, codes, expected_counts):
    from tokmo.programs.quant_discovery_runtime import run_quant_once
    starts = []
    deps = _deps(tmp_path, starts)
    original = deps.transport
    def transport(request):
        if request.operation.value == "RANKINGS":
            starts.append((request.operation.value, request.market_country, request.symbols, request.symbol, request.count))
            return Reply(ranking_body)
        if candle_body is not None and request.operation.value == "STOCK_CANDLES":
            starts.append((request.operation.value, request.market_country, request.symbols, request.symbol, request.count))
            return Reply(candle_body)
        return original(request)
    deps.transport = transport
    first = run_quant_once(deps=deps, now=NOW)
    assert first.receipt.outcome == outcome
    assert first.report.unavailable_codes == codes
    assert first.receipt.operation_counts == expected_counts
    starts.clear()
    replay = run_quant_once(deps=deps, now=NOW)
    assert replay.report == first.report and replay.receipt == first.receipt
    assert replay.replay_counts == (("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0)) and starts == []


def test_valid_active_pension_projection_and_union_are_isolated():
    from tokmo.programs.quant_discovery_runtime import plan_target_union, project_pension_symbols
    item = HoldingItemV1("069500", "3", "100", "KRW", "KR", None)
    revision = PensionHoldingsRevisionV1("a"*32, PENSION_HOLDINGS_SCHEMA_VERSION, "b"*64, PENSION_PARSER_VERSION, 1, (item,), NOW, NOW, NOW, "c"*64, "d"*64, 1, 0)
    projected = project_pension_symbols(revision)
    assert projected.symbols == (("069500", QuantMarketProfileV1.KR),) and projected.unavailable_code is None
    union = plan_target_union(discovery=(("005930", QuantMarketProfileV1.KR),), public_personal=(("AAPL", QuantMarketProfileV1.US),), active_pension=projected.symbols, reviewed_peers=(("005930", QuantMarketProfileV1.KR),))
    assert union.market == (("005930", QuantMarketProfileV1.KR),)
    assert union.personal == (("AAPL", QuantMarketProfileV1.US),)
    assert union.pension == (("069500", QuantMarketProfileV1.KR),)
    assert union.all_unique == (("005930", QuantMarketProfileV1.KR), ("069500", QuantMarketProfileV1.KR), ("AAPL", QuantMarketProfileV1.US))


def test_target_union_preserves_admitted_discovery_order_before_sorted_extras():
    from tokmo.programs.quant_discovery_runtime import plan_target_union

    union = plan_target_union(
        discovery=(("SPY", QuantMarketProfileV1.US), ("NVDA", QuantMarketProfileV1.US)),
        public_personal=(("AAPL", QuantMarketProfileV1.US),),
        active_pension=(),
        reviewed_peers=(("SPY", QuantMarketProfileV1.US),),
    )

    assert union.all_unique == (
        ("SPY", QuantMarketProfileV1.US),
        ("NVDA", QuantMarketProfileV1.US),
        ("AAPL", QuantMarketProfileV1.US),
    )


def test_local_tool_requires_fixture_and_prints_public_receipt(tmp_path, capsys):
    from tools.run_quant_v2_once import main
    with pytest.raises(SystemExit):
        main([])
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"root": str(tmp_path / "reports"), "token": TOKEN, "completed_session": "2026-08-24", "profile_revision": "p", "report_revision": "r", "shortlist_cap": 1, "candle_start_cap": 1, "replies": {"RANKINGS:KR": _ranking(("005930",)), "STOCKS:005930": _stocks(("005930",)), "STOCK_CANDLES:005930": _candles(date(2026, 8, 24))}}))
    assert main(["--fixture", str(fixture)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"identity_sha256", "outcome", "operation_counts", "report_sha256", "delivery_state"}


def test_source_import_guard_and_news_notice():
    root = Path(__file__).parents[1]
    source = root / "src/tokmo/programs/quant_discovery_runtime.py"
    tree = ast.parse(source.read_text())
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imports |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    forbidden = ("quant_recurring", "quant_profile", "membership", "news", "cadence", "telegram", "composition")
    assert not any(any(token in name.lower() for token in forbidden) for name in imports)
    lowered = source.read_text().lower()
    assert "news" in lowered and "trade trigger" in lowered and "v2" not in lowered


def test_personal_peer_real_vertical_is_private_reviewed_and_replayable(tmp_path, monkeypatch):
    import tokmo.programs.quant_discovery_runtime as runtime
    from tokmo.programs.quant_discovery_runtime import run_personal_peer_once
    from tokmo.values.pension_holdings import pension_account_scope
    personal_scope = "toss-acct-" + "1" * 24
    holdings = CurrentHoldingsV1(CURRENT_HOLDINGS_SCHEMA_VERSION, (
        AccountHoldingsV1(personal_scope, 1, (HoldingItemV1("REF", "SECRET_QTY", "SECRET_AVG", "KRW", "KR", None),)),
        AccountHoldingsV1(pension_account_scope(), 2, (HoldingItemV1("PENSION_SECRET", "9", "8", "KRW", "KR", None),)),
    ), True, NOW, {
        personal_scope: AccountScopeIdentityV1(personal_scope, "SECRET_ACCOUNT", 1, "SECRET_MASK"),
        pension_account_scope(): AccountScopeIdentityV1(pension_account_scope(), "pension", 2, None),
    })
    group = PeerGroupV1("personal-ref", QuantProductKind.PERSONAL_PEER, QuantMarketProfileV1.KR,
                        "STOCK", "REF", ("PEER",), "manual review", date(2026, 8, 24), ())
    starts = []
    deps = _deps(tmp_path, starts, cap=2)
    deps.peer_registry = PeerRegistryV1("quant-peer-registry-v1", "peers-1", date(2026, 8, 24), (group,))
    deps.transport = lambda request: (starts.append((request.operation.value, request.symbols, request.symbol)) or
        Reply(_stocks(request.symbols) if request.operation.value == "STOCKS" else _candles(date(2026, 8, 24))))
    first = run_personal_peer_once(deps=deps, holdings=holdings, completed_session=date(2026, 8, 24), now=NOW)
    assert first.report.identity.product_kind is QuantProductKind.PERSONAL_PEER
    assert [card.symbol for card in first.report.cards] == ["REF"]
    assert first.report.cards[0].standings and all(s.sample_size == 2 and s.percentile is None for s in first.report.cards[0].standings)
    assert starts == [("STOCKS", ("PEER", "REF"), ""), ("STOCK_CANDLES", (), "PEER"), ("STOCK_CANDLES", (), "REF")]
    private = "SECRET_QTY SECRET_AVG SECRET_ACCOUNT SECRET_MASK PENSION_SECRET " + personal_scope + " " + pension_account_scope()
    public_bytes = repr((first.report, first.receipt, starts)) + "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert not any(token in public_bytes for token in private.split())
    starts.clear()
    projection_calls = 0
    original_projection = runtime.project_personal_symbols
    def counted_projection(value):
        nonlocal projection_calls
        projection_calls += 1
        return original_projection(value)
    monkeypatch.setattr(runtime, "project_personal_symbols", counted_projection)
    replay = run_personal_peer_once(deps=deps, holdings=holdings, completed_session=date(2026, 8, 24), now=NOW)
    assert replay.report == first.report and starts == [] and projection_calls == 0
    assert replay.replay_counts == (("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0))
    changed_holdings = CurrentHoldingsV1(CURRENT_HOLDINGS_SCHEMA_VERSION, holdings.accounts, True,
                                        NOW + timedelta(seconds=1), holdings.identity_map)
    changed = run_personal_peer_once(deps=deps, holdings=changed_holdings,
                                     completed_session=date(2026, 8, 24), now=NOW)
    assert changed.report.identity.identity_sha256 != first.report.identity.identity_sha256
    absent_then_ready = _deps(tmp_path / "absent-then-ready", [], cap=2)
    absent_then_ready.peer_registry = deps.peer_registry
    absent_then_ready.transport = deps.transport
    absent_first = run_personal_peer_once(deps=absent_then_ready, holdings=None,
                                          completed_session=date(2026, 8, 24), now=NOW)
    starts.clear()
    ready = run_personal_peer_once(deps=absent_then_ready, holdings=holdings,
                                   completed_session=date(2026, 8, 24), now=NOW)
    assert absent_first.report.identity.identity_sha256 != ready.report.identity.identity_sha256
    assert [card.symbol for card in ready.report.cards] == ["REF"] and starts
    missing = _deps(tmp_path / "missing", [], cap=0)
    missing.peer_registry = PeerRegistryV1("quant-peer-registry-v1", "empty", date(2026, 8, 24), ())
    result = run_personal_peer_once(deps=missing, holdings=holdings, completed_session=date(2026, 8, 24), now=NOW)
    assert result.report.cards == () and result.report.unavailable_codes == (QuantUnavailableCode.PEER_UNCONFIRMED,)
    partial_holdings = CurrentHoldingsV1(CURRENT_HOLDINGS_SCHEMA_VERSION, (
        AccountHoldingsV1(personal_scope, 1, (
            HoldingItemV1("REF", "PRIVATE_QTY", "PRIVATE_AVG", "KRW", "KR", None),
            HoldingItemV1("MISSING", "PRIVATE_QTY_2", "PRIVATE_AVG_2", "KRW", "KR", None),
        )),), True, NOW + timedelta(seconds=2), {personal_scope: holdings.identity_map[personal_scope]})
    partial_deps = _deps(tmp_path / "partial", starts, cap=2)
    partial_deps.peer_registry = deps.peer_registry
    partial_deps.transport = deps.transport
    starts.clear()
    partial = run_personal_peer_once(deps=partial_deps, holdings=partial_holdings,
                                     completed_session=date(2026, 8, 24), now=NOW)
    assert [card.symbol for card in partial.report.cards] == ["REF"]
    assert partial.symbol_unavailable == (("MISSING", QuantUnavailableCode.PEER_UNCONFIRMED),)
    assert partial.report.unavailable_codes == (QuantUnavailableCode.PEER_UNCONFIRMED,)
    assert starts == [("STOCKS", ("PEER", "REF"), ""),
                      ("STOCK_CANDLES", (), "PEER"), ("STOCK_CANDLES", (), "REF")]
    assert not any(token in repr(partial) for token in ("PRIVATE_QTY", "PRIVATE_AVG", personal_scope))
    for age_days, expect_card in ((90, True), (91, False)):
        age_deps = _deps(tmp_path / f"peer-age-{age_days}", [], cap=2)
        reviewed = NOW.date() - timedelta(days=age_days)
        age_group = PeerGroupV1("personal-ref", QuantProductKind.PERSONAL_PEER,
            QuantMarketProfileV1.KR, "STOCK", "REF", ("PEER",),
            "manual review", reviewed, ())
        age_deps.peer_registry = PeerRegistryV1("quant-peer-registry-v1",
            f"peer-age-{age_days}", reviewed, (age_group,))
        age_deps.transport = deps.transport
        age_result = run_personal_peer_once(deps=age_deps, holdings=holdings,
            completed_session=date(2026, 8, 24), now=NOW)
        assert bool(age_result.report.cards) is expect_card
        if not expect_card:
            assert age_result.report.unavailable_codes == (QuantUnavailableCode.PEER_UNCONFIRMED,)
    mismatch_deps = _deps(tmp_path / "personal-all-session-mismatch", [], cap=2)
    mismatch_deps.peer_registry = deps.peer_registry
    mismatch_deps.transport = lambda request: Reply(
        _stocks(request.symbols) if request.operation.value == "STOCKS"
        else _candles(date(2026, 8, 23)))
    mismatch = run_personal_peer_once(deps=mismatch_deps, holdings=holdings,
        completed_session=date(2026, 8, 24), now=NOW)
    assert mismatch.receipt.outcome == "SUCCEEDED_SILENT"
    assert mismatch.report.unavailable_codes == (QuantUnavailableCode.CANDLE_SESSION_NOT_READY,)
    capped = _deps(tmp_path / "cap", [], cap=1); capped.peer_registry = deps.peer_registry; capped.transport = deps.transport
    starts.clear()
    with pytest.raises(ValueError, match="candle start cap"):
        run_personal_peer_once(deps=capped, holdings=holdings, completed_session=date(2026, 8, 24), now=NOW)
    assert starts == [("STOCKS", ("PEER", "REF"), "")]


def test_pension_efficiency_requires_active_final_week_and_is_namespaced(tmp_path, monkeypatch):
    import tokmo.programs.quant_discovery_runtime as runtime
    from tokmo.programs.quant_discovery_runtime import run_pension_efficiency_once
    starts = []
    deps = _deps(tmp_path / "valid", starts, cap=2)
    group = PeerGroupV1("pension-etf", QuantProductKind.PENSION_EFFICIENCY, QuantMarketProfileV1.KR,
                        "ETF", "069500", ("102110",), "manual pension review", date(2026, 8, 24), ())
    deps.peer_registry = PeerRegistryV1("quant-peer-registry-v1", "pension-peers-1", date(2026, 8, 24), (group,))
    def pension_transport(request):
        starts.append((request.operation.value, request.symbols, request.symbol))
        if request.operation.value == "STOCKS":
            rows = _stocks(request.symbols)
            for row in rows:
                row["securityType"] = "ETF"
            return Reply(rows)
        return Reply(_candles(date(2026, 8, 24)))
    deps.transport = pension_transport
    absent_deps = _deps(tmp_path / "absent", starts, cap=2)
    absent_deps.peer_registry = deps.peer_registry
    absent = run_pension_efficiency_once(deps=absent_deps, active_revision=None, completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW)
    assert absent.report.unavailable_codes == (QuantUnavailableCode.PENSION_ACTIVE_REVISION_ABSENT,) and starts == []
    revision = PensionHoldingsRevisionV1("a"*32, PENSION_HOLDINGS_SCHEMA_VERSION, "b"*64, PENSION_PARSER_VERSION, 1,
        (HoldingItemV1("069500", "PRIVATE_QTY", "PRIVATE_AVG", "KRW", "KR", None),), NOW, NOW, NOW, "c"*64, "d"*64, 1, 0)
    not_final_deps = _deps(tmp_path / "not-final", starts, cap=2)
    not_final_deps.peer_registry = deps.peer_registry
    not_final_deps.transport = pension_transport
    not_final = run_pension_efficiency_once(deps=not_final_deps, active_revision=revision, completed_session=date(2026, 8, 24), completed_week_final_proof=False, now=NOW)
    assert not_final.report.unavailable_codes == (QuantUnavailableCode.PENSION_WEEK_NOT_FINAL,) and starts == []
    starts.clear()
    final_after_pending = run_pension_efficiency_once(deps=not_final_deps, active_revision=revision,
        completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW)
    assert [card.symbol for card in final_after_pending.report.cards] == ["069500"] and starts
    assert final_after_pending.report.identity.identity_sha256 != not_final.report.identity.identity_sha256
    starts.clear()
    first = run_pension_efficiency_once(deps=deps, active_revision=revision, completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW)
    assert first.report.identity.product_kind is QuantProductKind.PENSION_EFFICIENCY
    assert first.report.identity.market is QuantMarketProfileV1.KR and first.report.identity.completed_iso_week == "2026-W35"
    assert first.report.identity.peer_registry_revision == "pension-peers-1"
    assert [card.symbol for card in first.report.cards] == ["069500"] and first.report.cards[0].standings
    assert starts == [("STOCKS", ("069500", "102110"), ""), ("STOCK_CANDLES", (), "069500"), ("STOCK_CANDLES", (), "102110")]
    assert "PRIVATE_QTY" not in repr(first) and "PRIVATE_AVG" not in repr(first)
    starts.clear()
    projection_calls = 0
    original_projection = runtime.project_pension_symbols
    def counted_projection(value):
        nonlocal projection_calls
        projection_calls += 1
        return original_projection(value)
    monkeypatch.setattr(runtime, "project_pension_symbols", counted_projection)
    replay = run_pension_efficiency_once(deps=deps, active_revision=revision, completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW)
    assert replay.report == first.report and starts == [] and projection_calls == 0
    assert replay.receipt.operation_counts == first.receipt.operation_counts
    assert first.report.identity.identity_sha256 != absent.report.identity.identity_sha256
    changed_profile = _deps(tmp_path / "profile-change", [], cap=2)
    changed_profile.peer_registry = deps.peer_registry
    changed_profile.transport = deps.transport
    profile_one = run_pension_efficiency_once(deps=changed_profile, active_revision=revision,
        completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW)
    changed_profile.profile_revision = "profile-2"
    profile_two = run_pension_efficiency_once(deps=changed_profile, active_revision=revision,
        completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW)
    assert profile_one.report.identity.identity_sha256 != profile_two.report.identity.identity_sha256
    mismatch_deps = _deps(tmp_path / "all-session-mismatch", [], cap=2)
    mismatch_deps.peer_registry = deps.peer_registry
    def mismatch_transport(request):
        if request.operation.value != "STOCKS":
            return Reply(_candles(date(2026, 8, 23)))
        rows = _stocks(request.symbols)
        for row in rows:
            row["securityType"] = "ETF"
        return Reply(rows)
    mismatch_deps.transport = mismatch_transport
    mismatch = run_pension_efficiency_once(deps=mismatch_deps, active_revision=revision,
        completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW)
    assert mismatch.receipt.outcome == "SUCCEEDED_SILENT"
    assert mismatch.report.unavailable_codes == (QuantUnavailableCode.CANDLE_SESSION_NOT_READY,)
    with pytest.raises(ValueError, match="KR only"):
        run_pension_efficiency_once(deps=deps, active_revision=revision, completed_session=date(2026, 8, 24), completed_week_final_proof=True, now=NOW, market=QuantMarketProfileV1.US)
