from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tokmo.facts.toss import TossAccessToken, _TossTransportSuccess, build_auth_request, parse_token_response
from tokmo.facts.toss_auth_live import TossAuthLiveTransport
from tokmo.facts.toss_read_parsers import parse_envelope
from tokmo.facts.toss_reads import TossReadOperation, build_read_request
from tokmo.facts.toss_reads_live import TossReadsLiveTransport
from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.runtime.config import load_toss_config
from tokmo.values.calendar_refresh import market_local_date
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import KrMarketCalendarResponse, UsMarketCalendarResponse

ROOT = Path(__file__).resolve().parents[1] / "var/data"
PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "ops/calendar-cache-v2-migration.json"
CACHE = ROOT / "ops/calendar_cache.json"
MAX_STARTS = 6  # two explicit tranches of TOKEN + KR + US; never reset


def now():
    return datetime.now(timezone.utc)


def persist(state):
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = EVIDENCE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, sort_keys=True) + "\n")
    temp.chmod(0o600)
    temp.replace(EVIDENCE)


def start(state, operation):
    if len(state["starts"]) >= MAX_STARTS:
        raise RuntimeError("migration_request_cap")
    state["starts"].append({"operation": operation, "started_at": now().isoformat()})
    persist(state)


def key_shape(body):
    try:
        envelope = json.loads(body)
        result = envelope.get("result", {})
        today = result.get("today", {})
        integrated = today.get("integrated", {}) if isinstance(today, dict) else {}
        return {
            "result_keys": sorted(result) if isinstance(result, dict) else [],
            "today_keys": sorted(today) if isinstance(today, dict) else [],
            "integrated_keys": sorted(integrated) if isinstance(integrated, dict) else [],
        }
    except Exception:
        return {"shape": "unreadable"}


def main():
    if EVIDENCE.exists():
        state = json.loads(EVIDENCE.read_text())
        state["cap"] = MAX_STARTS
        state["outcome"] = "STARTED_TRANCHE_2"
    else:
        state = {"campaign": "calendar-cache-v2-migration-v1", "cap": MAX_STARTS,
                 "starts": [], "outcome": "STARTED"}
    persist(state)
    config = load_toss_config(PROJECT / ".env.toss")
    start(state, "TOKEN")
    auth = TossAuthLiveTransport()(build_auth_request(
        client_id=config.client_id, client_secret=config.client_secret))
    if not isinstance(auth, _TossTransportSuccess):
        raise RuntimeError("auth_failed")
    token = parse_token_response(auth.body, observed_at=now())
    if not isinstance(token, TossAccessToken):
        raise RuntimeError("token_parse_failed")
    reads = TossReadsLiveTransport()
    days = []
    observed = now()
    for market, operation, expected in (
        (Market.KR, TossReadOperation.KR_MARKET_CALENDAR, KrMarketCalendarResponse),
        (Market.US, TossReadOperation.US_MARKET_CALENDAR, UsMarketCalendarResponse),
    ):
        start(state, operation.value)
        requested_date = market_local_date(market, observed)
        reply = reads(build_read_request(
            operation, token=token.value, date=requested_date.isoformat(),
        ))
        if not isinstance(reply, _TossTransportSuccess):
            raise RuntimeError(f"{operation.value}_transport_failed")
        parsed = parse_envelope(reply.body, operation.value)
        if not isinstance(parsed, expected):
            state.setdefault("diagnostic_key_shapes", {})[operation.value] = key_shape(reply.body)
            state["outcome"] = "CONTRACT_MISMATCH"
            persist(state)
            raise RuntimeError(f"{operation.value}_parse_failed")
        if parsed.today.date != requested_date:
            raise RuntimeError(f"{operation.value}_date_mismatch")
        days.append(parsed.today)
    backup = CACHE.with_name("calendar_cache.schema1.pre-v2.json")
    if CACHE.exists() and not backup.exists():
        backup.write_bytes(CACHE.read_bytes())
        backup.chmod(0o600)
    CalendarCacheStore(CACHE, clock=now).write(days[0], days[1])
    reread = CalendarCacheStore(CACHE, clock=now).read()
    if reread is None or reread.kr is None or reread.us is None:
        raise RuntimeError("cache_v2_readback_failed")
    state["outcome"] = "COMPLETE"
    state["schema_version"] = reread.schema_version
    state["session_names"] = {
        "KR": sorted(reread.kr.sessions), "US": sorted(reread.us.sessions),
    }
    persist(state)
    print(json.dumps({"outcome": state["outcome"], "schema_version": state["schema_version"],
                      "session_names": state["session_names"]}, sort_keys=True))


if __name__ == "__main__":
    main()
