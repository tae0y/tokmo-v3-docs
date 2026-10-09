#!/usr/bin/env python3
"""Report which credentials tokmo-v3 can find, per module, without ever printing a value.

Every loader in the code base keeps its own file (that is deliberate: each file is 0600 and read by
exactly one adapter). This tool only tells you what exists, what is missing, and which module needs it,
so a new install can be checked in one command:

    .venv/bin/python tools/check_api_keys.py            # human table
    .venv/bin/python tools/check_api_keys.py --json     # machine readable

Exit code 0 when every credential required by an *enabled* module is present, 1 otherwise.
Locations are relative to the project root unless stated. See docs/api-keys.md.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SECRETS = PROJECT / ".secrets"
DATA = PROJECT / "var" / "data"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

# module -> [(display location, path, required keys, note)]
CATALOG = {
    "toss (holdings, orders, pulse, rotation)": [
        (
            ".env.toss",
            PROJECT / ".env.toss",
            ["TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NUMBER"],
            "Toss Securities open API; read + order scope",
        ),
    ],
    "news (collection + decision)": [
        (
            ".secrets/config/tokmo/news.env",
            SECRETS / "config/tokmo/news.env",
            ["FINNHUB_API_KEY", "NAVER_NEWS_CLIENT_ID", "NAVER_NEWS_CLIENT_SECRET"],
            "news connectors",
        ),
        (
            ".secrets/config/tokmo-v3/marketaux.env",
            SECRETS / "config/tokmo-v3/marketaux.env",
            ["MARKETAUX_API_KEY"],
            "optional connector",
        ),
        (
            ".secrets/config/tokmo-v3/thenewsapi.env",
            SECRETS / "config/tokmo-v3/thenewsapi.env",
            ["THENEWSAPI_API_KEY"],
            "optional connector",
        ),
        (
            ".secrets/config/tokmo-v3/opendart.env",
            SECRETS / "config/tokmo-v3/opendart.env",
            ["OPENDART_API_KEY"],
            "Korean disclosures (also quant fundamentals)",
        ),
        (
            ".secrets/config/tokmo-v3/sec.env",
            SECRETS / "config/tokmo-v3/sec.env",
            ["SEC_USER_AGENT"],
            "US filings; a contact string, not a key",
        ),
        (
            "~/.hermes/.env",
            HERMES_HOME / ".env",
            ["GLM_API_KEY|ZAI_API_KEY"],
            "Z.AI GLM: screen + first-pass analysis",
        ),
        (
            ".secrets/hermes-credentials/news_decision_stock_telegram.env",
            SECRETS / "hermes-credentials/news_decision_stock_telegram.env",
            ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
            "stock-bot health notices",
        ),
        (
            ".secrets/hermes-credentials/news_health_telegram.env",
            SECRETS / "hermes-credentials/news_health_telegram.env",
            ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
            "collector health notices",
        ),
        (
            "var/data/news_delivery_route.json",
            DATA / "news_delivery_route.json",
            ["chat_id", "credential_file"],
            "where alerts go (JSON, not a secret)",
        ),
    ],
    "quant + follow-up (ETF context, decisions)": [
        (
            ".env.kis",
            PROJECT / ".env.kis",
            ["KIS_APP_KEY", "KIS_APP_SECRET"],
            "Korea Investment ETF quotes",
        ),
        (
            ".secrets/config/tokmo-v3/krx.env",
            SECRETS / "config/tokmo-v3/krx.env",
            ["KRX_API_KEY"],
            "KRX membership",
        ),
        (
            "~/.hermes/.env",
            HERMES_HOME / ".env",
            ["GLM_API_KEY|ZAI_API_KEY", "TELEGRAM_BOT_TOKEN"],
            "GLM analysis + group delivery bot",
        ),
        (
            "(subscription CLI login)",
            None,
            ["codex login", "claude login"],
            "Codex / Claude CLIs use their own login, no API key is stored",
        ),
    ],
    "macro (FRED, BLS, EIA)": [
        (
            ".secrets/hermes-credentials/fred.env",
            SECRETS / "hermes-credentials/fred.env",
            ["FRED_API_KEY"],
            "",
        ),
        (
            ".secrets/hermes-credentials/bls_ua.env",
            SECRETS / "hermes-credentials/bls_ua.env",
            ["BLS_USER_AGENT"],
            "contact string",
        ),
        (
            ".secrets/config/tokmo-v3/tokmo.env",
            SECRETS / "config/tokmo-v3/tokmo.env",
            ["EIA_API_KEY"],
            "",
        ),
    ],
    "youtube (12man tracker)": [
        ("~/.hermes/.env", HERMES_HOME / ".env", ["YOUTUBE_API_KEY"], ""),
    ],
    "telegram stock-bot (pulse dialogue)": [
        (
            "(shell environment)",
            None,
            ["TELEGRAM_STOCK_BOT", "TELEGRAM_CHAT_ID"],
            "exported by the cron wrappers from your shell profile",
        ),
    ],
}
OPTIONAL = {"MARKETAUX_API_KEY", "THENEWSAPI_API_KEY"}


def keys_in(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    if path.suffix == ".json":
        try:
            return {k for k, v in json.loads(text).items() if v not in (None, "", [])}
        except ValueError:
            return set()
    found = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            if value.strip().strip("'\""):
                found.add(name.strip().removeprefix("export "))
    return found


def mode_ok(path: Path) -> bool:
    try:
        return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0 and not path.is_symlink()
    except OSError:
        return False


def enabled_modules() -> dict:
    def flag(name, key="enabled"):
        try:
            return bool(json.loads((DATA / name).read_text()).get(key))
        except (OSError, ValueError):
            return False

    return {
        "news": flag("news_decision.json"),
        "followup": flag("investment_followup.json"),
        "rotation": (PROJECT / "var/state/hyundai-rotation/live.flag").exists(),
    }


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def model_report() -> dict:
    """Which model (or chain) answers each stage, where that is configured, and whether every id
    and provider is usable here. Never calls a model."""
    sys.path.insert(0, str(PROJECT / "src"))
    from tokmo.values.model_registry import (
        ModelRegistryError,
        codex_catalog,
        known_models,
        load_registry,
        news_chains,
        parse_chain,
        unknown_models,
    )

    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    catalog = known_models(codex_home)
    live = codex_catalog(codex_home)
    codex_source = (
        "built-in list"
        if live is None
        else "~/.codex/models_cache.json (this login's catalog, %d visible)" % len(live)
    )
    rows, problems = [], []
    try:
        registry = load_registry(DATA / "models.json")
    except ModelRegistryError as exc:
        registry, problems = {}, [f"var/data/models.json: {exc}"]
    news = read_json(DATA / "news_decision.json")
    for stage, chain in news_chains(registry, news).items():
        if chain:
            source = (
                "var/data/models.json"
                if registry.get("news", {}).get(stage)
                else "var/data/news_decision.json"
            )
            rows.append(dict(module="news", stage=stage, chain=chain, source=source))
    followup = read_json(DATA / "investment_followup.json")
    for stage, key in (
        ("screen", "screen_model"),
        ("first_pass", "first_pass_model"),
        ("analysis", "analysis_model"),
    ):
        if followup.get(key):
            try:
                rows.append(
                    dict(
                        module="followup",
                        stage=stage,
                        chain=parse_chain(followup[key]),
                        source="var/data/investment_followup.json",
                    )
                )
            except ModelRegistryError as exc:
                problems.append(f"investment_followup.json {key}: {exc}")
    # Providers a CLI must exist for: only the models the configured stages actually select.
    providers = {spec.provider for row in rows for spec in row["chain"]}
    try:
        from quant_decision_models import model_profiles

        rows.append(
            dict(
                module="quant",
                stage="on-demand analysis",
                chain=parse_chain(list(model_profiles())),
                source="MODEL_ROUTING_STATE.md (current product routes; a plan pins one exact ID)",
            )
        )
    except (OSError, ValueError, ImportError) as error:
        problems.append("quant routing unavailable: " + type(error).__name__)
    for row in rows:
        row["unknown"] = (
            []
            if row["module"] == "quant"
            else [spec.name for spec in unknown_models(row["chain"], catalog)]
        )
        row["models"] = [str(spec) for spec in row["chain"]]
        row["selected"] = (
            row["module"] != "quant"
        )  # the quant row lists what a plan may pick; only selected models decide the exit code
        for name in row["unknown"] if row["selected"] else ():
            problems.append(
                f"{row['module']}.{row['stage']}: unknown model id {name} (known {row['module']} ids: see docs/api-keys.md)"
            )
        del row["chain"]
    import shutil

    clis = {
        "codex": shutil.which("codex"),
        "claude": shutil.which("claude")
        or (
            str(Path.home() / ".local/bin/claude")
            if (Path.home() / ".local/bin/claude").exists()
            else None
        ),
    }
    for provider in sorted(providers - {"zai"}):
        if not clis.get(provider):
            problems.append(
                f"provider {provider} is referenced but the {provider} CLI is not installed (login: `{provider} login`)"
            )
    return dict(
        rows=rows,
        providers=sorted(providers),
        codex_catalog=codex_source,
        problems=problems,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows, missing_required = [], 0
    for module, entries in CATALOG.items():
        for location, path, keys, note in entries:
            present = keys_in(path) if path is not None else set()
            env = set(k for k in os.environ if os.environ.get(k))
            for key in keys:
                alternatives = key.split("|")
                have = (
                    any(a in present or a in env for a in alternatives)
                    if path is not None or "login" not in key
                    else None
                )
                status = (
                    "n/a (login)"
                    if have is None
                    else "ok"
                    if have
                    else ("missing (optional)" if key in OPTIONAL else "MISSING")
                )
                if status == "MISSING":
                    missing_required += 1
                rows.append(
                    dict(
                        module=module,
                        location=location,
                        key=key,
                        status=status,
                        mode="ok"
                        if path is None or not path.exists() or mode_ok(path)
                        else "TOO OPEN (chmod 600)",
                        note=note,
                    )
                )
    models = model_report()
    if args.json:
        print(
            json.dumps(
                dict(
                    enabled=enabled_modules(),
                    rows=rows,
                    missing_required=missing_required,
                    models=models,
                ),
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        width = max(len(r["location"]) for r in rows)
        current = None
        for r in rows:
            if r["module"] != current:
                current = r["module"]
                print(f"\n[{current}]")
            flag = "" if r["mode"] == "ok" else f"  <{r['mode']}>"
            print(
                f"  {r['status']:19} {r['location']:{width}}  {r['key']}{flag}   {r['note']}"
            )
        print(
            "\n[models]  (first = preferred; the next one is used only while the previous is out of quota/auth/reachable)"
        )
        for r in models["rows"]:
            state = (
                ("UNKNOWN ID: " if r["selected"] else "not here: ")
                + ", ".join(r["unknown"])
                if r["unknown"]
                else "ok"
            )
            print(
                f"  {state:19} {r['module']}.{r['stage']:22} {' -> '.join(r['models'])}   ({r['source']})"
            )
        print(
            f"  providers referenced: {', '.join(models['providers']) or '-'}  |  codex ids checked against: {models['codex_catalog']}"
        )
        for problem in models["problems"]:
            print(f"  PROBLEM  {problem}")
        print(
            f"\nenabled modules: {enabled_modules()}  |  required missing: {missing_required}  |  model problems: {len(models['problems'])}"
        )
    return 1 if missing_required or models["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
