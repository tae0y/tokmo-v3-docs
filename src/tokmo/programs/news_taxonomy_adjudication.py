"""Stage-7 NEWS taxonomy pending adjudication worker (2026-08-22).

Batched, provider-pluggable resolution of the durable PENDING_MODEL
queue. The worker is inert by default and fail-closed everywhere:

- Config authority is one JSON file; missing, default, or malformed
  config is OFF (never a fallback route). The exact schema::

      OFF (also the default when the file is absent or malformed):
          {"mode": "OFF"}
      Explicit route (route_id is an opaque operational identifier —
      never a taxonomy/domain value; bounds are hard-capped):
          {"mode": "ROUTE", "route_id": "route-alpha",
           "batch_max": 8, "max_starts": 1, "retry_max": 0}

- The transport is an injected callable ``transport(batch_request) ->
  response`` speaking only the provider-neutral batch protocol in
  ``tokmo.values.news_taxonomy``. No provider or model name appears in
  this module, the protocol, or the taxonomy store; the route adapter is
  bound separately at composition behind its own activation review. The
  CLI below binds NO transport, so it can never start a provider call.

- Priority is deterministic: collision/ambiguity risk and units matching
  the injected holdings/watchlist/typed-interest scope come before typed
  but unscoped items; broad unscoped generic pending (unscoped repeated
  topics, subject-less metrics) is never model-adjudicated merely for
  being pending — it expires into a durable DEFERRED marker instead,
  with reason and evidence history preserved.

- Caps: at most ``max_starts`` (default 1) provider starts per run, at
  most ``batch_max`` (default 8, hard max 12) units per start, no loop
  until empty. Idempotency and crash recovery live in the store's
  adjudication job ledger (same pending evidence never starts twice;
  crashes reconcile to explicit UNKNOWN; default retry budget 0).
"""
import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from tokmo.programs.news_topic_discovery import (
    _contains_term,
    _normalise,
    _tokens,
)
from tokmo.values.news_taxonomy import (
    TAXONOMY_KINDS_V1,
    adjudication_job_id,
    adjudication_response_digest,
    build_adjudication_batch_request,
    validate_adjudication_batch_response,
)

ADJUDICATION_BATCH_DEFAULT = 8
ADJUDICATION_BATCH_HARD_MAX = 12
ADJUDICATION_STARTS_DEFAULT = 1
ADJUDICATION_STARTS_HARD_MAX = 4
ADJUDICATION_RETRY_HARD_MAX = 2
ADJUDICATION_EXPIRY_DAYS = 14
ADJUDICATION_SCAN_LIMIT = 96
ADJUDICATION_EXPIRE_PER_RUN = 8
ADJUDICATION_STALE_AFTER_SECONDS = 900

# Pending reasons that already prove collision/ambiguity risk: always the
# highest adjudication priority.
_AMBIGUITY_REASONS = frozenset(
    {"ALIAS_COLLISION", "CATALYST_AMBIGUOUS", "EFFECT_AMBIGUOUS"})
# Broad low-value pending: unscoped repeated phrases and subject-less
# metrics are never sent to a model unless the current scope reaches them.
_LOW_VALUE_UNSCOPED_REASONS = frozenset(
    {"TOPIC_UNSCOPED_REPEATED", "METRIC_SUBJECT_UNRESOLVED"})
_KIND_ORDER = {kind: index for index, kind in enumerate(TAXONOMY_KINDS_V1)}
_ROUTE_SLUG = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}")


@dataclass(frozen=True)
class AdjudicationConfigV1:
    mode: str                      # "OFF" | "ROUTE"
    route_id: "str | None"
    batch_max: int
    max_starts: int
    retry_max: int
    reason: str                    # OFF/MISSING/MALFORMED/ROUTE


def _off(reason):
    return AdjudicationConfigV1("OFF", None, ADJUDICATION_BATCH_DEFAULT,
                                ADJUDICATION_STARTS_DEFAULT, 0, reason)


def _bounded_int(payload, key, default, ceiling):
    value = payload.get(key, default)
    if type(value) is not int or not 0 <= value <= ceiling \
            or (key != "retry_max" and value < 1):
        raise ValueError(key)
    return value


