#!/usr/bin/env python3
"""Fixed-path, non-recurring Quant canary composition entrypoint."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from tokmo.programs.quant_canary import CampaignFailure

CANONICAL_PROJECT_ROOT = Path.home() / "tokmo" / "tokmo-v3" / "projects" / "tokmo-v3"
CANONICAL_DATA_ROOT = Path(__file__).resolve().parents[1] / "var" / "data"
CANONICAL_CREDENTIAL = CANONICAL_PROJECT_ROOT / ".env.toss"
CALENDAR_CACHE = CANONICAL_DATA_ROOT / "ops" / "calendar_cache.json"
SESSION_HISTORY = CANONICAL_DATA_ROOT / "ops" / "regular_session_history.sqlite3"
CURRENT_HOLDINGS = CANONICAL_DATA_ROOT / "stores" / "current_holdings.json"
PENSION_REGISTRY = CANONICAL_DATA_ROOT / "stores" / "pension_registry.sqlite3"
CANARY_ROOT = CANONICAL_DATA_ROOT / "ops" / "quant-g2-preview"
OUTPUT_FIELDS = {"campaign_status", "total_starts", "counts", "products", "local", "rate_remaining"}
COUNT_FIELDS = {"TOKEN:NONE", "RANKINGS:KR", "RANKINGS:US", "STOCKS:KR", "STOCKS:US",
                "STOCK_CANDLES:KR", "STOCK_CANDLES:US"}
OUTCOMES = {"SUCCEEDED_WITH_CARDS", "SUCCEEDED_SILENT", "NO_DATA", "PENDING"}
WIRE_FAILURE_REASONS = {"http_429", "auth_rejection", "forbidden", "bad_request", "http_5xx",
                        "timeout", "redirect"}
PULSE_RUNTIME_ROOT = Path.home() / ".hermes" / "runtime" / "tokmo-v3" / "pulse"


def _completed_pulse_receipt(root, now):
    slot = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")
    path = Path(root) / slot / "manifest.json"
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(row, dict):
        return False
    return (row.get("job_id") == "tokmo-v3-pulse-" + slot
            and row.get("status") == "SUCCEEDED"
            and row.get("child_exit_code") == 0
            and row.get("timed_out") is False)


def wait_for_pulse_safe_window(root=PULSE_RUNTIME_ROOT, *, clock=None,
                               monotonic=None, sleeper=None,
                               timeout_seconds=70.0, min_headroom_seconds=30.0):
    clock = clock or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or time.monotonic
    sleeper = sleeper or time.sleep
    deadline = monotonic() + max(0.0, timeout_seconds)
    while True:
        now = clock()
        if now.tzinfo is None:
            return False
        headroom = 60.0 - now.second - now.microsecond / 1_000_000
        if headroom >= min_headroom_seconds and _completed_pulse_receipt(root, now):
            return True
        if monotonic() >= deadline:
            return False
        sleeper(0.1)


def _wire_failure_reason(code):
    if code in WIRE_FAILURE_REASONS:
        return code
    if code == "connection_failure":
        return "connection"
    return "protocol"


def _validate_public_result(result):
    if set(result) != OUTPUT_FIELDS:
        raise ValueError("output schema refused")
    if result["campaign_status"] not in {"SUCCEEDED", "REPLAYED", "PARTIAL"}:
        raise ValueError("output schema refused")
    if (type(result["total_starts"]) is not int or not 0 <= result["total_starts"] <= 9
            or type(result["rate_remaining"]) is not int or not 0 <= result["rate_remaining"] <= 9
            or (result["campaign_status"] != "PARTIAL" and result["total_starts"] + result["rate_remaining"] != 9)
            or (result["campaign_status"] == "PARTIAL" and result["rate_remaining"] != 0)):
        raise ValueError("output schema refused")
    if (not isinstance(result["counts"], dict) or set(result["counts"]) - COUNT_FIELDS or not all(
            type(v) is int and 0 <= v <= 2 for v in result["counts"].values())):
        raise ValueError("output schema refused")
    product_fields = {"outcome", "cards", "report_readback", "receipt_readback", "replay_zero"}
    if set(result["products"]) != {"KR", "US"} or not all(
            isinstance(v, dict) and set(v) == product_fields
            and v["outcome"] in OUTCOMES and type(v["cards"]) is int and 0 <= v["cards"] <= 2
            and all(type(v[k]) is bool for k in ("report_readback", "receipt_readback", "replay_zero"))
            for v in result["products"].values()):
        raise ValueError("output schema refused")
    local_fields = {"personal_present", "personal_projected", "pension_present", "pension_projected"}
    if set(result["local"]) != local_fields or not all(
            type(result["local"][k]) is (bool if k.endswith("present") else int) for k in local_fields):
        raise ValueError("output schema refused")


def build_production_runner(*, auth_handler=None, reads_handler=None, clock=None,
                            pulse_gate=wait_for_pulse_safe_window, _test_context=None):
    """Build the real fixed-path composition; handlers are the permanent offline seam."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    from tokmo.facts.toss import build_auth_request, parse_token_response
    from tokmo.facts.toss_reads import parse_read_response
    from tokmo.facts.toss_auth_live import TossAuthLiveTransport
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport
    from tokmo.programs.market_regime_shadow import _resolve_market_session
    from tokmo.programs.quant_canary import open_preview_campaign, run_bounded_canary
    from tokmo.programs.quant_discovery_runtime import admit_ranking_candidates, project_personal_symbols, project_pension_symbols, run_quant_once
    from tokmo.records.calendar_cache import CalendarCacheStore
    from tokmo.records.current_holdings_store import CurrentHoldingsStore
    from tokmo.records.pension_revisions import PensionRegistryStore
    from tokmo.records.regular_session_history import RegularSessionHistoryStore
    from tokmo.values.quant_discovery import QuantMarketProfileV1
    from tokmo.values.session_gate import Market
    from tokmo.composition import _require_private_credential_file, load_toss_config

    if _test_context is None and pulse_gate() is not True:
        raise ValueError("pulse token window refused")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None: raise ValueError("clock must be aware")
    if _test_context is None:
        _require_private_credential_file(CANONICAL_CREDENTIAL)
        config = load_toss_config(CANONICAL_CREDENTIAL)
        cache = CalendarCacheStore(CALENDAR_CACHE, clock=lambda: now).read()
        history = RegularSessionHistoryStore(SESSION_HISTORY, clock=lambda: now)
        sessions = {name: _resolve_market_session(market=market, scheduled_for_utc=now, history=history, cache=cache)
                    for name, market in (("KR", Market.KR), ("US", Market.US))}
        holdings = CurrentHoldingsStore(CURRENT_HOLDINGS, clock=lambda: now).read()
        pension = PensionRegistryStore(PENSION_REGISTRY, clock=lambda: now).read_active_revision_ro()
        campaign_root = CANARY_ROOT
    else:
        if set(_test_context) != {"config", "sessions", "holdings", "pension", "campaign_root"}:
            raise ValueError("invalid offline production seam")
        config, sessions = _test_context["config"], _test_context["sessions"]
        holdings, pension, campaign_root = _test_context["holdings"], _test_context["pension"], _test_context["campaign_root"]
    if any(value is None for value in sessions.values()): raise ValueError("session preflight refused")
    if now.astimezone(ZoneInfo("Asia/Seoul")).time().isoformat() < "06:00:00": raise ValueError("US session boundary refused")

    identities = {}
    for name, value in sessions.items():
        scalars = ["MARKET_DISCOVERY", name, value.isoformat(), "quant-profile-v1", None, "quant-report-v1"]
        identities[name] = __import__("hashlib").sha256(
            json.dumps(scalars, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    campaign = open_preview_campaign(campaign_root, sessions=sessions, identities=identities,
                                     _test_policy_roots=(() if _test_context is not None else None))
    auth_raw = TossAuthLiveTransport(https_handler=auth_handler)
    reads_raw = TossReadsLiveTransport(https_handler=reads_handler)
    token_box = {}
    def require_wire(reply):
        if not hasattr(reply, "body"):
            raise CampaignFailure(_wire_failure_reason(getattr(reply, "code", None)))
        return reply
    def auth():
        reply = require_wire(auth_raw(build_auth_request(client_id=config.client_id, client_secret=config.client_secret)))
        try: parsed = parse_token_response(reply.body, observed_at=now)
        except Exception as exc: raise CampaignFailure("parser") from exc
        if not hasattr(parsed, "value"): raise CampaignFailure("contract")
        token_box["token"] = parsed.value
        return True
    def market(name, metered):
        profile = QuantMarketProfileV1(name)
        parsed = {}
        def validate(request, reply):
            reply = require_wire(reply)
            try: outcome = parse_read_response(req=request, body=reply.body, fetched_at=now)
            except Exception as exc: raise CampaignFailure("parser") from exc
            if outcome.code != "success": raise CampaignFailure("contract")
            parsed[request.operation.value] = outcome.value
            if request.operation.value == "STOCKS":
                funnel = admit_ranking_candidates(profile, parsed["RANKINGS"], outcome.value)
                targets = tuple(candidate.symbol for candidate in funnel.candidates)
                campaign.bind_admitted(name, targets,
                    candidate_digest=__import__("hashlib").sha256("\0".join(request.symbols).encode()).hexdigest(),
                    admitted_digest=__import__("hashlib").sha256("\0".join(targets).encode()).hexdigest())
        def transport(request):
            operation = request.operation.value
            if operation == "STOCKS":
                campaign.bind_candidates(name, request.symbols,
                    candidate_digest=__import__("hashlib").sha256("\0".join(request.symbols).encode()).hexdigest())
            return metered(operation, name, lambda: reads_raw(request),
                cardinality=len(request.symbols) if operation == "STOCKS" else None,
                target=request.symbol if operation == "STOCK_CANDLES" else None,
                validator=lambda reply: validate(request, reply))
        deps = SimpleNamespace(root=campaign.report_root, token=token_box["token"], transport=transport,
            clock=lambda: now, profiles=(profile,), completed_sessions={profile: sessions[name]},
            profile_revision="quant-profile-v1", report_revision="quant-report-v1", shortlist_cap=2,
            candle_start_cap=2, personal_symbols=(), pension_revision=None, reviewed_peers=())
        outcome = run_quant_once(deps=deps, now=now)
        return {"outcome": outcome.receipt.outcome, "cards": len(outcome.report.cards),
                "report_readback": True, "receipt_readback": True,
                "replay_zero": outcome.replay_counts == (("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0))}
    def replay_market(name, expected_identity):
        profile = QuantMarketProfileV1(name)
        deps = SimpleNamespace(root=campaign.report_root, token=None,
            transport=lambda *_: (_ for _ in ()).throw(ValueError("replay transport disabled")),
            clock=lambda: (_ for _ in ()).throw(ValueError("replay clock disabled")),
            profiles=(profile,), completed_sessions={profile: sessions[name]},
            profile_revision="quant-profile-v1", report_revision="quant-report-v1", shortlist_cap=2,
            candle_start_cap=2, personal_symbols=(), pension_revision=None, reviewed_peers=())
        outcome = run_quant_once(deps=deps, now=now)
        if outcome is None or outcome.report.identity.identity_sha256 != expected_identity:
            raise ValueError("product replay identity mismatch")
        return outcome
    def local():
        personal = project_personal_symbols(holdings); projected = project_pension_symbols(pension)
        return {"personal_present": holdings is not None, "personal_projected": len(personal),
                "pension_present": pension is not None, "pension_projected": len(projected.symbols)}
    return lambda: run_bounded_canary(campaign=campaign, auth=auth, run_market=market,
        local_projection=local, product_replay=replay_market)


def main(argv=None, *, factory=build_production_runner):
    parser = argparse.ArgumentParser(description="Run the fixed bounded Quant canary once")
    parser.parse_args(argv)
    try:
        result = factory()()
        _validate_public_result(result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        refused = {"campaign_status": "REFUSED"}
        if isinstance(exc, CampaignFailure):
            refused["reason"] = exc.reason
        print(json.dumps(refused, separators=(",", ":")), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__": raise SystemExit(main())
