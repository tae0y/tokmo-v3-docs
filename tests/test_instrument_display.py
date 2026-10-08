"""User-facing instrument display contract (2026-08-21).

One pure formatter owns every user-facing instrument rendering:

    KR: 한글명(종목코드)      — 현대차(005380)
    US: 영문티커(한글명)      — MRNA(모더나)

Names come only from canonical, Toss-evidenced identity — the Git-tracked
playbook document's ``instrument_name`` front-matter field. Nothing is ever
translated, transliterated, or guessed: an unavailable name fails soft to
the canonical symbol/code alone.
"""

from datetime import datetime

import pytest

from tokmo.programs.conditional_detection import (
    build_conditional_fill_question,
)
from tokmo.programs.execution_detection import NewTradeExecutionV1
from tokmo.programs.execution_dialogue import build_trade_question_message
from tokmo.records.playbook_identity import (
    playbook_scope_dir,
    read_instrument_name,
)
from tokmo.values.instrument_display import (
    InstrumentDisplayV1,
    format_instrument,
)
from tokmo.values.trade_event import TradeEventTimesV1, trade_event_id

TOSS_SCOPE = "toss-acct-" + "a" * 24
PENSION_SCOPE = "pension-excel-" + "b" * 24


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


NOW = _utc("2026-08-20T21:10:00Z")


# --- RED-D1: the pure formatter --------------------------------------------


def test_kr_renders_korean_name_then_code():
    assert format_instrument(
        InstrumentDisplayV1("KR", "005380", "현대차")) == "현대차(005380)"


def test_us_renders_ticker_then_korean_name():
    assert format_instrument(
        InstrumentDisplayV1("US", "MRNA", "모더나")) == "MRNA(모더나)"


def test_missing_name_fails_soft_to_symbol_only():
    assert format_instrument(InstrumentDisplayV1("KR", "005380", None)) \
        == "005380"
    assert format_instrument(InstrumentDisplayV1("US", "MRNA", None)) \
        == "MRNA"


def test_blank_or_non_string_name_is_treated_as_unavailable():
    for name in ("", "   ", "\n"):
        assert format_instrument(InstrumentDisplayV1("US", "MRNA", name)) \
            == "MRNA"


def test_display_value_rejects_unknown_market_and_empty_symbol():
    with pytest.raises(ValueError):
        InstrumentDisplayV1("JP", "7203", "도요타")
    with pytest.raises(ValueError):
        InstrumentDisplayV1("KR", "", "현대차")
    with pytest.raises(ValueError):
        InstrumentDisplayV1("KR", "005380", 5)


def test_formatter_is_pure_and_never_reaches_a_provider():
    import inspect

    from tokmo.values import instrument_display

    source = inspect.getsource(instrument_display)
    for forbidden in ("open(", "Path", "requests", "urlopen", "import os"):
        assert forbidden not in source


# --- RED-D2: the canonical name resolver seam ------------------------------


def _playbook(root, *, scope_dir, market, code, name):
    target = root / scope_dir / market / f"{code}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        "schema_version: 1\n"
        "document_kind: instrument\n"
        f'playbook_id: "{scope_dir}:{market}:{code}"\n'
        f"portfolio_scope: {scope_dir}\n"
        f"market: {market}\n"
        f'instrument_code: "{code}"\n'
        f'instrument_name: "{name}"\n'
        "---\n\n"
        f"# {name}\n\n## 3층 — 매매이력\n",
        encoding="utf-8")
    return target


def test_scope_dir_maps_toss_to_personal_and_pension_to_retirement():
    assert playbook_scope_dir(TOSS_SCOPE) == "personal"
    assert playbook_scope_dir(PENSION_SCOPE) == "retirement"


def test_resolver_reads_the_canonical_front_matter_name(tmp_path):
    root = tmp_path / "playbooks"
    _playbook(root, scope_dir="personal", market="US", code="MRNA",
              name="모더나")
    assert read_instrument_name(
        root, account_scope=TOSS_SCOPE, market="US", symbol="MRNA") == "모더나"


def test_resolver_returns_none_when_the_document_is_absent(tmp_path):
    root = tmp_path / "playbooks"
    root.mkdir()
    assert read_instrument_name(
        root, account_scope=TOSS_SCOPE, market="US", symbol="MRNA") is None


def test_resolver_returns_none_for_a_document_without_the_field(tmp_path):
    root = tmp_path / "playbooks"
    target = root / "personal" / "US" / "MRNA.md"
    target.parent.mkdir(parents=True)
    target.write_text("# MRNA\n\n## 3층 — 매매이력\n", encoding="utf-8")
    assert read_instrument_name(
        root, account_scope=TOSS_SCOPE, market="US", symbol="MRNA") is None


def test_resolver_never_reads_outside_its_own_market_and_scope(tmp_path):
    root = tmp_path / "playbooks"
    _playbook(root, scope_dir="retirement", market="US", code="MRNA",
              name="모더나")
    # the personal document does not exist: no cross-scope borrowing
    assert read_instrument_name(
        root, account_scope=TOSS_SCOPE, market="US", symbol="MRNA") is None


