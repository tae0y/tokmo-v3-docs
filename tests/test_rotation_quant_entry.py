import json
import subprocess
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from test_rotation_analysis import DEADLINE, NOW, report, snapshot

from tokmo.programs.rotation_quant_entry import ExistingQuantAnalyzer


def request_snapshot():
    from test_rotation_analysis import build_rotation_request, context, policy

    return build_rotation_request(snapshot(), policy(), context(), now=NOW)


def test_adapter_calls_existing_quant_entry_with_fixed_astra_and_one_request(tmp_path):
    calls = []

    def launch(args, *, deadline):
        calls.append((args, deadline))
        output = Path(args[args.index("--output") + 1])
        value = report()
        (output / (value["report_id"] + ".json")).write_text(json.dumps(value))
        return 0

    analyzer = ExistingQuantAnalyzer(
        python="/python",
        entry=tmp_path / "run_quant_decisions.py",
        root=tmp_path / "runs",
        launch=launch,
    )
    result = analyzer(request_snapshot(), DEADLINE)
    args, deadline = calls[0]
    assert args[:2] == ["/python", str(tmp_path / "run_quant_decisions.py")]
    assert args[args.index("--model") + 1] == "codex:gpt-6-astra"
    assert args[args.index("--max-model-calls") + 1] == "2"
    assert (
        "--snapshot" in args and "--request-only" in args and "--periodic" not in args
    )
    assert deadline == DEADLINE and result["report_id"] == report()["report_id"]
    with pytest.raises(FileExistsError):
        analyzer(request_snapshot(), DEADLINE)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["missing", "duplicate", "failed"])
def test_adapter_refuses_missing_ambiguous_or_failed_output(tmp_path, mode):
    def launch(args, *, deadline):
        output = Path(args[args.index("--output") + 1])
        if mode == "duplicate":
            for ident in ("a", "b"):
                (output / f"qd-{ident * 16}.json").write_text("{}")
        return 2 if mode == "failed" else 0

    analyzer = ExistingQuantAnalyzer(
        python="/python", entry=tmp_path / "entry", root=tmp_path, launch=launch
    )
    with pytest.raises(ValueError):
        analyzer(request_snapshot(), DEADLINE)


def test_quant_symbol_flag_follows_the_single_analysis_request(tmp_path):
    data = request_snapshot()
    data["analysis_requests"][0]["key"] = "KR:000660"
    calls = []

    def launch(args, *, deadline):
        calls.append(args)
        output = Path(args[args.index("--output") + 1])
        value = report()
        (output / (value["report_id"] + ".json")).write_text(json.dumps(value))
        return 0

    ExistingQuantAnalyzer(
        python="/python", entry=tmp_path / "entry", root=tmp_path, launch=launch
    )(data, DEADLINE)
    assert calls[0][calls[0].index("--symbol") + 1] == "KR:000660"


def test_missing_request_does_not_fall_back_to_hyundai(tmp_path):
    with pytest.raises(ValueError, match="quant_request_scope"):
        ExistingQuantAnalyzer(
            python="/python",
            entry=tmp_path / "entry",
            root=tmp_path,
            launch=lambda *a, **k: pytest.fail("unexpected launch"),
        )(snapshot(), DEADLINE)


def test_process_deadline_stops_child_and_does_not_retry(tmp_path):
    import sys
    from datetime import datetime

    from tokmo.programs.rotation_quant_entry import launch_until

    with pytest.raises(TimeoutError):
        launch_until(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            deadline=datetime.now(UTC) + timedelta(milliseconds=80),
        )


def test_existing_cli_runs_recorded_full_report_without_live_model(tmp_path):
    import sys

    from test_rotation_analysis import build_rotation_request, context, policy

    entry = Path(__file__).parents[1] / "tools/run_quant_decisions.py"
    capture = tmp_path / "snapshot.json"
    responses = tmp_path / "responses.json"
    capture.write_text(
        json.dumps(build_rotation_request(snapshot(), policy(), context(), now=NOW))
    )
    responses.write_text(
        json.dumps({"personal-a:KR:005380": report()["results"][0]["analysis"]})
    )
    output = tmp_path / "output"
    result = subprocess.run(
        [
            sys.executable,
            str(entry),
            "--snapshot",
            str(capture),
            "--request-only",
            "--model",
            "recorded",
            "--responses",
            str(responses),
            "--new-limit",
            "0",
            "--symbol",
            "KR:005380",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    files = [p for p in output.glob("qd-*.json") if p.name.count(".") == 1]
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    row = saved["results"][0]
    assert len(saved["results"]) == 1 and row["status"] == "ANALYZED"
    assert (
        row["selected_option"]["quantity"] == "1"
        and row["analysis"]["price_proposal"]["entry_price"] == "380000"
    )
