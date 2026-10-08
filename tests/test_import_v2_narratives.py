from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "playbooks" / "import_v2_narratives.py"


def load_module():
    spec = importlib.util.spec_from_file_location("import_v2_narratives", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bounded_strategy_parser_uses_scalar_text_not_yaml_indentation(tmp_path):
    mod = load_module()
    source = tmp_path / "strategy.yaml"
    source.write_text(
        "stocks:\n"
        "  - 종목명: 현대차\n"
        "    ticker: 005380.KS\n"
        "    market: KR\n"
        "    구분: 보유\n"
        "    플레이북: |\n"
        "      첫 문장.\n"
        "        사용자가 넣은 추가 들여쓰기.\n"
        "      마지막 문장.\n",
        encoding="utf-8",
    )
    rows = mod.parse_strategy_playbooks(source)
    assert len(rows) == 1
    assert rows[0].payload == "첫 문장.\n  사용자가 넣은 추가 들여쓰기.\n마지막 문장.\n"
    assert rows[0].scope == "personal"
    assert rows[0].code == "005380"


def test_trade_json_account_mapping_and_legacy_key(tmp_path):
    mod = load_module()
    source = tmp_path / "trade_history.json"
    source.write_text(json.dumps([{
        "date": "2026-07-09", "type": "매도", "name": "ETF",
        "ticker": "490090.KS", "quantity": 1, "avg_price": 100,
        "total": 100, "reason": "추천 기록 원문", "account": "퇴직연금",
    }], ensure_ascii=False), encoding="utf-8")
    rows, source_sha = mod.parse_trade_reasons(source)
    assert rows[0].scope == "retirement"
    assert rows[0].payload == "추천 기록 원문"
    assert rows[0].code == "490090"
    assert rows[0].legacy_trade_key == mod.legacy_trade_key(source_sha, 1)


def test_unknown_account_is_unresolved_not_personal(tmp_path):
    mod = load_module()
    source = tmp_path / "trade_history.json"
    source.write_text(json.dumps([{
        "date": "2026-07-09", "type": "매수", "name": "ETF",
        "ticker": "490090.KS", "quantity": 1, "avg_price": 100,
        "total": 100, "reason": "원문", "account": "다른계좌",
    }], ensure_ascii=False), encoding="utf-8")
    rows, _ = mod.parse_trade_reasons(source)
    assert rows[0].scope is None


def test_live_verified_legacy_codes_map_to_current_toss_identity(tmp_path):
    mod = load_module()
    source = tmp_path / "trade_history.json"
    source.write_text(json.dumps([
        {"date": "2026-07-09", "type": "매수", "name": "KODEX 현대차로보틱스밸류체인Top2플러스", "ticker": "465110.KS", "quantity": 1, "avg_price": 100, "total": 100, "reason": "원문1", "account": "퇴직연금"},
        {"date": "2026-07-09", "type": "매수", "name": "SOL 화장품TOP3플러스", "ticker": "479850.KS", "quantity": 1, "avg_price": 100, "total": 100, "reason": "원문2", "account": "퇴직연금"},
    ], ensure_ascii=False), encoding="utf-8")
    rows, _ = mod.parse_trade_reasons(source)
    assert [(r.code, r.name) for r in rows] == [
        ("0204D0", "KODEX 현대차로보틱스밸류체인TOP3플러스"),
        ("0008T0", "SOL 화장품TOP3플러스"),
    ]
    assert [(r.legacy_code, r.legacy_name) for r in rows] == [
        ("465110", "KODEX 현대차로보틱스밸류체인Top2플러스"),
        ("479850", "SOL 화장품TOP3플러스"),
    ]


def test_preview_is_sanitized_on_stdout_but_contains_private_payload(tmp_path, capsys):
    mod = load_module()
    preview = tmp_path / "preview.json"
    mod.write_preview(preview, [{"path": "retirement/KR/490090.md", "payload": "비밀 원문"}])
    assert "비밀 원문" in preview.read_text(encoding="utf-8")
    captured = capsys.readouterr().out
    assert "비밀 원문" not in captured