def read_adjudication_config(path) -> AdjudicationConfigV1:
    """Single config authority; anything but an exact OFF or ROUTE shape
    is OFF (fail closed, no fallback)."""
    path = Path(path)
    if not path.exists():
        return _off("MISSING")
    try:
        payload = json.loads(path.read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("shape")
        if payload.get("mode") == "OFF":
            if set(payload) != {"mode"}:
                raise ValueError("keys")
            return _off("OFF")
        if payload.get("mode") != "ROUTE":
            raise ValueError("mode")
        if not set(payload) <= {"mode", "route_id", "batch_max",
                                "max_starts", "retry_max"}:
            raise ValueError("keys")
        route_id = payload["route_id"]
        if type(route_id) is not str or not _ROUTE_SLUG.fullmatch(route_id):
            raise ValueError("route_id")
        return AdjudicationConfigV1(
            "ROUTE", route_id,
            _bounded_int(payload, "batch_max", ADJUDICATION_BATCH_DEFAULT,
                         ADJUDICATION_BATCH_HARD_MAX),
            _bounded_int(payload, "max_starts", ADJUDICATION_STARTS_DEFAULT,
                         ADJUDICATION_STARTS_HARD_MAX),
            _bounded_int(payload, "retry_max", 0,
                         ADJUDICATION_RETRY_HARD_MAX),
            "ROUTE")
    except (OSError, ValueError, KeyError, TypeError):
        return _off("MALFORMED")


def _scope_texts(row):
    texts = [row["unit_key"], row.get("display_text") or "",
             row.get("canonical_key") or ""]
    try:
        evidence = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        evidence = {}
    if isinstance(evidence, dict):
        for key in ("subject", "canonical"):
            value = evidence.get(key)
            if isinstance(value, str):
                texts.append(value)
        by_canonical = evidence.get("by_canonical")
        if isinstance(by_canonical, dict):
            texts.extend(str(key) for key in by_canonical)
    return texts


def _unit_scoped(row, scope_terms):
    for text in _scope_texts(row):
        tokens = _tokens(str(text))
        if tokens and any(_contains_term(tokens, term)
                          for term in scope_terms):
            return True
    return False


def select_adjudication_units(rows, *, scope_terms, now, expiry_days):
    """Deterministic partition of the eligible pending window.

    Returns (batchable, expirable): batchable rows sorted by priority
    class (ambiguity → scoped relevance → typed unscoped), then grouped
    by kind and relevance identity, then created_at/unit_id; expirable is
    the broad low-value unscoped tail older than the expiry window (never
    model-adjudicated, durably deferred instead)."""
    scope = tuple(term for term in
                  (_normalise(str(term)) for term in scope_terms)
                  if term)
    horizon = (now - timedelta(days=expiry_days)).isoformat()
    ranked, expirable = [], []
    for row in rows:
        scoped = _unit_scoped(row, scope)
        reason = row["reason"]
        if reason in _AMBIGUITY_REASONS:
            rank = 0
        elif scoped:
            rank = 1
        elif reason in _LOW_VALUE_UNSCOPED_REASONS:
            if row["created_at"] <= horizon:
                expirable.append(row)
            continue
        else:
            rank = 2
        identity = row.get("canonical_key") or ""
        ranked.append(((rank, _KIND_ORDER.get(row["kind"], 99), identity,
                        row["created_at"], row["unit_id"]), row))
    ranked.sort(key=lambda pair: pair[0])
    return [row for _, row in ranked], expirable


def run_taxonomy_adjudication_worker(
        *, store, config, transport, now, scope_terms=frozenset(),
        scan_limit=ADJUDICATION_SCAN_LIMIT,
        expiry_days=ADJUDICATION_EXPIRY_DAYS,
        expire_max=ADJUDICATION_EXPIRE_PER_RUN,
        stale_after_seconds=ADJUDICATION_STALE_AFTER_SECONDS):
    """Drain at most ``config.max_starts`` bounded batches once.

    OFF (any reason) does nothing at all; a ROUTE config without a bound
    transport reports itself and starts zero. Every start, settle, and
    failure is exact-counted; there is no loop until empty and no
    automatic retry (EXHAUSTED batches defer their units durably so later
    runs move on)."""
    report = {"mode": config.mode, "route_id": config.route_id,
              "config_reason": config.reason,
              "transport_bound": transport is not None,
              "reconciled_unknown": 0, "eligible": 0, "expired_deferred": 0,
              "exhausted_deferred": 0, "requested": 0, "started": 0,
              "settled": 0, "failed": 0, "replay_skipped": 0,
              "in_flight_skipped": 0, "verified": 0, "rejected": 0,
              "deferred": 0}
    if config.mode != "ROUTE" or transport is None:
        return report
    report["reconciled_unknown"] = store.reconcile_taxonomy_adjudication(
        now=now, stale_after_seconds=stale_after_seconds)
    rows = store.list_taxonomy_adjudication_eligible(limit=scan_limit)
    report["eligible"] = len(rows)
    ordered, expirable = select_adjudication_units(
        rows, scope_terms=scope_terms, now=now, expiry_days=expiry_days)
    for row in expirable[:expire_max]:
        store.mark_taxonomy_adjudication_deferral(
            row["unit_id"], "EXPIRED", "ADJUDICATION_EXPIRED_LOW_VALUE",
            now=now)
        report["expired_deferred"] += 1
    batches = [ordered[index:index + config.batch_max]
               for index in range(0, len(ordered), config.batch_max)]
    for batch in batches[:config.max_starts]:
        request = build_adjudication_batch_request(batch)
        job_id = adjudication_job_id(batch)
        report["requested"] += len(batch)
        token = store.begin_taxonomy_adjudication(
            job_id, request_digest=request["request_digest"],
            unit_ids=request["unit_ids"], route_id=config.route_id,
            now=now, retry_max=config.retry_max)
        if token == "REPLAY_SETTLED":
            report["replay_skipped"] += 1
            continue
        if token == "IN_FLIGHT":
            report["in_flight_skipped"] += 1
            continue
        if token == "EXHAUSTED":
            for row in batch:
                store.mark_taxonomy_adjudication_deferral(
                    row["unit_id"], "EXHAUSTED",
                    "ADJUDICATION_ATTEMPTS_EXHAUSTED", now=now,
                    job_id=job_id)
                report["exhausted_deferred"] += 1
            continue
        report["started"] += 1
        try:
            response = transport(request)
        except Exception:
            store.settle_taxonomy_adjudication_failure(
                job_id, "TRANSPORT_FAIL", now=now)
            report["failed"] += 1
            continue
        try:
            results = validate_adjudication_batch_response(response, request)
        except ValueError:
            store.settle_taxonomy_adjudication_failure(
                job_id, "INVALID_RESPONSE", now=now)
            report["failed"] += 1
            continue
        try:
            store.apply_taxonomy_adjudication(
                job_id, results,
                response_digest=adjudication_response_digest(response),
                now=now)
        except Exception:
            store.settle_taxonomy_adjudication_failure(
                job_id, "STATE_CONFLICT", now=now)
            report["failed"] += 1
            continue
        report["settled"] += 1
        for result in results:
            report[result["verdict"].casefold()] += 1
    return report


def main(argv=None):
    from tokmo.records.news_episode_store import NewsEpisodeStore

    parser = argparse.ArgumentParser(
        description="Drain the NEWS taxonomy pending adjudication queue "
                    "once. Inert by construction: no transport is bound "
                    "here, so a ROUTE config reports itself and starts "
                    "zero; binding a route adapter is a separate "
                    "composition change behind its own review.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--now", required=True,
                        help="aware ISO-8601 judgment instant")
    parser.add_argument("--config", default=None,
                        help="route config path (default: "
                             "<root>/news_taxonomy_adjudication.json)")
    parser.add_argument("--scope-term", action="append", default=[],
                        help="holdings/watchlist/typed-interest scope term "
                             "for deterministic priority (repeatable)")
    arguments = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(arguments.now.replace("Z", "+00:00"))
    except ValueError:
        parser.error("--now must be ISO-8601")
    if now.tzinfo is None or now.utcoffset() is None:
        parser.error("--now must be timezone-aware")
    root = Path(arguments.root)
    config = read_adjudication_config(
        Path(arguments.config) if arguments.config
        else root / "news_taxonomy_adjudication.json")
    store = NewsEpisodeStore(root / "stores" / "news_episodes.sqlite3")
    report = run_taxonomy_adjudication_worker(
        store=store, config=config, transport=None, now=now,
        scope_terms=frozenset(arguments.scope_term))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
