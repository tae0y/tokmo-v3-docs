"""Slice 0 trade-reason seam (contract 2026-09-03 §8): network-zero journey."""

import ast
import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

from tokmo.programs import trade_reason_seam as seam
from tokmo.records.playbook_identity import read_instrument_name
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.trade_event import trade_event_id
from tokmo.values.trade_ledger import ImportAuditMeta, TradeLedgerEvent

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 9, 3, 6, 30, tzinfo=UTC)
SCOPE = "toss-acct-" + "a" * 24
OTHER_SCOPE = "toss-acct-" + "b" * 24
FILLED = datetime(2026, 8, 10, 3, 59, tzinfo=UTC)  # 12:59 KST
ALERT = (
    "Forwarded from Tokmo Stock Bot\n"
    "SPCX(스페이스엑스) 매도 확인\n"
    "3주  $90.50 = $271.50\n"
    "체결 2026-08-10 12:59 / 확인 2026-08-10 13:00\n"
    "추천 문구\n- 관련 정보가 부족해 추천문구 없음\n"
    "이 체결의 당시 이유나 맥락을 플레이북 매매이력에 추가할까요?"
)
REASON = "ai  집중을 해소하기위해 일부 매도를 계획 했으나\t생각보다 많이 올라서 매도함\n".encode()
INTERPRETATION = "AI 집중 해소 목적의 축소 매도로 해석한다."
OLD_ENTRY = "trade-old00001-spcx-buy"
_LEGACY = (
    "# 문서\n\n<!-- HERMES_CURRENT_START -->\n생각\n<!-- HERMES_CURRENT_END -->\n"
    "<!-- HERMES_ANCHORS_START -->\n<!-- HERMES_ANCHORS_END -->\n"
)
SPCX_DOC = f'''---
schema_version: 1
document_kind: instrument
playbook_id: "personal:US:SPCX"
portfolio_scope: personal
market: US
instrument_code: "SPCX"
instrument_name: "스페이스엑스"
summary_version: 0
summary_as_of: "unreviewed"
source_cutoff: "unreviewed"
---

# 스페이스엑스

<!-- HERMES_CURRENT_START -->
## 1층 — 헤르의 생각

성장 지속 관찰.
<!-- HERMES_CURRENT_END -->

<!-- USER_THESIS_START -->
## 2층 — 사용자의 원문

장기 보유.
<!-- USER_THESIS_END -->

## 3층 — 매매이력

{seam.LAYER3_PREAMBLE}

<a id="entry-{OLD_ENTRY}"></a>
### 2026-07-01 — 매수 (2주 @ USD 80)

- `entry_id`: `{OLD_ENTRY}`
- `source_kind`: `user_trade_reason`
- `source_ref`: `trade_ledger_events#old`
- `event_date`: `2026-07-01`
- `recorded_date`: `2026-07-01`
- `source_snapshot_date`: `2026-07-01`
- `source_snapshot_basis`: `v3_trade_ledger_event`
- `v3_trade_event_id`: `unlinked`
- `trade_summary`: `BUY 2 SPCX @ USD 80 = 160, filled 2026-07-01`
- `imported_at`: `2026-07-01T10:00:00+09:00`

<!-- USER_RAW_START entry_id="{OLD_ENTRY}" -->
첫 매수 이유
<!-- USER_RAW_END entry_id="{OLD_ENTRY}" -->
'''