def test_resolver_refuses_traversal_shaped_symbols(tmp_path):
    root = tmp_path / "playbooks"
    _playbook(root, scope_dir="personal", market="US", code="MRNA",
              name="모더나")
    for symbol in ("../US/MRNA", "MRNA/../MRNA", "/etc/passwd", ""):
        assert read_instrument_name(
            root, account_scope=TOSS_SCOPE, market="US",
            symbol=symbol) is None


def test_resolver_is_none_for_unknown_scope_or_market(tmp_path):
    root = tmp_path / "playbooks"
    _playbook(root, scope_dir="personal", market="US", code="MRNA",
              name="모더나")
    assert read_instrument_name(
        root, account_scope="not-a-scope", market="US",
        symbol="MRNA") is None
    assert read_instrument_name(
        root, account_scope=TOSS_SCOPE, market="JP", symbol="MRNA") is None
    assert read_instrument_name(
        None, account_scope=TOSS_SCOPE, market="US", symbol="MRNA") is None


# --- RED-D3: production renderers use the formatter ------------------------


def _times():
    return TradeEventTimesV1(
        filled_at=_utc("2026-08-20T21:10:00Z"), provider_observed_at=None,
        discovered_at=_utc("2026-08-20T21:12:00Z"),
        stored_at=_utc("2026-08-20T21:12:00Z"), notified_at=None)


def _event(symbol="MRNA", market="US", scope=TOSS_SCOPE):
    return NewTradeExecutionV1(
        trade_event_id("toss-orders-v1", scope, "ord-1"), scope, "ord-1",
        symbol, "BUY", "USD", "3", "25.5", "76.5", market, _times())


def test_trade_question_header_uses_the_us_display_form(tmp_path):
    root = tmp_path / "playbooks"
    _playbook(root, scope_dir="personal", market="US", code="MRNA",
              name="모더나")
    message = build_trade_question_message(
        _event(), evaluation=None, playbook_root=root)
    assert message.startswith("모더나(MRNA)\n매수 체결 확인\n")


def test_trade_question_header_uses_the_kr_display_form(tmp_path):
    root = tmp_path / "playbooks"
    _playbook(root, scope_dir="personal", market="KR", code="005380",
              name="현대차")
    message = build_trade_question_message(
        _event(symbol="005380", market="KR"), evaluation=None,
        playbook_root=root)
    assert message.startswith("현대차(005380)\n매수 체결 확인\n")


def test_trade_question_header_falls_back_to_the_bare_symbol(tmp_path):
    message = build_trade_question_message(
        _event(), evaluation=None, playbook_root=tmp_path / "playbooks")
    assert message.startswith("MRNA\n매수 체결 확인\n")
    message_without_root = build_trade_question_message(
        _event(), evaluation=None, playbook_root=None)
    assert message_without_root.startswith("MRNA\n매수 체결 확인\n")


def test_trade_question_never_leaks_the_playbook_path(tmp_path):
    root = tmp_path / "playbooks"
    _playbook(root, scope_dir="personal", market="US", code="MRNA",
              name="모더나")
    message = build_trade_question_message(
        _event(), evaluation=None, playbook_root=root)
    assert str(tmp_path) not in message
    assert "playbooks" not in message
    assert TOSS_SCOPE not in message


def test_conditional_question_leads_with_the_instrument_display():
    message = build_conditional_fill_question(
        account_scope=TOSS_SCOPE, symbol="MRNA", market_class="US",
        instrument_name="모더나")
    assert message.splitlines()[0] == "모더나(MRNA)"
    assert message.splitlines()[1] == "조건에 도달해 주문이 접수됐어요."


def test_conditional_question_falls_back_to_the_bare_symbol():
    message = build_conditional_fill_question(
        account_scope=TOSS_SCOPE, symbol="MRNA", market_class="US",
        instrument_name=None)
    assert message.splitlines()[0] == "MRNA"


def test_conditional_question_keeps_the_confirmed_body_lines():
    message = build_conditional_fill_question(
        account_scope=TOSS_SCOPE, symbol="005380", market_class="KR",
        instrument_name="현대차")
    assert message.splitlines() == [
        "현대차(005380)",
        "조건에 도달해 주문이 접수됐어요.",
        "체결 여부는 아직 확인되지 않았어요.",
        "",
        "추천문구",
        "- 플레이북 정보 부족으로 추천문구 없음",
        "",
        "답장으로 남긴 매매 사유는 체결 확인 전까지 임시 보관해요.",
    ]


# --- RED-D4: no duplicated display string logic ----------------------------


def test_no_renderer_hand_rolls_the_parenthesised_display_form():
    """Every user-facing instrument display goes through the one formatter."""
    from pathlib import Path

    src = Path(__file__).parents[1] / "src" / "tokmo"
    owners = {
        "values/instrument_display.py",
    }
    for path in sorted(src.rglob("*.py")):
        relative = path.relative_to(src).as_posix()
        if relative in owners:
            continue
        text = path.read_text(encoding="utf-8")
        assert "({name})" not in text, relative
        assert "({symbol})" not in text, relative
