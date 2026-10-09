"""One bounded inference using an already logged-in subscription CLI.

No project instructions, tools, conversation reuse, fallback or retry loop.
The application owns prompts, input data, validation and call accounting.
"""

import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


class SubscriptionInferenceError(RuntimeError):
    def __init__(self, category, *, exit_code=None, diagnostic_path=None):
        self.category = category
        self.exit_code = exit_code
        self.diagnostic_path = diagnostic_path
        self.inference_retryable = category in {"TRANSIENT", "EXECUTOR_UNKNOWN"}
        self.inference_retry_limit = 2 if category == "EXECUTOR_UNKNOWN" else 3
        super().__init__(
            "subscription inference failed: "
            + category
            + (f" (exit {exit_code})" if exit_code is not None else "")
        )


def _failure(output, error, code, diagnostic_path):
    # Classify transport errors from error events, not successful financial text.
    details = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and (
            event.get("type") in {"error", "turn.failed"} or event.get("is_error")
        ):
            details.append(json.dumps(event, ensure_ascii=False))
    text = ("\n".join(details) + "\n" + error).lower()
    # Codex CLI phrases the subscription cap as "You've hit your usage limit ... purchase more credits" (09-17 outage).
    if any(
        x in text
        for x in (
            "usage_limit_reached",
            "insufficient_quota",
            "credit balance",
            "usage limit exceeded",
            "hit your usage limit",
            "usage limit",
            "purchase more credits",
            "quota exceeded",
        )
    ):
        category = "QUOTA"
    elif any(
        x in text
        for x in (
            "unauthorized",
            "authentication",
            "invalid_api_key",
            "token_expired",
            "refresh token",
            "not logged in",
        )
    ):
        category = "AUTH"
    elif code == 2 or any(
        x in text
        for x in (
            "context_length_exceeded",
            "invalid_request_error",
            "model_not_found",
            "unsupported model",
            "invalid argument",
        )
    ):
        category = "REQUEST"
    elif any(
        x in text
        for x in (
            "rate_limit_exceeded",
            "too many requests",
            "status code 429",
            "status code 500",
            "status code 502",
            "status code 503",
            "status code 504",
            "connection reset",
            "connection closed",
            "stream disconnected",
            "connection refused",
            "temporarily unavailable",
            "internal server error",
            "timed out",
            "error sending request",
            "selected model is at capacity",
        )
    ):
        category = "TRANSIENT"
    else:
        category = "EXECUTOR_UNKNOWN"
    saved = None
    if diagnostic_path is not None:
        # A bounded private artifact keeps the original reason available. The
        # exception and user notices only expose category/code, never this text.
        from tokmo.records.quant_decision_reports import atomic_private

        try:
            atomic_private(
                Path(diagnostic_path),
                dict(
                    at=datetime.now(UTC).isoformat(),
                    category=category,
                    exit_code=code,
                    stdout_tail=output[-32768:],
                    stderr_tail=error[-16384:],
                ),
            )
            saved = str(diagnostic_path)
        except OSError:
            pass
    return SubscriptionInferenceError(category, exit_code=code, diagnostic_path=saved)


def _run(argv, prompt, cwd, timeout, *, diagnostic_path=None):
    env = dict(os.environ)
    # Reuse subscription login; never accidentally select metered API auth.
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        output, error = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise TimeoutError("subscription inference timed out")
    if process.returncode:
        # stdout/stderr can contain private prompts. Keep diagnostics local
        # to the caller rather than incorporating them into public notices.
        raise _failure(output, error, process.returncode, diagnostic_path)
    if len(output) > 2_000_000:
        raise ValueError("subscription response too large")
    return output


