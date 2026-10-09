"""NEWS-only bridge into the existing common launcher's preflight/READY/run."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from subscription_json_model import SubscriptionInferenceError, _failure

from tokmo.records.quant_decision_reports import atomic_private

# The only (model, reasoning effort) pair the shared launcher admits for NEWS confirmation.
HERMES_ADMITTED = frozenset({("gpt-5.6-sol", "high")})


def run_news(argv, prompt, cwd, timeout, *, diagnostic_path=None):
    """Return the original Codex event stream; caller keeps its existing parser."""
    model = argv[argv.index("--model") + 1]
    if not any(
        model == admitted and "model_reasoning_effort=" + json.dumps(effort) in argv
        for admitted, effort in HERMES_ADMITTED
    ):
        raise SubscriptionInferenceError("ADMISSION")
    schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    launcher = home / "scripts/minimal-continuity-cli"
    diagnostic = (
        Path(diagnostic_path)
        if diagnostic_path is not None
        else (
            Path.home()
            / ".local/share/tokmo-v3/news/decision-worker/inference-diagnostics/analyze-last-error.json"
        )
    )
    root = diagnostic.parent / "admissions"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = "news-" + uuid.uuid4().hex
    output = root / key
    folder = Path(cwd)
    prompt_path = folder / "news-prompt.txt"
    with os.fdopen(
        os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as handle:
        handle.write(prompt)
    request_path = folder / "news-request.json"
    request = dict(
        schema="minimal-launch-request-v1",
        task_key=key,
        purpose="tokmo-news-confirmation",
        workspace=str(folder.resolve()),
        role="analysis",
        provider="codex",
        model=model,
        continuity="fresh",
        prompt_file=str(prompt_path.resolve()),
        output_dir=str(output.resolve()),
        timeout_seconds=timeout,
        max_turns=1,
        response_schema=schema,
        expected_terminal_condition="one validated JSON news judgment",
    )
    atomic_private(request_path, request)
    base = [sys.executable, str(launcher)]
    preflight = subprocess.run(
        base + ["preflight", "--request", str(request_path)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if preflight.returncode:
        try:
            codes = {
                row["code"] for row in json.loads(preflight.stdout).get("issues", [])
            }
        except (ValueError, KeyError, TypeError):
            codes = set()
        # Preserve the last rejection without copying prompts or credentials.
        atomic_private(
            diagnostic.with_name(
                diagnostic.stem.replace("-last-error", "") + "-last-admission.json"
            ),
            dict(
                status="BLOCKED",
                codes=sorted(codes),
                model=model,
                model_api_started=False,
                fallback_used=False,
            ),
        )
        raise SubscriptionInferenceError(
            "QUOTA" if "QUOTA_EXHAUSTED" in codes else "ADMISSION"
        )
    # The common launcher owns the child deadline/cancellation and durable result.
    execution = subprocess.run(
        base
        + [
            "run",
            "--request",
            str(request_path),
            "--ready",
            str(output / "READY.json"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    result_path = output / "RESULT.json"
    if not result_path.exists():
        raise SubscriptionInferenceError("ADMISSION")
    result = json.loads(result_path.read_text())
    atomic_private(
        diagnostic.with_name(
            diagnostic.stem.replace("-last-error", "") + "-last-admission.json"
        ),
        dict(result, result_path=str(result_path)),
    )
    raw = (output / "child.stdout").read_text()
    error = (output / "child.stderr").read_text()
    if execution.returncode or result.get("external_cli_result") != "SUCCEEDED":
        if result.get("model_api_started") == "FALSE":
            raise SubscriptionInferenceError("ADMISSION")
        if result.get("model_api_started") == "UNKNOWN":
            raise TimeoutError(
                "NEWS common launcher did not complete a bounded inference"
            )
        raise _failure(raw, error, execution.returncode, diagnostic_path)
    if (
        result.get("model") != model
        or result.get("fallback_used") is not False
        or result.get("route_admission") != "ADMITTED"
        or result.get("model_api_started") != "TRUE"
    ):
        raise SubscriptionInferenceError("ADMISSION")
    if len(raw) > 2_000_000:
        raise ValueError("subscription response too large")
    return raw
