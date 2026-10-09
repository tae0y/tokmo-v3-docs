#!/usr/bin/env python3
"""Quant Telegram decision-card controller (product contract 2026-09-03).

Subcommands:
  preview   render the latest receipt-verified KR/US reports; no send, no write
  arm       write the mode-0600 CANARY gate for one activation id (operator only)
  send      one-attempt canary send of both market messages (gate + env bound)
  disable   rollback: rewrite the gate to OFF (fail-closed)
  status    read the one-attempt receipt for an activation id

Recipient is fixed to the existing private stock-bot env contract
(TELEGRAM_STOCK_BOT_TOKEN / TELEGRAM_STOCK_BOT_CHAT_ID, with the installer's
TELEGRAM_STOCK_BOT / TELEGRAM_CHAT_ID pair as the only fallback). Secret
values are never printed or persisted. No order or account write exists on
this path; no scheduler is installed.

Cycle-2 context (shared by the tokmo-group controller through ``render``):
holdings weights from canonical holdings × valid latest quotes, a
structure-only playbook synthesis (no raw text or path), and the bounded
OPEN conditional-order read (one page per mapped account, limit 100,
retry 0) through an injected ``conditional_fetch`` or, when absent on
``send``/``--conditional-live``, the official Toss read adapter. Provider
call counts are reported; no provider payload is persisted.

Contextual analysis (SDS 2026-09-03 v0.1): ``render`` accepts an injected
``analysis`` callable (evidence pack → JSON) plus optional ``search``/``fetch``
seams and per-symbol ``context_items``. Default is OFF: zero model calls,
zero external calls, byte-identical card. ``preview --analysis-preview``
without an injected callable still reports ``model_seam_unbound`` and
renders the plain card; ``send`` never analyses.

Finite live canary (2026-09-04, one approved run — SDS §12):
  canary-preview  armed gate + activation id → exact-route admission (the
                  two local route-authority files, read-only) → durable
                  analysis reservation → ≤4 bounded model calls through the
                  sanctioned direct Z.AI GLM route (one per located card,
                  retry 0, no web/RSS search) → all cards must validate →
                  sanitized analysis receipt; NO Telegram send
  canary-send     validated analysis receipt + expected KR/US report digests
                  → re-render from the stored analyses (zero model calls) →
                  exactly one root message per market through the existing
                  one-attempt send path; replay sends nothing
The credential is read per call from the owner-checked Hermes env file
only after the reservation exists and rides only the Authorization header.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tokmo.programs.quant_card_conditional import (
    CONDITIONAL_PAGE_LIMIT,
    collect_open_conditional_orders,
    conditional_context_for,
)
from tokmo.programs.quant_contextual_analysis import (
    ContextItemV1,
    build_evidence_pack,
    gather_external_evidence,
    run_contextual_analysis,
)
from tokmo.programs.quant_finite_canary import (
    FINITE_CANARY_CALL_TIMEOUT_SECONDS,
    FINITE_CANARY_MAX_TOKENS,
    FINITE_CANARY_MODEL_ID,
    RouteNotAdmitted,
    admit_preview,
    admit_route,
    admit_send,
    analyses_from_receipt,
    analyzer_prompt_text,
    bind_call_ledger,
    capped_analysis,
    check_message_digests,
    check_messages,
    partial_card_snapshot,
    reserve_analysis_receipt,
    retain_analysis,
    settle_preview,
)
from tokmo.programs.quant_local_context import (
    collect_dart_disclosure_items,
    collect_local_news_items,
    collect_playbook_context_items,
)
from tokmo.programs.quant_telegram_card import (
    MARKETS,
    WEIGHT_QUOTE_MAX_AGE,
    CardContextV1,
    ConditionalContextV1,
    HoldingContextV1,
    PositionV1,
    QuoteContextV1,
    compute_standings,
    compute_weights,
    format_price,
    fx_usd_krw_from_evidence,
    render_decision_cards,
    run_quant_telegram_card_canary,
    synthesize_playbook,
    telegram_pre_message,
)
from tokmo.records.account_native_refs import (
    AccountNativeRefStoreV1,
    NativeRefCorruptError,
)
from tokmo.records.current_holdings_store import (
    CurrentHoldingsCorruptError,
    CurrentHoldingsStore,
)
from tokmo.records.latest_quotes import LatestQuoteStore, LatestQuoteStoreReadError
from tokmo.records.playbook_identity import playbook_scope_dir
from tokmo.records.quant_report_locator import locate_latest_verified_reports
from tokmo.records.quant_telegram_card_receipts import (
    QuantTelegramCardReceiptStore,
    read_card_activation,
    valid_activation_id,
    write_card_activation,
)
from tokmo.values.current_holdings import classify_market
from tokmo.values.session_gate import Market
from tokmo.watchlists import WatchlistError, load_watchlists

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(__file__).resolve().parents[1] / "var" / "data"
DEFAULT_REPORT_ROOT = DATA_ROOT / "ops" / "quant-g2-preview"
GATE_PATH = DATA_ROOT / "ops" / "quant-telegram-card-activation.json"
RECEIPT_ROOT = DATA_ROOT / "ops" / "quant-telegram-card"
HOLDINGS_PATH = DATA_ROOT / "stores" / "current_holdings.json"
QUOTES_PATH = DATA_ROOT / "stores" / "latest_quotes.sqlite3"
NATIVE_REFS_PATH = DATA_ROOT / "ops" / "account_native_refs.sqlite3"
NEWS_PATH = DATA_ROOT / "news" / "external_news_v4.sqlite3"
DART_PATH = DATA_ROOT / "opendart-bulk.sqlite3"
QUOTE_SOURCE = "toss-current-price-v1"
QUOTE_SOURCE_LABEL = "최근 시세 스토어"
PLAYBOOK_ROOT = PROJECT_ROOT / "playbooks"
WATCHLIST_ROOT = PROJECT_ROOT / "watchlists"
TOSS_ENV_PATH = PROJECT_ROOT / ".env.toss"
# finite-canary model route: the same two route authorities and the same
# owner-checked credential file the NEWS classifier worker binds
ROUTING_STATE_PATH = Path.home() / ".hermes" / "MODEL_ROUTING_STATE.md"
QUOTA_LATEST_PATH = (
    Path.home() / ".hermes" / "cron" / "state" / "model-quota-latest.json"
)
ZAI_ENV_FILE = Path.home() / ".hermes" / ".env"
ENV_TOKEN, ENV_CHAT_ID = "TELEGRAM_STOCK_BOT_TOKEN", "TELEGRAM_STOCK_BOT_CHAT_ID"
LEGACY_ENV_TOKEN, LEGACY_ENV_CHAT_ID = "TELEGRAM_STOCK_BOT", "TELEGRAM_CHAT_ID"
SCOPE_LABELS = {"personal": "개인계좌", "retirement": "퇴직연금계좌"}
EXIT_OK, EXIT_REFUSED, EXIT_NOT_SENT = 0, 2, 3


def bind_stock_bot_env(environ) -> tuple[str, str] | None:
    """Exact private stock-bot recipient binding; None when not fully bound."""
    token = environ.get(ENV_TOKEN) or environ.get(LEGACY_ENV_TOKEN)
    chat_id = environ.get(ENV_CHAT_ID) or environ.get(LEGACY_ENV_CHAT_ID)
    if not token or not chat_id:
        return None
    return str(token), str(chat_id)


def scope_label(account_scope: str) -> str:
    return SCOPE_LABELS.get(playbook_scope_dir(account_scope), "계좌")


def build_live_conditional_fetch(*, toss_env_path=TOSS_ENV_PATH, clock):
    """Official Toss read adapter bound to ONE token and no retry; returns (fetch, calls)."""
    from tokmo.facts.toss import (
        TossAccessToken,
        build_auth_request,
        parse_token_response,
    )
    from tokmo.facts.toss_auth_live import TossAuthLiveTransport
    from tokmo.facts.toss_reads import TossReadOperation, build_read_request, query_read
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport
    from tokmo.programs.conditional_detection import PulseConditionalPageOutcomeV1
    from tokmo.runtime.config import load_toss_config

    config = load_toss_config(Path(toss_env_path))
    auth_transport, reads_transport = TossAuthLiveTransport(), TossReadsLiveTransport()
    calls = {"TOKEN": 0, "CONDITIONAL_ORDERS": 0}
    cell: dict = {}

    def token():
        if "value" not in cell:
            calls["TOKEN"] += 1
            reply = auth_transport(
                build_auth_request(
                    client_id=config.client_id, client_secret=config.client_secret
                )
            )
            parsed = (
                parse_token_response(reply.body, observed_at=clock())
                if hasattr(reply, "body")
                else None
            )
            if not isinstance(parsed, TossAccessToken):
                raise RuntimeError("token_transport_failed")
            cell["value"] = parsed.value
        return cell["value"]

    def fetch(*, account_scope, native_ref, status, cursor, limit):
        del account_scope
        if native_ref is None:
            return PulseConditionalPageOutcomeV1("account_unmapped")
        calls["CONDITIONAL_ORDERS"] += 1
        request = build_read_request(
            TossReadOperation.CONDITIONAL_ORDERS,
            token=token(),
            account_seq=native_ref,
            status=status,
            cursor=cursor,
            limit=min(int(limit), CONDITIONAL_PAGE_LIMIT),
        )
        outcome = query_read(request, transport=reads_transport, clock=clock)
        if outcome.code != "success":
            return PulseConditionalPageOutcomeV1(outcome.code)
        value = outcome.value
        return PulseConditionalPageOutcomeV1(
            "success",
            tuple(value.conditional_orders),
            value.next_cursor if isinstance(value.next_cursor, str) else None,
            value.has_next,
        )

    return fetch, calls


def build_live_analyzer_transport(
    *, model_id, env_file, timeout_seconds, max_tokens, opener=None, json_mode=False
):
    """Sanctioned direct non-agent model route for the finite canary: the
    NEWS classifier's registered HTTPS adapter (one bounded POST, no tools,
    no session, retry 0, fallback 0) carrying the quant analyzer prompt.

    Returns ``transport(request) -> str`` for ``analyzer_adapter``. The
    credential is read per call from the owner-checked env file — never at
    build time, never in argv, never logged — and rides only the
    Authorization header. The reply's last top-level JSON object is handed
    back as a compact string; anything else raises and rejects that card."""
    from tokmo.facts.news_http import zai_chat_once
    from tokmo.programs.news_classifier_executor import (
        extract_json_object,
        read_zai_credential_file,
    )

    if type(json_mode) is not bool:
        raise TypeError("json_mode must be bool")

    def transport(request):
        message = zai_chat_once(
            analyzer_prompt_text(request),
            timeout_seconds=timeout_seconds,
            api_key=read_zai_credential_file(env_file),
            model=model_id,
            max_tokens=max_tokens,
            opener=opener,
            json_mode=json_mode,
        )
        return json.dumps(extract_json_object(message), ensure_ascii=False)

    return transport


def _route_authority_reader(routing_state_path, quota_latest_path, now):
    """Fail-closed exact-route admission over the two local authority files:
    the routing state's own ``provider=zai``/``model=glm-5.3`` direct-route
    line (never the NEWS flash marker) plus the canonical quota bounds.
    Returns digests/mtime/observed_at only — no path."""

    def stamp(path):
        data = Path(path).read_bytes()
        return data, {
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtime_ns": os.stat(path).st_mtime_ns,
        }

    def reader(_request):
        try:
            routing_bytes, routing_stamp = stamp(routing_state_path)
            quota_bytes, quota_stamp = stamp(quota_latest_path)
            routing_text, quota_payload = (
                routing_bytes.decode("utf-8"),
                json.loads(quota_bytes),
            )
        except (OSError, ValueError):
            raise RouteNotAdmitted("MODEL_UNAVAILABLE")
        observed_at = admit_route(
            routing_text=routing_text, quota_payload=quota_payload, now=now
        )
        return {
            "routing_state": routing_stamp,
            "quota_latest": dict(quota_stamp, observed_at=observed_at),
        }

    return reader


def _holdings_state(path: Path, now):
    try:
        state = CurrentHoldingsStore(path, clock=lambda: now).read()
    except CurrentHoldingsCorruptError:
        return None, "holdings store corrupt"
    if state is None:
        return None, "holdings store absent"
    return state, None


def _quote(path: Path, symbol: str, now) -> tuple:
    try:
        fact = LatestQuoteStore(path, clock=lambda: now).read_latest(
            source=QUOTE_SOURCE, symbols=(symbol,)
        )[0]
    except LatestQuoteStoreReadError as error:
        reason = {
            "missing_latest_quote": "최근 시세 스토어에 종목 없음",
            "store_read_failed": "시세 스토어 읽기 실패",
        }.get(error.reason, error.reason)
        return None, reason
    try:
        price = Decimal(fact.last_price)
    except (InvalidOperation, TypeError, ValueError):
        return None, "시세 값 불량"
    return QuoteContextV1(
        price, fact.currency_raw, fact.timestamp, QUOTE_SOURCE_LABEL
    ), None


def _weight_quote(quote, reason, market: str, now) -> tuple:
    """Weight-grade quote: valid price, market currency, and not stale.

    Freshness is judged on the provider observation time (the store's
    nullable ``provider_timestamp``), never on our fetch instant. The two
    rejection labels stay distinct and evidence-bearing so a production
    denominator failure names the exact cause: a null provider timestamp
    versus an observation older than ``WEIGHT_QUOTE_MAX_AGE``."""
    if quote is None:
        return None, reason
    if quote.currency != ("KRW" if market == "KR" else "USD"):
        return None, "시세 통화 불일치"
    if quote.observed_at is None:
        return None, "시세 관측시각 없음 (제공자 timestamp null)"
    if now - quote.observed_at > WEIGHT_QUOTE_MAX_AGE:
        return None, (
            f"시세 관측시각 오래됨 (관측시각 {quote.observed_at.date().isoformat()}"
            f" · 기준 {WEIGHT_QUOTE_MAX_AGE.days}일)"
        )
    return quote.price, None


def _positions(state, quotes_path: Path, now, quote_cache: dict) -> tuple:
    positions = []
    for account in state.accounts:
        label = scope_label(account.account_scope)
        for item in account.holdings:
            market = classify_market(item.market_raw, item.market_field)
            if market is None:
                continue
            try:
                quantity = Decimal(item.quantity)
            except (InvalidOperation, TypeError, ValueError):
                continue
            if item.symbol not in quote_cache:
                quote_cache[item.symbol] = _quote(quotes_path, item.symbol, now)
            price, reason = _weight_quote(*quote_cache[item.symbol], market.value, now)
            positions.append(
                PositionV1(
                    account.account_scope,
                    label,
                    market.value,
                    item.symbol,
                    quantity,
                    price,
                    "KRW" if market is Market.KR else "USD",
                    reason,
                )
            )
    return tuple(positions)


def _playbook(root: Path, market: str, symbol: str, preferred_scopes) -> tuple:
    order = [scope for scope in preferred_scopes if scope] + ["personal", "retirement"]
    seen = []
    for scope_dir in order:
        if scope_dir in seen:
            continue
        seen.append(scope_dir)
        path = root / scope_dir / market / f"{symbol}.md"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None, "플레이북 읽기 실패"
        return synthesize_playbook(
            text, scope_label=SCOPE_LABELS.get(scope_dir, "계좌")
        ), None
    return None, "플레이북 문서 없음"


def build_contexts(
    reports: dict,
    *,
    holdings_path=HOLDINGS_PATH,
    quotes_path=QUOTES_PATH,
    playbook_root=PLAYBOOK_ROOT,
    watchlist_root=WATCHLIST_ROOT,
    native_refs_path=NATIVE_REFS_PATH,
    conditional_fetch=None,
    fx_evidence=None,
    now,
) -> tuple:
    """(contexts by market, evidence dict). Evidence carries counts/reasons only."""
    state, holdings_reason = _holdings_state(holdings_path, now)
    holdings_ok = state is not None
    quote_cache: dict = {}
    positions = _positions(state, quotes_path, now, quote_cache) if holdings_ok else ()
    fx_rate, fx_reason = fx_usd_krw_from_evidence(fx_evidence, now=now)
    weights = compute_weights(positions, fx_usd_krw=fx_rate, fx_reason=fx_reason)
    try:
        watchlists = {
            market.value: symbols
            for market, symbols in load_watchlists(watchlist_root).items()
        }
    except WatchlistError:
        watchlists = None
    scan = None
    scan_evidence = {"performed": False, "provider_calls": 0, "scopes": {}}
    if conditional_fetch is not None and holdings_ok:
        try:
            refs = AccountNativeRefStoreV1(
                native_refs_path, clock=lambda: now
            ).read_refs()
        except NativeRefCorruptError:
            refs = None
        scan = collect_open_conditional_orders(
            holdings=state, refs=refs, fetch=conditional_fetch
        )
        scan_evidence = {
            "performed": True,
            "provider_calls": scan.provider_calls,
            "page_limit": CONDITIONAL_PAGE_LIMIT,
            "retry": 0,
            # keyed by label+ordinal: account scope ids stay out of stdout/evidence
            "scopes": {
                f"{scope_label(item.account_scope)}#{index}": {
                    "status": item.status,
                    "reason": item.reason,
                    "open_orders": len(item.orders),
                }
                for index, item in enumerate(scan.scopes, 1)
            },
            "writes": dict(scan.writes),
        }
    contexts: dict = {}
    for market in MARKETS:
        located = reports.get(market)
        rows = []
        if located is not None:
            for card in located.report.cards:
                held = []
                scopes = []
                for position in positions:
                    # Quant holdings are the personal account only: the retirement
                    # (pension) book is never mixed into the card (contract 2026-09-04)
                    if playbook_scope_dir(position.scope_key) != "personal":
                        continue
                    if (position.market, position.symbol) == (market, card.symbol):
                        try:
                            avg = format_price(
                                Decimal(_avg_price(state, position)), position.currency
                            )
                        except (InvalidOperation, ValueError, TypeError):
                            avg = _avg_price(state, position)
                        held.append(
                            HoldingContextV1(
                                position.scope_label,
                                f"{position.quantity.normalize():f}",
                                avg,
                                weights.get((position.scope_key, market, card.symbol)),
                            )
                        )
                        scopes.append(playbook_scope_dir(position.scope_key))
                if card.symbol not in quote_cache:
                    quote_cache[card.symbol] = _quote(quotes_path, card.symbol, now)
                quote, quote_reason = quote_cache[card.symbol]
                playbook, playbook_reason = _playbook(
                    playbook_root, market, card.symbol, scopes
                )
                watchlisted = (
                    None
                    if watchlists is None
                    else card.symbol in watchlists.get(market, frozenset())
                )
                if scan is None:
                    conditional = ConditionalContextV1(
                        "UNAVAILABLE",
                        "조회 미실행" if holdings_ok else "보유 스토어 미확인",
                    )
                else:
                    conditional = conditional_context_for(
                        scan, market=market, symbol=card.symbol, scope_label=scope_label
                    )
                valid_price, validity_reason = _weight_quote(
                    quote, quote_reason, market, now
                )
                rows.append(
                    CardContextV1(
                        card.symbol,
                        quote,
                        quote_reason,
                        tuple(held),
                        holdings_ok,
                        watchlisted,
                        playbook,
                        playbook_reason,
                        conditional,
                        report_verified=True,
                        quote_age_valid=valid_price is not None,
                        quote_validity_reason=validity_reason,
                    )
                )
        contexts[market] = tuple(rows)
    evidence = {
        "holdings": {
            "available": holdings_ok,
            "reason": holdings_reason,
            "positions": len(positions),
            "priced": sum(1 for p in positions if p.price is not None),
        },
        "fx": {"rate_available": fx_rate is not None, "reason": fx_reason},
        "conditional": scan_evidence,
    }
    return contexts, evidence


def _avg_price(state, position: PositionV1) -> str:
    for account in state.accounts:
        if account.account_scope != position.scope_key:
            continue
        for item in account.holdings:
            if item.symbol == position.symbol:
                return item.avg_price
    return "?"


def pre_entity_sender(token: str, chat_id: str, *, send=None):
    """Live sender: the renderer's ``` fences become one Telegram ``pre``
    entity each (the monospace return table); text without fences is posted
    in the legacy byte-identical form. ``send`` is injectable for tests."""
    if send is None:
        from tokmo.facts.telegram_dialogue_live import send_dialogue_message as send

    def telegram_send(text):
        body, entities = telegram_pre_message(text)
        return send(
            body_text=body,
            bot_token=token,
            chat_id=chat_id,
            entities=entities or None,
            protect_content=False,
        )

    return telegram_send


def _local_universe(watchlist_root) -> dict:
    """{symbol: (name,)} from the canonical watchlist identities; {} on error."""
    try:
        from tokmo.watchlists import load_watchlist_identities

        return {
            symbol: (name,)
            for identities in load_watchlist_identities(Path(watchlist_root)).values()
            for symbol, name in identities
        }
    except Exception:
        return {}


def build_local_context_items(
    cards,
    *,
    universe,
    completed_session_date,
    playbook_root=PLAYBOOK_ROOT,
    news_path=NEWS_PATH,
    dart_path=DART_PATH,
) -> dict:
    """{(market, symbol): ContextItemV1 tuple} from local read-only sources.

    Bounded projection through the quant_local_context collectors; every
    missing store degrades to an empty tuple for that source plus one
    ``source_unavailable`` marker item per degraded source (per-source
    availability stays visibly distinct from a valid zero-result) — never
    raises, never writes, and the result only feeds the no-delivery analysis
    preview. ``completed_session_date`` is either one ISO date applied to
    every card or a per-card ``{(market, symbol): ISO date}`` session
    mapping, so a multi-market call can bind each card to its own market's
    completed session (NEWS/DART freshness is per-market, never collapsed
    to a shared minimum).
    """
    universe = dict(universe or {})
    items: dict = {}
    for market, symbol, name in cards:
        names = tuple(universe.get(symbol, (name,)))
        member_names = {key: tuple(values) for key, values in universe.items()}
        member_names.setdefault(symbol, names)
        status: dict = {}
        if isinstance(completed_session_date, dict):
            card_session = completed_session_date.get((market, symbol))
        else:
            card_session = completed_session_date
        collected: list = list(
            collect_playbook_context_items(playbook_root, market=market, symbol=symbol)
        )
        collected.extend(
            collect_local_news_items(
                news_path,
                market=market,
                symbol=symbol,
                official_names=names,
                universe=member_names,
                completed_session_date=card_session,
                status_out=status,
            )
        )
        if market == "KR":
            collected.extend(
                collect_dart_disclosure_items(
                    dart_path,
                    market=market,
                    symbol=symbol,
                    completed_session_date=card_session,
                    status_out=status,
                )
            )
        markers = [
            ContextItemV1(
                "source_unavailable",
                f"{source}:{source_status}",
                source_label="로컬 스토어 가용성",
            )
            for source, source_status in sorted(status.items())
            if source_status not in ("OK", "NOT_REQUESTED")
        ]
        # availability markers are never crowded out by real items
        items[(market, symbol)] = tuple(collected[: 5 - len(markers)] + markers)
    return items


def build_analyses(
    located: dict,
    contexts: dict,
    *,
    analysis,
    search=None,
    fetch=None,
    context_items=None,
    validated_card=None,
) -> tuple:
    """(validated analyses by market → symbol, receipt). One bounded model call
    per card; gaps searched only through the injected seams; nothing persisted."""
    counter = {"model_calls": 0}
    external_budget = {"queries": 0, "fetches": 0}

    def counted(pack):
        counter["model_calls"] += 1
        return analysis(pack)

    analyses: dict = {}
    receipt = {
        "enabled": True,
        "model_calls": 0,
        "validated": [],
        "rejected": {},
        "external": {"queries": 0, "fetches": 0, "retries": 0, "gaps_open": {}},
    }
    for market, item in located.items():
        if item is None:
            continue
        standings = compute_standings(item.report.cards)
        by_symbol = {context.symbol: context for context in contexts.get(market, ())}
        per_market: dict = {}
        for card in item.report.cards:
            key = f"{market}:{card.symbol}"
            context = by_symbol.get(card.symbol, CardContextV1(card.symbol))
            try:
                pack = build_evidence_pack(
                    card,
                    context,
                    completed_session_date=str(item.completed_session_date),
                    standings=standings,
                    context_items=(context_items or {}).get((market, card.symbol), ()),
                )
                pack = gather_external_evidence(
                    pack, search=search, fetch=fetch, budget=external_budget
                )
            except ValueError as error:
                receipt["rejected"][key] = f"pack_rejected:{error}"
                continue
            external = pack["external"]
            receipt["external"]["queries"] += external["queries"]
            receipt["external"]["fetches"] += external["fetches"]
            if external["gaps_open"]:
                receipt["external"]["gaps_open"][key] = list(external["gaps_open"])
            result, reason = run_contextual_analysis(pack, analysis=counted)
            if result is None:
                receipt["rejected"][key] = reason
            else:
                per_market[card.symbol] = result
                receipt["validated"].append(key)
                if validated_card is not None:
                    validated_card(
                        key=key,
                        analysis=result,
                        card=card,
                        context=context,
                        standings=standings,
                        as_of=pack["completed_session_date"],
                    )
        analyses[market] = per_market
    receipt["model_calls"] = counter["model_calls"]
    return analyses, receipt


def render(
    report_root: Path,
    *,
    now,
    analysis=None,
    search=None,
    fetch=None,
    context_items=None,
    stored_analyses=None,
    validated_card=None,
    **paths,
) -> tuple:
    located = locate_latest_verified_reports(report_root)
    contexts, evidence = build_contexts(
        located,
        now=now,
        **{
            name: value
            for name, value in paths.items()
            if name not in ("news_path", "dart_path")
        },
    )
    reports = {
        market: (None if item is None else (item.report, item.report_sha256))
        for market, item in located.items()
    }
    analyses = None
    evidence["analysis"] = {"enabled": False, "model_calls": 0}
    if stored_analyses is not None:
        # finite-canary delivery: the already-validated analyses from the
        # sanitized receipt; zero model calls, no local-context rebinding
        analyses = stored_analyses
        evidence["analysis"] = {
            "enabled": True,
            "model_calls": 0,
            "source": "analysis_receipt",
            "validated": [
                f"{market}:{symbol}"
                for market in MARKETS
                for symbol in sorted(stored_analyses.get(market, {}))
            ],
        }
    elif analysis is not None:
        # operating path: bind the located cards/universe to the read-only
        # local context collectors — grouped per market so each market's
        # cards use that market's own completed_session_date (NEWS/DART
        # freshness is per-market, never collapsed to a shared minimum) —
        # then feed build_analyses()
        local_paths = {
            name: value
            for name, value in paths.items()
            if name in ("playbook_root", "news_path", "dart_path")
        }
        universe = _local_universe(paths.get("watchlist_root"))
        bound_items: dict = {}
        bound_card_count = 0
        for market, item in located.items():
            if item is None or not item.report.cards:
                continue
            market_cards = tuple(
                (market, card.symbol, card.name) for card in item.report.cards
            )
            bound_items.update(
                build_local_context_items(
                    market_cards,
                    universe=universe,
                    completed_session_date=str(item.completed_session_date),
                    **local_paths,
                )
            )
            bound_card_count += len(market_cards)
        evidence["local_context"] = {
            "cards": bound_card_count,
            "items": len(bound_items),
        }
        if context_items is not None:
            bound_items = {**bound_items, **dict(context_items)}
        analyses, evidence["analysis"] = build_analyses(
            located,
            contexts,
            analysis=analysis,
            search=search,
            fetch=fetch,
            context_items=bound_items,
            validated_card=validated_card,
        )
    messages = render_decision_cards(reports, contexts, analyses)
    summary = {
        market: (
            None
            if item is None
            else {
                "completed_session_date": item.completed_session_date,
                "identity_sha256": item.identity_sha256,
                "report_sha256": item.report_sha256,
                "campaign_id": item.campaign_id,
                "cards": len(item.report.cards),
                "report_path": str(item.report_path),
            }
        )
        for market, item in located.items()
    }
    summary["evidence"] = evidence
    return messages, summary


def _message_summary(message) -> dict:
    return {
        "market": message.market,
        "completed_session_date": message.completed_session_date,
        "report_sha256": message.report_sha256,
        "parts": len(message.parts),
        "chars": [len(part.encode("utf-16-le")) // 2 for part in message.parts],
        "grounded_sections": list(message.grounded_sections),
        "unavailable_sections": [list(item) for item in message.unavailable_sections],
    }


def main(
    argv=None,
    *,
    environ=None,
    clock=None,
    sender_factory=None,
    gate_path=GATE_PATH,
    receipt_root=RECEIPT_ROOT,
    conditional_fetch=None,
    analysis=None,
    search=None,
    fetch=None,
    context_items=None,
    transport_factory=None,
    authority_reader=None,
    observation=None,
    canary_conditional_live=True,
    canary_json_mode=False,
    **paths,
) -> int:
    if type(canary_json_mode) is not bool:
        raise TypeError("canary_json_mode must be bool")
    parser = argparse.ArgumentParser(
        description="Quant Telegram decision-card controller"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        (
            "preview",
            "render the latest receipt-verified KR/US reports; no send, no write",
        ),
        ("send", "one-attempt canary send of both market messages (gate + env bound)"),
        (
            "canary-preview",
            "finite live canary phase 1: ≤4 bounded model calls (direct Z.AI GLM route, "
            "retry 0, no web search), all cards must validate, sanitized analysis receipt, "
            "NO Telegram send; replay makes zero calls",
        ),
        (
            "canary-send",
            "finite live canary phase 2: send exactly one root message per market (≤2) from the "
            "validated analysis receipt with zero new model calls; replay sends nothing",
        ),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    sub.choices["preview"].add_argument(
        "--text", action="store_true", help="print message parts as plain text"
    )
    sub.choices["preview"].add_argument(
        "--conditional-live",
        action="store_true",
        help="perform the bounded OPEN conditional-order read (Toss, retry 0)",
    )
    sub.choices["preview"].add_argument(
        "--analysis-preview",
        action="store_true",
        help="no-delivery contextual analysis through the injected analysis seam "
        "(live model transport not bound: reports model_seam_unbound)",
    )
    sub.choices["send"].add_argument("--activation-id", required=True)
    sub.choices["send"].add_argument(
        "--override-report-dedupe",
        action="store_true",
        help="resend a SENT/FAILED report; UNKNOWN/RESERVED are never resent",
    )
    sub.choices["send"].add_argument(
        "--markets",
        default="KR,US",
        help="comma-separated subset of KR,US (recovery: --markets US)",
    )
    sub.choices["send"].add_argument(
        "--expect-report-sha256",
        action="append",
        default=[],
        metavar="MARKET=SHA256",
        help="refuse unless the located report digest matches",
    )
    for name in ("canary-preview", "canary-send"):
        sub.choices[name].add_argument(
            "--activation-id",
            required=True,
            help="the armed CANARY gate's activation id; one analysis reservation and "
            "one send attempt per id, never replayed",
        )
    sub.choices["canary-preview"].add_argument(
        "--text",
        action="store_true",
        help="print the analysed message parts as plain text",
    )
    sub.choices["canary-preview"].add_argument(
        "--routing-state",
        type=Path,
        default=ROUTING_STATE_PATH,
        help="route authority markdown (read-only admission)",
    )
    sub.choices["canary-preview"].add_argument(
        "--quota-latest",
        type=Path,
        default=QUOTA_LATEST_PATH,
        help="quota authority JSON (read-only admission, 45 min freshness)",
    )
    sub.choices["canary-preview"].add_argument(
        "--zai-env-file",
        type=Path,
        default=ZAI_ENV_FILE,
        help="owner-checked private env file holding GLM_API_KEY/ZAI_API_KEY; "
        "read per call after the reservation, never printed",
    )
    sub.choices["canary-send"].add_argument(
        "--expect-report-sha256",
        action="append",
        default=[],
        metavar="MARKET=SHA256",
        help="required for BOTH KR and US: refuse unless the located report digest "
        "and the analysed digest both match",
    )
    ack = sub.add_parser(
        "ack", help="operator acknowledgement; never changes machine status"
    )
    ack.add_argument("--activation-id", required=True)
    ack.add_argument("--part", required=True, help="e.g. KR:1")
    ack.add_argument("--note", required=True)
    arm = sub.add_parser("arm")
    arm.add_argument("--activation-id", required=True)
    arm.add_argument("--expires-in-minutes", type=int, default=30)
    sub.add_parser("disable")
    status = sub.add_parser("status")
    status.add_argument("--activation-id", required=True)
    args = parser.parse_args(argv)
    if observation is not None:
        if (
            type(observation) is not dict
            or set(observation) != {"set_id", "round_id", "repair_of", "retest_of"}
            or any(
                not valid_activation_id(observation[name])
                for name in ("set_id", "round_id")
            )
            or any(
                value is not None and not valid_activation_id(value)
                for name, value in observation.items()
                if name in ("repair_of", "retest_of")
            )
        ):
            raise ValueError("observation_linkage_shape")

    environ = dict(os.environ) if environ is None else environ
    now = (clock or (lambda: datetime.now(UTC)))()
    emit = lambda payload: print(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    provider_calls = None

    def conditional_seam(live: bool):
        nonlocal provider_calls
        if conditional_fetch is not None:
            return conditional_fetch
        if not live:
            return None
        try:
            fetch, provider_calls = build_live_conditional_fetch(clock=lambda: now)
        except (
            Exception
        ) as error:  # credential/config absent: fail closed, never crash the send
            provider_calls = {"seam": "unavailable", "error": type(error).__name__}
            return None
        return fetch

    if args.command == "preview":
        # analysis runs only on the explicit no-delivery preview flag AND an injected callable;
        # the live model transport is a later wiring step, so the flag alone stays OFF
        wanted = analysis if args.analysis_preview else None
        messages, summary = render(
            args.report_root,
            now=now,
            analysis=wanted,
            search=search,
            fetch=fetch,
            context_items=context_items,
            conditional_fetch=conditional_seam(args.conditional_live),
            **paths,
        )
        if args.analysis_preview and analysis is None:
            summary["evidence"]["analysis"] = {
                "enabled": False,
                "model_calls": 0,
                "reason": "model_seam_unbound",
            }
        if args.text:
            for message in messages:
                for index, part in enumerate(message.parts, 1):
                    print(
                        f"===== {message.market} part {index}/{len(message.parts)} ====="
                    )
                    print(part)
                    print()
        emit(
            {
                "mode": "preview",
                "sent": 0,
                "reports": summary,
                "provider_calls": provider_calls,
                "messages": [_message_summary(message) for message in messages],
            }
        )
        return EXIT_OK

    if args.command == "arm":
        if (
            not valid_activation_id(args.activation_id)
            or args.expires_in_minutes < 1
            or args.expires_in_minutes > 24 * 60
        ):
            emit({"armed": False, "reason": "invalid_parameters"})
            return EXIT_REFUSED
        expires = now + timedelta(minutes=args.expires_in_minutes)
        write_card_activation(
            gate_path,
            activation_id=args.activation_id,
            mode="CANARY",
            expires_at=expires,
        )
        emit(
            {
                "armed": True,
                "activation_id": args.activation_id,
                "expires_at": expires.isoformat(),
                "gate_path": str(gate_path),
            }
        )
        return EXIT_OK

    if args.command == "disable":
        current = read_card_activation(gate_path, now=now)
        activation_id = current.get("activation_id") or "rollback-off"
        write_card_activation(
            gate_path,
            activation_id=activation_id,
            mode="OFF",
            expires_at=now + timedelta(days=3650),
        )
        emit(
            {
                "disabled": True,
                "gate_path": str(gate_path),
                "readback": read_card_activation(gate_path, now=now)["mode"],
            }
        )
        return EXIT_OK

    if args.command == "status":
        receipts = QuantTelegramCardReceiptStore(receipt_root)
        receipt = (
            receipts.read(args.activation_id)
            if valid_activation_id(args.activation_id)
            else None
        )
        emit(
            {
                "activation_id": args.activation_id,
                "receipt": receipt,
                "gate": read_card_activation(gate_path, now=now),
            }
        )
        return EXIT_OK

    if args.command == "ack":
        receipts = QuantTelegramCardReceiptStore(receipt_root)
        try:
            receipt = receipts.acknowledge(
                args.activation_id, args.part, note=args.note, now=now
            )
        except ValueError as error:
            emit({"acknowledged": False, "reason": str(error)})
            return EXIT_REFUSED
        emit(
            {
                "acknowledged": True,
                "activation_id": args.activation_id,
                "part": args.part,
                "receipt_status": receipt["status"],
            }
        )
        return EXIT_OK

    def print_parts(messages):
        for message in messages:
            for index, part in enumerate(message.parts, 1):
                print(f"===== {message.market} part {index}/{len(message.parts)} =====")
                print(part)
                print()

    if args.command == "canary-preview":
        base = {
            "mode": "canary-preview",
            "sent": 0,
            "activation_id": args.activation_id,
            "model_calls": 0,
        }
        activation = read_card_activation(gate_path, now=now)
        if (
            activation["mode"] != "CANARY"
            or activation["activation_id"] != args.activation_id
        ):
            emit(
                {
                    **base,
                    "off": activation.get("reason") or "activation_mismatch",
                    "gate": activation,
                }
            )
            return EXIT_REFUSED
        receipts = QuantTelegramCardReceiptStore(receipt_root)
        existing = receipts.read_analysis(args.activation_id)
        if (
            existing is not None
        ):  # the id's single analysis attempt is consumed: replay makes no call
            emit(
                {
                    **base,
                    "off": "replay_no_call",
                    "receipt_status": existing["status"],
                    "receipt_model_calls": existing["model_calls"],
                }
            )
            return EXIT_REFUSED
        located = locate_latest_verified_reports(args.report_root)
        off = admit_preview(located)
        if off is not None:
            emit({**base, "off": off})
            return EXIT_REFUSED
        # exact-route admission over the two local authority files (read-only), before any reservation
        try:
            authority = (
                authority_reader
                or _route_authority_reader(args.routing_state, args.quota_latest, now)
            )(None)
        except Exception as error:
            emit(
                {
                    **base,
                    "off": f"route_not_admitted:{getattr(error, 'exit_code', type(error).__name__)}",
                }
            )
            return EXIT_REFUSED
        # atomic exclusive reservation: a concurrent duplicate loses here and replays nothing
        if not receipts.reserve_analysis(
            args.activation_id,
            reserve_analysis_receipt(
                activation_id=args.activation_id,
                located=located,
                route_authority=authority,
                now=now,
            ),
        ):
            existing = receipts.read_analysis(args.activation_id)
            emit(
                {
                    **base,
                    "off": "replay_no_call",
                    "receipt_status": existing["status"],
                    "receipt_model_calls": existing["model_calls"],
                }
            )
            return EXIT_REFUSED
        # the transport exists only after the durable reservation; its credential is
        # resolved per call inside it (never here, never printed)
        # Preserve injected factories' existing closed keyword interface.
        if transport_factory is not None:
            transport = transport_factory(
                model_id=FINITE_CANARY_MODEL_ID,
                env_file=args.zai_env_file,
                timeout_seconds=FINITE_CANARY_CALL_TIMEOUT_SECONDS,
                max_tokens=FINITE_CANARY_MAX_TOKENS,
            )
        else:
            transport = build_live_analyzer_transport(
                model_id=FINITE_CANARY_MODEL_ID,
                env_file=args.zai_env_file,
                timeout_seconds=FINITE_CANARY_CALL_TIMEOUT_SECONDS,
                max_tokens=FINITE_CANARY_MAX_TOKENS,
                json_mode=canary_json_mode,
            )
        # every start is gated by the durable count under the id's lock (cumulative cap 4)
        update = lambda mutate: receipts.update_analysis(args.activation_id, mutate)
        reserve, settle = bind_call_ledger(update=update)
        captured: dict = {}
        persist = lambda key, result: update(
            lambda current: retain_analysis(current, key, result)
        )
        observe = validated_card = None
        if observation is not None:
            # Hash source and the exact per-card checked prompt, never save the prompt.
            source_paths = (
                Path(__file__),
                Path(__file__).resolve().parents[1]
                / "src/tokmo/programs/quant_finite_canary.py",
                Path(__file__).resolve().parents[1]
                / "src/tokmo/programs/quant_contextual_analysis.py",
                Path(__file__).resolve().parents[1]
                / "src/tokmo/records/quant_telegram_card_receipts.py",
            )
            code_sha256 = hashlib.sha256(
                b"\0".join(path.read_bytes() for path in source_paths)
            ).hexdigest()
            validator_sha256 = hashlib.sha256(source_paths[2].read_bytes()).hexdigest()

            def observe(key, finding):
                receipts.observe_failure(
                    args.activation_id,
                    key,
                    dict(
                        finding,
                        linkage=dict(observation),
                        code_sha256=code_sha256,
                        validator_sha256=validator_sha256,
                    ),
                )

            def validated_card(**kwargs):
                try:
                    snapshot = partial_card_snapshot(**kwargs)
                except ValueError as error:
                    if error.args != ("partial_card_unrenderable",):
                        raise
                    snapshot = {"omitted": "partial_card_unrenderable"}
                key = kwargs["key"]
                update(
                    lambda current: dict(
                        current,
                        partial_cards={
                            **current.get("partial_cards", {}),
                            key: snapshot,
                        },
                    )
                )

        try:
            messages, summary = render(
                args.report_root,
                now=now,
                analysis=capped_analysis(
                    transport,
                    reserve=reserve,
                    settle=settle,
                    captured=captured,
                    persist=persist,
                    observe=observe,
                ),
                validated_card=validated_card,
                conditional_fetch=conditional_seam(canary_conditional_live),
                **paths,
            )
        except Exception:  # calls already made stay consumed; the id is never replayed
            receipt = update(
                lambda current: dict(
                    current,
                    status="FAILED",
                    settled_at=now.isoformat(),
                    off=f"render_failure:{type(error).__name__}",
                )
            )
            emit(
                {
                    **base,
                    "model_calls": receipt["model_calls"],
                    "status": "FAILED",
                    "off": receipt["off"],
                }
            )
            return EXIT_REFUSED
        receipt = update(
            lambda current: settle_preview(
                current,
                analysis_evidence=summary["evidence"]["analysis"],
                captured=captured,
                messages=messages,
                now=now,
            )
        )
        if args.text:
            print_parts(messages)
        emit(
            {
                **base,
                "model_calls": receipt["model_calls"],
                "status": receipt["status"],
                "off": receipt["off"],
                "validated": receipt["validated"],
                "rejected": receipt["rejected"],
                "reports": summary,
                "provider_calls": provider_calls,
                "receipt_path": str(receipts.analysis_path(args.activation_id)),
                "messages": [_message_summary(message) for message in messages],
            }
        )
        return EXIT_OK if receipt["status"] == "ANALYZED" else EXIT_REFUSED

    if args.command == "canary-send":
        base = {
            "mode": "canary-send",
            "sent": 0,
            "activation_id": args.activation_id,
            "model_calls": 0,
        }
        activation = read_card_activation(gate_path, now=now)
        if (
            activation["mode"] != "CANARY"
            or activation["activation_id"] != args.activation_id
        ):
            emit(
                {
                    **base,
                    "off": activation.get("reason") or "activation_mismatch",
                    "gate": activation,
                }
            )
            return EXIT_REFUSED
        expected = {}
        for item in args.expect_report_sha256:
            market, separator, digest = item.partition("=")
            if separator != "=" or market not in MARKETS or len(digest) != 64:
                emit({**base, "off": "invalid_expected_digest"})
                return EXIT_REFUSED
            expected[market] = digest.lower()
        receipts = QuantTelegramCardReceiptStore(receipt_root)
        analysis_receipt = receipts.read_analysis(args.activation_id)
        located = locate_latest_verified_reports(args.report_root)
        off = admit_send(receipt=analysis_receipt, located=located, expected=expected)
        if off is not None:
            emit({**base, "off": off})
            return EXIT_REFUSED
        bound = bind_stock_bot_env(environ)
        if bound is None:
            emit({**base, "off": "recipient_unbound"})
            return EXIT_REFUSED
        token, chat_id = bound
        messages, summary = render(
            args.report_root,
            now=now,
            conditional_fetch=conditional_seam(True),
            stored_analyses=analyses_from_receipt(analysis_receipt),
            **paths,
        )
        # Telegram cap, then byte-identity with the previewed messages — both before any transport
        off = check_messages(messages) or check_message_digests(
            messages, analysis_receipt
        )
        if off is not None:
            emit({**base, "off": off, "reports": summary})
            return EXIT_REFUSED
        telegram_send = (
            pre_entity_sender if sender_factory is None else sender_factory
        )(token, chat_id)
        result = run_quant_telegram_card_canary(
            activation=activation,
            receipts=receipts,
            activation_id=args.activation_id,
            messages=messages,
            telegram_send=telegram_send,
            now=now,
        )
        receipt = receipts.read(args.activation_id)
        emit(
            {
                **base,
                "sent": result["sent"],
                "result": result,
                "reports": summary,
                "provider_calls": provider_calls,
                "message_digests_verified": [message.market for message in messages],
                "receipt_status": None if receipt is None else receipt["status"],
                "receipt_path": str(receipts.path(args.activation_id)),
                "messages": [_message_summary(message) for message in messages],
            }
        )
        if result["off"] is not None:
            return EXIT_REFUSED
        return (
            EXIT_OK
            if result["failed"] == 0
            and result["unknown"] == 0
            and result["skipped"] == 0
            else EXIT_NOT_SENT
        )

    # send
    activation = read_card_activation(gate_path, now=now)
    if (
        activation["mode"] != "CANARY"
        or activation["activation_id"] != args.activation_id
    ):
        emit(
            {
                "sent": 0,
                "off": activation.get("reason") or "activation_mismatch",
                "gate": activation,
            }
        )
        return EXIT_REFUSED
    markets = tuple(
        dict.fromkeys(item.strip() for item in args.markets.split(",") if item.strip())
    )
    if not markets or any(market not in MARKETS for market in markets):
        emit({"sent": 0, "off": "invalid_markets"})
        return EXIT_REFUSED
    expected = {}
    for item in args.expect_report_sha256:
        market, separator, digest = item.partition("=")
        if separator != "=" or market not in markets or len(digest) != 64:
            emit({"sent": 0, "off": "invalid_expected_digest"})
            return EXIT_REFUSED
        expected[market] = digest.lower()
    bound = bind_stock_bot_env(environ)
    if bound is None:
        emit({"sent": 0, "off": "recipient_unbound"})
        return EXIT_REFUSED
    token, chat_id = bound
    messages, summary = render(
        args.report_root, now=now, conditional_fetch=conditional_seam(True), **paths
    )
    messages = tuple(message for message in messages if message.market in markets)
    for message in messages:
        if (
            message.market in expected
            and message.report_sha256 != expected[message.market]
        ):
            emit(
                {
                    "sent": 0,
                    "off": f"report_digest_mismatch:{message.market}",
                    "located_report_sha256": message.report_sha256,
                    "reports": summary,
                }
            )
            return EXIT_REFUSED
    telegram_send = (pre_entity_sender if sender_factory is None else sender_factory)(
        token, chat_id
    )
    receipts = QuantTelegramCardReceiptStore(receipt_root)
    result = run_quant_telegram_card_canary(
        activation=activation,
        receipts=receipts,
        activation_id=args.activation_id,
        messages=messages,
        telegram_send=telegram_send,
        now=now,
        override_report_dedupe=args.override_report_dedupe,
    )
    receipt = receipts.read(args.activation_id)
    emit(
        {
            "activation_id": args.activation_id,
            "markets": list(markets),
            "result": result,
            "reports": summary,
            "provider_calls": provider_calls,
            "receipt_status": None if receipt is None else receipt["status"],
            "receipt_path": str(receipts.path(args.activation_id)),
            "messages": [_message_summary(message) for message in messages],
        }
    )
    if result["off"] is not None:
        return EXIT_REFUSED
    return (
        EXIT_OK
        if result["failed"] == 0 and result["unknown"] == 0 and result["skipped"] == 0
        else EXIT_NOT_SENT
    )


if __name__ == "__main__":
    raise SystemExit(main())
