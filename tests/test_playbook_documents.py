from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "playbooks" / "validate_playbooks.py"


def load_module():
    spec = importlib.util.spec_from_file_location("validate_playbooks", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_raw_block_round_trip_preserves_final_newline_and_content():
    mod = load_module()
    for payload in ["한 줄", "한 줄\n", "첫 줄\n  들여쓴 내용 \n"]:
        rendered = mod.render_raw_block("entry-1", payload)
        assert mod.extract_raw_block(rendered, "entry-1") == payload


def test_raw_block_normalizes_crlf_transport_only():
    mod = load_module()
    rendered = mod.render_raw_block("entry-1", "첫 줄\r\n둘째 줄\r\n")
    assert mod.extract_raw_block(rendered, "entry-1") == "첫 줄\n둘째 줄\n"


def test_reserved_marker_is_rejected():
    mod = load_module()
    with pytest.raises(ValueError, match="reserved marker"):
        mod.render_raw_block("entry-1", "<!-- USER_RAW_END")


def test_validator_accepts_document_and_rejects_broken_anchor(tmp_path):
    mod = load_module()
    playbooks = tmp_path / "playbooks"
    target = playbooks / "personal" / "KR" / "005380.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        mod.render_instrument_document(
            scope="personal",
            market="KR",
            code="005380",
            name="현대차",
            entries=[
                mod.TimelineEntry(
                    entry_id="strategy-1",
                    heading_date="2026-07-21",
                    heading="V2 최신 플레이북 이관",
                    source_kind="v2_strategy_playbook",
                    source_ref="strategy_advisor.yaml@abc",
                    payload="원문\n",
                    event_date="unknown",
                    recorded_date="unknown",
                    source_snapshot_date="2026-07-21",
                    source_snapshot_basis="latest_git_commit_touching_source",
                )
            ],
        ),
        encoding="utf-8",
    )
    assert mod.validate_tree(playbooks) == []

    text = target.read_text(encoding="utf-8").replace(
        "#entry-strategy-1", "#entry-missing"
    )
    target.write_text(text, encoding="utf-8")
    assert any("missing anchor" in error for error in mod.validate_tree(playbooks))


def test_readme_is_exempt(tmp_path):
    mod = load_module()
    playbooks = tmp_path / "playbooks"
    playbooks.mkdir()
    (playbooks / "README.md").write_text("# docs\n", encoding="utf-8")
    assert mod.validate_tree(playbooks) == []