def infer(
    prompt,
    *,
    provider,
    model,
    timeout,
    schema=None,
    effort="medium",
    diagnostic_path=None,
    runner=None,
):
    start = time.monotonic()
    run_inference = runner or _run
    with tempfile.TemporaryDirectory(prefix="tokmo-news-inference-") as directory:
        folder = Path(directory)
        if provider == "codex":
            argv = [
                "codex",
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--json",
                "--model",
                model,
                "--cd",
                directory,
                "-c",
                'approval_policy="never"',
                "-c",
                'web_search="disabled"',
                "-c",
                "features.shell_tool=false",
                "-c",
                "features.multi_agent=false",
                "-c",
                "features.plugins=false",
                "-c",
                "features.remote_plugin=false",
                "-c",
                "features.skip_host_skill_discovery=true",
                "-c",
                "model_reasoning_effort=" + json.dumps(effort),
            ]
            if schema:
                path = folder / "output-schema.json"
                path.write_text(json.dumps(schema))
                argv.extend(["--output-schema", str(path)])
            diagnostic_args = (
                {"diagnostic_path": diagnostic_path}
                if diagnostic_path is not None
                else {}
            )
            raw = run_inference(
                argv + ["-"], prompt, directory, timeout, **diagnostic_args
            )
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            messages, usage = [], {}
            for event in events:
                item = event.get("item", {})
                if item.get("type") in (
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "web_search",
                ):
                    raise ValueError("unexpected tool use in inference")
                if event.get("type") in ("turn.failed", "error"):
                    raise _failure(raw, "", None, diagnostic_path)
                if (
                    event.get("type") == "item.completed"
                    and item.get("type") == "agent_message"
                ):
                    messages.append(item["text"])
                if event.get("type") == "turn.completed":
                    usage = event.get("usage", {})
            if not messages:
                raise ValueError("subscription model returned no final message")
            text = messages[-1]
        elif provider == "claude":
            argv = [
                str(Path.home() / ".local/bin/claude"),
                "-p",
                "--model",
                model,
                "--output-format",
                "stream-json",
                "--verbose",
                "--max-turns",
                "1",
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--setting-sources",
                "",
                "--no-session-persistence",
            ]
            diagnostic_args = (
                {"diagnostic_path": diagnostic_path}
                if diagnostic_path is not None
                else {}
            )
            raw = run_inference(argv, prompt, directory, timeout, **diagnostic_args)
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            for event in events:
                if event.get("type") == "assistant" and any(
                    c.get("type") == "tool_use"
                    for c in event.get("message", {}).get("content", [])
                ):
                    raise ValueError("unexpected tool use in inference")
            final = next(
                (e for e in reversed(events) if e.get("type") == "result"), None
            )
            if not final or final.get("is_error") or final.get("subtype") != "success":
                raise _failure(raw, "", None, diagnostic_path)
            text = final.get("result")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("subscription model returned no final message")
            usage = final.get("usage", {})
            # Claude separates cache reads/writes from uncached input.
            if type(usage.get("input_tokens")) is int:
                usage = dict(
                    usage,
                    input_tokens=sum(
                        usage.get(k, 0)
                        for k in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    ),
                )
            if type(usage.get("cache_read_input_tokens")) is int:
                usage["cached_input_tokens"] = usage["cache_read_input_tokens"]
        else:
            raise ValueError("unknown subscription provider")
        # Missing usage is unknown, not a measured zero. Cache/reasoning counts
        # are subsets of input/output and must not be added to total again.
        tokens = {
            key: usage[field]
            for key, field in [
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
            ]
            if type(usage.get(field)) is int and usage[field] >= 0
        }
        if "prompt_tokens" in tokens and "completion_tokens" in tokens:
            tokens["total_tokens"] = (
                tokens["prompt_tokens"] + tokens["completion_tokens"]
            )
        for field, detail, name, total in (
            (
                "cached_input_tokens",
                "prompt_tokens_details",
                "cached_tokens",
                "prompt_tokens",
            ),
            (
                "reasoning_output_tokens",
                "completion_tokens_details",
                "reasoning_tokens",
                "completion_tokens",
            ),
        ):
            value = usage.get(field)
            nested = usage.get(
                "input_tokens_details"
                if total == "prompt_tokens"
                else "output_tokens_details"
            )
            if value is None and isinstance(nested, dict):
                value = nested.get(name)
            if type(value) is int and 0 <= value <= tokens.get(total, -1):
                tokens[detail] = {name: value}
        return text, dict(
            _usage=tokens,
            _provider=provider,
            _model=model,
            _reasoning_effort=effort,
            _elapsed_seconds=round(time.monotonic() - start, 3),
        )