def _validator():
    spec = importlib.util.spec_from_file_location(
        "validate_playbooks", ROOT / "scripts" / "playbooks" / "validate_playbooks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_tree


def _root(tmp_path, *, with_spcx=True):
    root = tmp_path / "playbooks"
    for sub in ("personal/US", "personal/KR", "retirement/KR"):
        (root / sub).mkdir(parents=True)
    (root / "portfolio.md").write_text(_LEGACY, encoding="utf-8")
    (root / "personal" / "_account.md").write_text(_LEGACY, encoding="utf-8")
    if with_spcx:
        (root / "personal" / "US" / "SPCX.md").write_text(SPCX_DOC, encoding="utf-8")
    return root


def _trade(
    order="ord-001",
    scope=SCOPE,
    symbol="SPCX",
    side="SELL",
    price="90.5",
    market="US",
    currency="USD",
    filled_at=FILLED,
    trade_date=date(2026, 8, 9),
):
    return seam.LedgerTradeV1(
        trade_event_id(seam.LEDGER_SOURCE, scope, order),
        scope,
        market,
        symbol,
        side,
        "3",
        price,
        "271.5",
        currency,
        trade_date,
        filled_at,
    )


def _preview(root, trade=None, reason=REASON, **overrides):
    kwargs = dict(
        playbook_root=root,
        trade=trade or _trade(),
        reason=reason,
        now=NOW,
        hermes_interpretation=INTERPRETATION,
        user_thesis=None,
        instrument_name="스페이스엑스",
    )
    kwargs.update(overrides)
    return seam.build_reason_preview(**kwargs)


def _apply(root, preview, *, token=None, ledger=None, validator=None):
    return seam.apply_approved_reason(
        playbook_root=root,
        preview=preview,
        approval_token=preview.approval_token if token is None else token,
        ledger_trades=(_trade(),) if ledger is None else ledger,
        playbook_validator=validator or _validator(),
    )


def test_forwarded_alert_binds_exactly_one_ledger_trade():
    hint = seam.parse_alert_hint(ALERT)
    assert (
        hint.symbol,
        hint.side,
        hint.quantity,
        hint.unit_price,
        hint.currency,
        hint.filled_kst,
        hint.source_label,
    ) == ("SPCX", "SELL", "3", "90.50", "USD", "2026-08-10 12:59", "Tokmo Stock Bot")
    ledger = (_trade(), _trade("ord-002", side="BUY"), _trade("ord-003", symbol="NFLX"))
    result = seam.match_ledger_trade(hint, ledger, account_scope=SCOPE, market="US")
    assert result.status == "matched"
    assert result.trade.trade_event_id == trade_event_id(
        seam.LEDGER_SOURCE, SCOPE, "ord-001"
    )
    assert dict(result.writes) == {
        "playbook_files": 0,
        "ledger": 0,
        "order": 0,
        "account": 0,
        "provider": 0,
    }


def test_ambiguous_or_missing_candidates_fail_closed_with_sanitized_candidates():
    no_price = "\n".join(line for line in ALERT.splitlines() if "주 " not in line)
    hint = seam.parse_alert_hint(no_price)
    ledger = (
        _trade(),
        _trade("ord-009", price="91.5"),
        _trade("ord-010", scope=OTHER_SCOPE),
    )
    result = seam.match_ledger_trade(hint, ledger)
    assert result.status == "ambiguous" and result.trade is None
    assert len(result.candidates) == 3
    assert result.missing_discriminators[:1] == ("account_scope",)
    assert "quantity" in result.missing_discriminators
    assert "unit_price" in result.missing_discriminators
    flattened = repr(result.candidates)
    assert SCOPE not in flattened and OTHER_SCOPE not in flattened
    assert {item["scope"] for item in result.candidates} == {"personal"}
    assert {item["trade_event_id_prefix"] for item in result.candidates} == {
        trade.trade_event_id[:8] for trade in ledger
    }
    assert (
        seam.match_ledger_trade(hint, ledger, account_scope=SCOPE).status == "ambiguous"
    )
    unknown = seam.parse_alert_hint(ALERT.replace("SPCX(스페이스엑스)", "MRNA(모더나)"))
    assert seam.match_ledger_trade(unknown, ledger).status == "no_match"
    assert seam.parse_alert_hint("아무 말이나\n\n").symbol is None
    assert seam.parse_alert_hint(b"bytes") == seam.AlertHintV1()


def test_kr_display_and_date_only_ledger_row_match_by_market_date():
    text = "현대차(005380) 매수 확인\n10주  ₩70,000.00 = ₩700,000.00\n체결 2026-08-10 09:05 / 확인 2026-08-10 09:06"
    hint = seam.parse_alert_hint(text)
    assert (hint.symbol, hint.side, hint.quantity, hint.unit_price, hint.currency) == (
        "005380",
        "BUY",
        "10",
        "70000.00",
        "KRW",
    )
    kr = seam.LedgerTradeV1(
        trade_event_id(seam.LEDGER_SOURCE, SCOPE, "kr-1"),
        SCOPE,
        "KR",
        "005380",
        "BUY",
        "10",
        "70000",
        "700000",
        "KRW",
        date(2026, 8, 10),
    )
    assert seam.match_ledger_trade(hint, (kr,)).status == "matched"
    other_day = seam.parse_alert_hint(
        text.replace("2026-08-10 09:05", "2026-08-11 09:05")
    )
    assert seam.match_ledger_trade(other_day, (kr,)).status == "no_match"


def test_existing_playbook_preview_then_approved_append_writes_one_entry_at_top(
    tmp_path,
):
    root = _root(tmp_path)
    preview = _preview(root)
    assert preview.status == "ready" and preview.action == "append"
    assert preview.relative_path == "personal/US/SPCX.md"
    assert preview.entry_id == f"trade-{_trade().trade_event_id[:8]}-spcx-sell"
    assert REASON.decode() in preview.text and INTERPRETATION in preview.text
    assert "1층: 변경 없음" in preview.text and "2층 원문: 변경 없음" in preview.text
    assert f"기존 3층 항목 1건, 최신 {OLD_ENTRY} 방향 다름" in preview.text
    assert "원장 0 · 주문 0 · 계좌 0 · 프로바이더 0" in preview.text
    assert SCOPE not in preview.text
    before = (root / "personal" / "US" / "SPCX.md").read_bytes()
    assert before == SPCX_DOC.encode()  # preview wrote nothing

    result = _apply(root, preview)
    assert result.status == "applied", result
    assert dict(result.writes) == {
        "playbook_files": 1,
        "ledger": 0,
        "order": 0,
        "account": 0,
        "provider": 0,
    }
    after = (root / "personal" / "US" / "SPCX.md").read_bytes()
    assert result.bytes_written == len(after)
    text = after.decode()
    layer3 = text.split(seam.LAYER3_HEADING, 1)[1]
    assert layer3.startswith(
        f'\n\n{seam.LAYER3_PREAMBLE}\n\n<a id="entry-{preview.entry_id}"></a>\n'
    )
    assert layer3.index(f'entry-{preview.entry_id}"') < layer3.index(
        f'entry-{OLD_ENTRY}"'
    )
    assert (
        text.split(seam.LAYER3_HEADING, 1)[0]
        == SPCX_DOC.split(seam.LAYER3_HEADING, 1)[0]
    )
    assert seam.extract_raw_bytes(after, OLD_ENTRY) == "첫 매수 이유".encode()
    assert seam.extract_raw_bytes(after, preview.entry_id) == REASON
    assert f"- `v3_trade_event_id`: `{_trade().trade_event_id}`" in text
    assert (
        "- `event_date`: `2026-08-10`" in text
        and "- `recorded_date`: `2026-09-03`" in text
    )
    assert INTERPRETATION not in text  # interpretation is preview-only on append
    assert _validator()(root) == []


def test_missing_playbook_creates_canonical_first_trade_file(tmp_path):
    root = _root(tmp_path, with_spcx=False)
    preview = _preview(root)
    assert preview.status == "ready" and preview.action == "create"
    assert preview.file_digest == "absent" and "신규 파일 생성" in preview.text
    assert "1층: 위 해석을 헤르 해석 라벨로 기록" in preview.text
    assert "USER_THESIS 구역을 만들지 않음" in preview.text
    assert preview.episode_proposal == "new_episode"
    assert not (root / "personal" / "US" / "SPCX.md").exists()

    result = _apply(root, preview)
    assert result.status == "applied", result
    path = root / "personal" / "US" / "SPCX.md"
    written = path.read_bytes()
    assert written == preview.document and (path.stat().st_mode & 0o777) == 0o644
    text = written.decode()
    assert text.startswith(
        "---\nschema_version: 1\ndocument_kind: instrument\n"
        'playbook_id: "personal:US:SPCX"\nportfolio_scope: personal\n'
        'market: US\ninstrument_code: "SPCX"\ninstrument_name: "스페이스엑스"\n'
    )
    assert text.count(seam.HERMES_START) == 1 and text.count(seam.HERMES_END) == 1
    assert (
        "입력된 헤르 해석(대화에서 제공, AI 자동 생성 아님)" in text
        and INTERPRETATION in text
    )
    assert seam.THESIS_START not in text and seam.NO_THESIS in text
    assert f"[`{preview.entry_id}`](#entry-{preview.entry_id})" in text
    assert seam.extract_raw_bytes(written, preview.entry_id) == REASON
    assert text.index(seam.LAYER3_HEADING) < text.index(
        f'<a id="entry-{preview.entry_id}"></a>'
    )
    assert _validator()(root) == []
    assert (
        read_instrument_name(root, account_scope=SCOPE, market="US", symbol="SPCX")
        == "스페이스엑스"
    )
    unnamed = _preview(
        root,
        _trade("ord-777", symbol="NFLX"),
        instrument_name=None,
        hermes_interpretation=None,
    )
    assert seam.NO_INTERPRETATION in unnamed.document.decode()
    assert 'instrument_name: "' not in unnamed.document.decode()
    assert seam.NO_NAME in unnamed.document.decode()


def test_creation_never_invents_a_thesis_and_keeps_supplied_thesis_bytes(tmp_path):
    root = _root(tmp_path, with_spcx=False)
    thesis = "  장기  보유 종목임. 오타도 그대로\n".encode()
    with_thesis = _preview(root, user_thesis=thesis)
    text = with_thesis.document.decode()
    assert text.count(seam.THESIS_START) == 1 and 'thesis_as_of: "2026-09-03"' in text
    assert (
        f"- `recorded_date`: `2026-09-03`\n\n{thesis.decode()}\n{seam.THESIS_END}"
        in text
    )
    assert seam.ANCHORS_START not in text and seam.NO_THESIS not in text
    assert "[플레이북 정본](../../README.md)" in text
    assert "오타도 그대로 둔다" not in seam.THESIS_PREAMBLE
    assert _apply(root, with_thesis).status == "applied" and _validator()(root) == []
    (root / "personal" / "US" / "SPCX.md").unlink()
    without = _preview(root)
    text = without.document.decode()
    assert seam.THESIS_START not in text and seam.NO_THESIS in text
    assert text.count(REASON.decode().strip()) == 1  # reason lives only in USER_RAW
    assert seam.extract_raw_bytes(without.document, without.entry_id) == REASON
    assert (
        _preview(root, user_thesis=b"<!-- USER_RAW_START x").status
        == "thesis_reason_reserved_marker"
    )


def test_reason_bytes_are_preserved_exactly_and_unsafe_bytes_are_rejected(tmp_path):
    root = _root(tmp_path)
    odd = "오타  그대로\t탭도\n\n마지막 줄 공백  \n".encode()
    preview = _preview(root, reason=odd)
    assert preview.status == "ready" and preview.reason_digest == seam._digest(odd)
    assert _apply(root, preview, ledger=(_trade(),)).status == "applied"
    assert (
        seam.extract_raw_bytes(
            (root / "personal" / "US" / "SPCX.md").read_bytes(), preview.entry_id
        )
        == odd
    )
    for raw, token in (
        (b"\xff\xfe", "reason_undecodable"),
        (b"  \n", "reason_empty"),
        (b"a\x00b", "reason_control_chars"),
        (b"a\r\nb", "reason_control_chars"),
        ("문자열", "reason_not_bytes"),
        (b"x" * 16385, "reason_too_long"),
        (b"<!-- HERMES_CURRENT_END -->", "reason_reserved_marker"),
    ):
        assert seam.reason_rejection(raw) == token
        assert _preview(root, reason=raw).status == token
    assert _preview(root, hermes_interpretation="").status == "interpretation_invalid"
    assert _preview(root, instrument_name='a"b').status == "instrument_name_invalid"


def test_stale_file_wrong_token_and_changed_ledger_all_write_zero(tmp_path):
    root = _root(tmp_path)
    path = root / "personal" / "US" / "SPCX.md"
    preview = _preview(root)
    path.write_bytes(SPCX_DOC.encode() + b"\n<!-- touched -->\n")
    touched = path.read_bytes()
    assert _apply(root, preview).status == "stale_file" and path.read_bytes() == touched
    fresh = _preview(root)
    assert fresh.approval_token != preview.approval_token
    assert (
        _apply(root, fresh, token=preview.approval_token).status == "approval_mismatch"
    )
    assert _apply(root, fresh, token="").status == "approval_mismatch"
    assert _apply(root, fresh, ledger=()).status == "ledger_stale"
    assert _apply(root, fresh, ledger=(_trade(price="90.6"),)).status == "ledger_stale"
    assert path.read_bytes() == touched
    rejected = _preview(root, reason=b"\xff")
    assert _apply(root, rejected).status == "preview_not_ready"
    assert _apply(root, fresh).status == "applied"


def test_replay_is_idempotent_and_a_different_reason_conflicts(tmp_path):
    root = _root(tmp_path)
    path = root / "personal" / "US" / "SPCX.md"
    preview = _preview(root)
    assert _apply(root, preview).status == "applied"
    once = path.read_bytes()
    replay = _apply(root, preview)
    assert replay.status == "already_recorded" and replay.bytes_written == 0
    assert path.read_bytes() == once
    again = _preview(root)
    assert again.status == "already_recorded" and again.approval_token is None
    assert again.document is None
    assert _preview(root, reason=b"other reason").status == "entry_conflict"
    assert path.read_bytes() == once
    # anchor, entry_id field, USER_RAW_START, USER_RAW_END — and nothing more
    assert once.count(preview.entry_id.encode()) == 4


def test_validator_failure_after_write_restores_bytes_or_removes_new_file(tmp_path):
    root = _root(tmp_path)
    real = _validator()

    def flaky():
        calls = []

        def validator(tree):  # passes before the write and after the restore
            calls.append(tree)
            return ["injected failure"] if len(calls) == 2 else real(tree)

        return validator

    preview = _preview(root)
    result = _apply(root, preview, validator=flaky())
    assert result.status == "validator_failed_after" and result.bytes_written == 0
    assert (root / "personal" / "US" / "SPCX.md").read_bytes() == SPCX_DOC.encode()
    created = _preview(
        root, _trade("ord-555", symbol="NFLX"), instrument_name="넷플릭스"
    )
    result = _apply(
        root, created, ledger=(_trade("ord-555", symbol="NFLX"),), validator=flaky()
    )
    assert result.status == "validator_failed_after"
    assert not (root / "personal" / "US" / "NFLX.md").exists()
    (root / "personal" / "US" / "broken.md").write_text(
        "no markers\n", encoding="utf-8"
    )
    assert _apply(root, _preview(root)).status == "validator_failed_before"
    assert (root / "personal" / "US" / "SPCX.md").read_bytes() == SPCX_DOC.encode()


def test_episode_proposal_and_closed_episode_hard_boundary(tmp_path):
    root = _root(tmp_path)
    same_side = _preview(root, _trade("ord-2", side="BUY"))
    assert same_side.episode_proposal == f"same_episode_candidate:{OLD_ENTRY}"
    assert "확인 전 자동 묶음 없음" in same_side.text
    assert _preview(root).episode_proposal == "new_episode"
    refused = _preview(
        root,
        episode_decision=f"join:{OLD_ENTRY}",
        closed_episode_entry_ids=frozenset({OLD_ENTRY}),
    )
    assert refused.status == "episode_closed_refused" and refused.approval_token is None
    assert (
        _preview(root, episode_decision="join:nope").status == "episode_target_missing"
    )
    assert _preview(root, episode_decision="merge").status == "episode_decision_invalid"
    joined = _preview(root, episode_decision=f"join:{OLD_ENTRY}")
    assert joined.status == "ready" and f"결정: join:{OLD_ENTRY}" in joined.text
    assert joined.approval_token != _preview(root).approval_token
    assert (root / "personal" / "US" / "SPCX.md").read_bytes() == SPCX_DOC.encode()


def test_seam_reads_the_canonical_ledger_and_owns_no_write_path(tmp_path):
    source = (ROOT / "src" / "tokmo" / "programs" / "trade_reason_seam.py").read_text()
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module.startswith("tokmo")
    }
    assert imports == {
        "tokmo.records.durability",
        "tokmo.records.playbook_identity",
        "tokmo.values.material_pack",
        "tokmo.values.session_gate",
        "tokmo.values.trade_event",
    }
    for banned in (
        "append_batch",
        "urlopen",
        "sqlite3",
        "execution_dialogue",
        "telegram",
        "/api/v1",
        "record_reason_append",
        "subprocess",
        "conditional",
        "getUpdates",
        "sendMessage",
    ):
        assert banned not in source, banned
    store = TradeLedgerStore(
        tmp_path / "stores" / "trade_ledger.sqlite3",
        clock=lambda: NOW,
        id_factory=lambda: "batch-1",
    )
    event = TradeLedgerEvent(
        source=seam.LEDGER_SOURCE,
        account_scope=SCOPE,
        source_record_key="ord-001",
        event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=date(2026, 8, 9),
        transaction_type_raw="SELL",
        instrument_raw="SPCX",
        quantity="3",
        unit_price="90.5",
        amount="271.5",
        medium_raw="toss-orders",
        currency_raw="USD",
    )
    receipt = store.append_batch(
        (event,), import_audit=ImportAuditMeta(None, "fixture")
    )
    assert receipt.status == "SAVED", receipt
    trades = seam.ledger_trades_from_store(store, account_scope=SCOPE)
    assert trades == (_trade(filled_at=None),)
    assert store.read_scope(source=seam.LEDGER_SOURCE, account_scope=SCOPE) == (event,)
    hint = seam.parse_alert_hint(
        ALERT.replace("12:59", "03:00")
    )  # KST → ET date 2026-08-09
    assert seam.match_ledger_trade(hint, trades).trade == trades[0]
