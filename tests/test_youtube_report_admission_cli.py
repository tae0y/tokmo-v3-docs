"""Strict-TDD RED suite: fail-closed CLI seam for 12man report admission.

``tools/run_youtube_report_admission.py`` is the repo-managed operator/cron
edge: explicit ``--meta``/``--report``/``--db`` inputs, private-file safety
via the existing private-store rules, and one bounded JSON read-back line
on stdout. Every refusal must happen BEFORE the ops-kernel store is opened,
so a refused run leaves no database behind.

All fixtures are synthetic and live under ``tmp_path``.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "run_youtube_report_admission.py"

META = {
    "video_id": "YlVhQO-Fkc8",
    "url": "https://www.youtube.com/watch?v=YlVhQO-Fkc8",
    "title": "[12시에 만나요] fixture",
    "published": "2026-08-13T04:21:51+00:00",
    "fetched_at": "2026-08-13T05:15:02+00:00",
    "transcript_sha256": "a" * 64,
    "extraction": "innertube-android-srv3-v1",
    "caption_kind": "ko-asr",
}

REPORT = """=== TRACKING_CANDIDATE BEGIN ===
STATEMENT: 정상 형제 블록 가설
TRIGGER: EVENT_OCCUR | entity:005380:stake_change
SUPPORT: yt:2026-08-13:00:14
INVALIDATE: disclosure:005380:exit
NEXT_CHECK: none
TOUCH: PERSONAL
KEYS: entity:005380
=== TRACKING_CANDIDATE END ==="""


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "run_youtube_report_admission_under_test",
        TOOL,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def private_inputs(tmp_path):
    """Private-mode input files plus a db path whose parent does not exist."""
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700)
    meta_path = inputs / "video.meta.json"
    meta_path.write_text(json.dumps(META), encoding="utf-8")
    meta_path.chmod(0o600)
    report_path = inputs / "report.txt"
    report_path.write_text(REPORT, encoding="utf-8")
    report_path.chmod(0o600)
    db_dir = tmp_path / "store"
    return meta_path, report_path, db_dir / "ops_kernel.sqlite3"


def run_tool(argv, capsys):
    code = load_tool().main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_happy_path_emits_one_bounded_json_line(private_inputs, capsys):
    meta_path, report_path, db_path = private_inputs
    code, out, err = run_tool(
        [
            "--meta",
            str(meta_path),
            "--report",
            str(report_path),
            "--db",
            str(db_path),
        ],
        capsys,
    )
    assert code == 0, err
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["source_ref"] == "YlVhQO-Fkc8"
    assert payload["admission"]["created"] == 1
    assert payload["read_back"]["source_revision_registered"] is True
    assert db_path.exists()
    for forbidden in ("/Users", "tokmo", str(db_path), "hypothesis:"):
        assert forbidden not in lines[0]


def test_replay_run_reports_duplicate(private_inputs, capsys):
    meta_path, report_path, db_path = private_inputs
    argv = [
        "--meta",
        str(meta_path),
        "--report",
        str(report_path),
        "--db",
        str(db_path),
    ]
    assert run_tool(argv, capsys)[0] == 0
    code, out, _err = run_tool(argv, capsys)
    assert code == 0
    payload = json.loads(out.strip())
    assert payload["admission"]["created"] == 0
    assert payload["admission"]["duplicates"] == 1


def test_missing_report_refuses_before_db_creation(private_inputs, capsys):
    meta_path, report_path, db_path = private_inputs
    report_path.unlink()
    code, out, err = run_tool(
        [
            "--meta",
            str(meta_path),
            "--report",
            str(report_path),
            "--db",
            str(db_path),
        ],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert "refusal=" in err
    assert not db_path.parent.exists()


def test_non_private_report_mode_refuses_before_db_creation(private_inputs, capsys):
    meta_path, report_path, db_path = private_inputs
    report_path.chmod(0o644)
    code, out, err = run_tool(
        [
            "--meta",
            str(meta_path),
            "--report",
            str(report_path),
            "--db",
            str(db_path),
        ],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert not db_path.parent.exists()


def test_symlinked_meta_refuses_before_db_creation(private_inputs, tmp_path, capsys):
    meta_path, report_path, db_path = private_inputs
    link = meta_path.parent / "linked.meta.json"
    link.symlink_to(meta_path)
    code, out, _err = run_tool(
        [
            "--meta",
            str(link),
            "--report",
            str(report_path),
            "--db",
            str(db_path),
        ],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert not db_path.parent.exists()


def test_malformed_meta_refuses_before_db_creation(private_inputs, capsys):
    meta_path, report_path, db_path = private_inputs
    broken = dict(META)
    del broken["transcript_sha256"]
    meta_path.write_text(json.dumps(broken), encoding="utf-8")
    code, out, err = run_tool(
        [
            "--meta",
            str(meta_path),
            "--report",
            str(report_path),
            "--db",
            str(db_path),
        ],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert not db_path.parent.exists()


def test_meta_that_is_not_json_refuses_before_db_creation(private_inputs, capsys):
    meta_path, report_path, db_path = private_inputs
    meta_path.write_text("not json at all", encoding="utf-8")
    code, out, _err = run_tool(
        [
            "--meta",
            str(meta_path),
            "--report",
            str(report_path),
            "--db",
            str(db_path),
        ],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert not db_path.parent.exists()


def test_source_mismatch_refuses_before_db_creation(private_inputs, capsys):
    meta_path, report_path, db_path = private_inputs
    code, out, err = run_tool(
        [
            "--meta",
            str(meta_path),
            "--report",
            str(report_path),
            "--db",
            str(db_path),
            "--expect-source-ref",
            "someOtherVid",
        ],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert "source_mismatch" in err
    assert not db_path.parent.exists()


def test_missing_required_arguments_exit_with_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        load_tool().main([])
    assert exit_info.value.code == 2
