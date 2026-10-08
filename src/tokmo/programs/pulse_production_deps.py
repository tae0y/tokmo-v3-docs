"""Pulse production dependency composition seam.

Plan §27: inject canonical stores from an isolated root into PulseDepsV1
without touching the existing ``tokmo.composition`` immutable seal. This
module is a separate DI factory that reads CalendarLookaheadStore,
CurrentHoldingsStore (latest complete), AccountNativeRefStoreV1,
watchlists, LatestQuoteStore, TradeLedger, OrderScanState, and the
dialogue store from a declared isolated root and returns them as
read-only callables for ``PulseDepsV1``.

Tests use fixtures with zero network access. The Toss read adapter reuse is
through dependency injection; production transport is assembled only by the
explicit production composition path and remains guarded by lane ownership.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import sqlite3
import stat
import time
from types import MappingProxyType
from typing import Callable, Mapping

from tokmo.programs.integrated_pulse import PulseDepsV1
from tokmo.programs.news_feedback import build_news_callback_handler
from tokmo.programs.news_event_admission import reconcile_news_outbox
from tokmo.programs.news_alert_delivery import (
    live_delivery_authorized,
    run_alert_delivery,
    run_canary_alert_delivery,
    run_live_alert_delivery,
)
from tokmo.programs.news_official_evidence import (
    build_official_evidence_reader,
    build_official_receipt_sink,
)
from tokmo.programs.news_production import NewsProducerDepsV1
from tokmo.programs.news_topic_discovery import _PUBLIC_ALIASES, _normalise
from tokmo.records.account_native_refs import (
    AccountNativeRefStoreV1,
    NativeRefMappingV1,
)
from tokmo.records.calendar_lookahead import CalendarLookaheadStoreV1
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.dialogue_activation import (
    DialogueActivationV1,
    activation_from_payload,
)
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.records.external_news_query import read_discovery_documents_v1
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.playbook_identity import read_instrument_name
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.current_holdings import classify_market
from tokmo.watchlists import WatchlistError, load_watchlist_identities
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NEWS_CLASSIFIER_PROVIDER,
    NewsClassifierFailure,
)
from tokmo.values.news_config import (
    DISCOVERY_MAX_DOCUMENTS,
    PRODUCER_CANDIDATES_PER_TICK,
    PRODUCER_CANDIDATE_ATTEMPT_MAX,
    PRODUCER_CANDIDATE_TTL_SECONDS,
    PRODUCER_INTERVAL_SECONDS,
    PRODUCER_LEASE_SECONDS,
    PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS,
)
from tokmo.values.operation_lane_registry import OPERATION_LANE_REGISTRY_V1
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.session_gate import Market


class PulseProductionDepsError(ValueError):
    pass


_CADENCE_POLICY = "cadence-300s-market-portfolio-v1"
_CADENCE_BOUNDARY_WAIT_SECONDS = 12.0


def wait_for_cadence_complete(root: Path, scheduled_for_utc: datetime,
                              *, timeout_seconds: float =
                              _CADENCE_BOUNDARY_WAIT_SECONDS) -> bool:
    """Wait boundedly for the same-slot production cadence to COMPLETE.

    Read-only SQLite access only. FAILED/PARTIAL/other terminal states fail
    immediately; missing/STARTED polls until the bounded deadline.  Pulse has
    not recorded or started a provider wire when this function runs.
    """
    db = Path(root) / "ops" / "cadence_ticks.sqlite3"
    slot = scheduled_for_utc.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000000Z")
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    first_attempt = True
    while first_attempt or time.monotonic() <= deadline:
        first_attempt = False
        if db.exists():
            try:
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True,
                                     timeout=0.2) as conn:
                    row = conn.execute(
                        "SELECT status FROM cadence_tick_receipts "
                        "WHERE policy_version=? AND scheduled_for_utc=?",
                        (_CADENCE_POLICY, slot)).fetchone()
            except sqlite3.Error:
                return False
            if row is not None:
                if row[0] == "COMPLETE":
                    return True
                if row[0] != "STARTED":
                    return False
        time.sleep(0.1)
    return False


def _make_unique_id_factory(clock: Callable[[], datetime]) -> Callable[[], str]:
    """Build a ledger id_factory that never collides within a single tick.

    The primary key is ``import_batch_id`` / ``event_id``. A frozen clock
    can return the same timestamp for multiple accounts in one tick, so a
    pure clock-based id would collide. This factory combines a
    secrets-based random token with a process-local monotonic counter,
    ensuring uniqueness even when the clock is frozen.

    The counter is also injected so deterministic tests can supply their
    own via ``id_factory`` parameter on ``TradeLedgerStore``.
    """
    import secrets
    counter = {"n": 0}

    def _factory() -> str:
        token = secrets.token_hex(8)
        n = counter["n"]
        counter["n"] = n + 1
        return f"pulse-prod-{clock().isoformat()}-{n:04d}-{token}"

    return _factory


def _news_registry_entity(aliases):
    """The one public-catalog entity any official alias matches, else None.
    An ambiguous multi-entity match fails closed (no merge)."""
    keys = {key for alias in aliases
            for key, known in _PUBLIC_ALIASES.items()
            if key.startswith("ent:") and _normalise(str(alias)) in known}
    return keys.pop() if len(keys) == 1 else None


def resolve_news_identity_pairs(entries):
    """Official local (symbol, market, names) observations → canonical NEWS
    identity pairs (M2).

    A pair whose official alias matches an existing canonical registry
    identity enriches that identity (SK하이닉스/000660 → ``ent:sk-hynix``,
    never a second ``ent:000660``). Without a registry match the identity
    is a stable market-qualified key (``ent:kr:005930``) so cross-market
    symbol collisions can never merge; with no market either, the pair is
    dropped (fail closed — no bare collision-prone identity). The explicit
    symbol always stays an alias of the resolved identity."""
    pairs = {}
    for symbol, market, names in entries:
        aliases = tuple(dict.fromkeys(
            str(item) for item in (symbol, *tuple(names))
            if str(item).strip()))
        if not aliases:
            continue
        key = _news_registry_entity(aliases)
        if key is None:
            token = getattr(market, "value", market)
            if type(token) is not str or not token.strip():
                continue
            key = f"ent:{token.casefold()}:{str(symbol).casefold()}"
        pairs[key] = tuple(dict.fromkeys(pairs.get(key, ()) + aliases))
    return pairs


# YouTube 12man typed hypothesis index keys → taxonomy interest lanes:
# entities feed identity candidates/scope; metric/effect/source-event terms
# feed their typed lanes only, never entity alias identity.
_HYPOTHESIS_INTEREST_LANES = {"ENTITY": "ENTITY", "METRIC": "METRIC",
                              "EFFECT": "EFFECT", "SOURCE_EVENT": "CATALYST"}


def enumerate_news_interest_terms(ops_kernel, watchlist_root):
    """Typed interest terms from the approved sources that actually exist
    (M3): the Git-tracked watchlists (user identity intent, symbol + name)
    and the open YouTube 12man typed hypotheses already in the ops kernel
    (safe read-only enumerators ``read_open_hypotheses`` /
    ``read_hypothesis_index``). Each source degrades to nothing when
    absent or malformed — no guessed terms, no full-market preload."""
    typed = {}

    def add(lane, term):
        term = str(term).strip()
        if term:
            typed.setdefault(lane, set()).add(term)

    if watchlist_root is not None:
        try:
            identities = load_watchlist_identities(Path(watchlist_root))
        except WatchlistError:
            identities = {}
        for rows in identities.values():
            for symbol, name in rows:
                add("ENTITY", symbol)
                add("ENTITY", name)
    if ops_kernel is not None:
        try:
            hypotheses = ops_kernel.read_open_hypotheses()
        except Exception:
            hypotheses = ()
        for hypothesis in hypotheses:
            try:
                index_keys = ops_kernel.read_hypothesis_index(
                    hypothesis.hypothesis_id)
            except Exception:
                continue
            for key in index_keys:
                lane = _HYPOTHESIS_INTEREST_LANES.get(
                    getattr(key.kind, "value", None))
                if lane is None:
                    continue
                add(lane, str(key.value).split(":", 1)[-1])
    return {lane: tuple(sorted(terms)) for lane, terms in typed.items()}


_NEWS_GATE_DISABLED = DialogueActivationV1(False, None, None)
NEWS_GATE_MAX_BYTES = 4096
# Canary window (approval packet §2.2, extended by user direction
# 2026-08-22): a bounded span in (0, 72h] so a weekend/holiday daytime
# validation fits one approval, never an open-ended canary. The durable
# one-attempt cap is keyed by activation_id and spans the whole window —
# extending window_end never re-arms it.
NEWS_CANARY_WINDOW_MAX_SECONDS = 72 * 3600
# Legacy observation windows keep their seven-day bound. User direction
# 2026-09-14 adds explicit continuous LIVE operation after stabilization.
NEWS_LIVE_WINDOW_MAX_SECONDS = 7 * 24 * 3600
_ACTIVATION_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


@dataclass(frozen=True)
class NewsDeliveryV1:
    """Independent NEWS Telegram delivery judgment (approval packet §2.1).

    ``mode`` is "OFF", "TELEGRAM_CANARY", or "TELEGRAM_LIVE"; every other
    field is set only in an armed mode. The parser fails closed: any
    missing, malformed, or out-of-contract field resolves the whole
    judgment to OFF while leaving the SHADOW producer judgment untouched
    (independent reversibility). ``predecessor_activation_id`` names the
    canary activation whose durable SENT + duplicate-observation proof is
    a live-mode precondition checked against the ledger, not this file."""
    mode: str
    activation_id: "str | None"
    cutoff: "datetime | None"
    window_end: "datetime | None"
    attempt_cap: "int | None"
    predecessor_activation_id: "str | None" = None
    continuous: bool = False

    def canary_active(self, now: datetime) -> bool:
        """True only inside the bounded [cutoff, window_end) canary window."""
        return (self.mode == "TELEGRAM_CANARY"
                and self.cutoff is not None and self.window_end is not None
                and self.cutoff <= now < self.window_end)

    def live_active(self, now: datetime) -> bool:
        """Live operation starts at cutoff, with an optional explicit expiry."""
        return (self.mode == "TELEGRAM_LIVE"
                and self.cutoff is not None and self.cutoff <= now
                and ((self.continuous is True and self.window_end is None)
                     or (not self.continuous and self.window_end is not None and now < self.window_end)))


NEWS_DELIVERY_OFF = NewsDeliveryV1("OFF", None, None, None, None)


def _valid_activation_id(value) -> bool:
    return (type(value) is str and 1 <= len(value) <= 64
            and set(value) <= _ACTIVATION_ID_CHARS
            and value[0] not in "._-")


def _parse_aware_window_end(payload):
    window_raw = payload.get("window_end")
    if type(window_raw) is not str:
        return None
    try:
        window_end = datetime.fromisoformat(window_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if window_end.tzinfo is None or window_end.utcoffset() is None:
        return None
    return window_end.astimezone(timezone.utc)


def news_delivery_from_payload(payload, activation, producer_mode):
    """Pure fail-closed delivery judgment over one gate byte string.

    Delivery requires the base activation to be valid AND the producer to
    be the authorized SHADOW token AND every field of the selected mode to
    match its approved contract exactly. TELEGRAM_CANARY keeps the
    one-attempt contract: telegram_attempt_cap exactly int 1 (bool
    excluded) and a bounded aware window_end inside
    (cutoff, cutoff + NEWS_CANARY_WINDOW_MAX_SECONDS].
    TELEGRAM_LIVE requires a distinct predecessor_activation_id, no
    telegram_attempt_cap key at all (no arbitrary cap; ledger dedupe and
    outbox claims govern), and a bounded aware window_end inside
    (cutoff, cutoff + NEWS_LIVE_WINDOW_MAX_SECONDS] requiring explicit
    renewal. Explicit continuous=true LIVE operation instead has no window_end.
    Canary operation always remains bounded. Anything else resolves to OFF."""
    if type(payload) is not dict or not activation.enabled \
            or producer_mode != "SHADOW":
        return NEWS_DELIVERY_OFF
    mode = payload.get("delivery_mode")
    if mode not in ("TELEGRAM_CANARY", "TELEGRAM_LIVE"):
        return NEWS_DELIVERY_OFF
    if not _valid_activation_id(payload.get("activation_id")):
        return NEWS_DELIVERY_OFF
    continuous=payload.get('continuous',False)
    if type(continuous) is not bool or activation.cutoff is None:
        return NEWS_DELIVERY_OFF
    window_end = _parse_aware_window_end(payload)
    if continuous:
        if mode!='TELEGRAM_LIVE' or payload.get('window_end') is not None:
            return NEWS_DELIVERY_OFF
    elif window_end is None:
        return NEWS_DELIVERY_OFF
    if mode == "TELEGRAM_CANARY":
        cap = payload.get("telegram_attempt_cap")
        if type(cap) is not int or cap != 1:
            return NEWS_DELIVERY_OFF
        span = window_end - activation.cutoff
        if not (timedelta(0) < span
                <= timedelta(seconds=NEWS_CANARY_WINDOW_MAX_SECONDS)):
            return NEWS_DELIVERY_OFF
        return NewsDeliveryV1("TELEGRAM_CANARY", payload["activation_id"],
                              activation.cutoff, window_end, 1)
    predecessor = payload.get("predecessor_activation_id")
    if not _valid_activation_id(predecessor) \
            or predecessor == payload["activation_id"]:
        return NEWS_DELIVERY_OFF
    if "telegram_attempt_cap" in payload:
        return NEWS_DELIVERY_OFF
    if continuous:
        return NewsDeliveryV1('TELEGRAM_LIVE',payload['activation_id'],
                              activation.cutoff,None,None,predecessor,True)
    span = window_end - activation.cutoff
    if not (timedelta(0) < span
            <= timedelta(seconds=NEWS_LIVE_WINDOW_MAX_SECONDS)):
        return NEWS_DELIVERY_OFF
    return NewsDeliveryV1("TELEGRAM_LIVE", payload["activation_id"],
                          activation.cutoff, window_end, None, predecessor)


class NewsGateSnapshotV1(tuple):
    """One-read NEWS gate snapshot: unpacks exactly like the historical
    ``(activation, producer_mode)`` pair, and additionally carries the
    independent ``delivery`` judgment derived from the same byte string so
    producer and delivery can never split-brain across two file reads."""

    def __new__(cls, activation, producer_mode,
                delivery: NewsDeliveryV1 = NEWS_DELIVERY_OFF):
        snapshot = tuple.__new__(cls, (activation, producer_mode))
        snapshot.delivery = delivery
        return snapshot


_NEWS_GATE_OFF_SNAPSHOT = NewsGateSnapshotV1(_NEWS_GATE_DISABLED, "OFF")


def read_news_gate(root) -> "tuple[DialogueActivationV1, str]":
    """Single-descriptor NEWS gate snapshot (SDS §5.1): open once, verify the
    opened object with fstat, read boundedly from the same descriptor, and
    derive activation, ``producer_mode``, and the delivery judgment from that
    one byte string. Every degenerate state resolves to (disabled, "OFF")
    with delivery OFF; "SHADOW" is the only authorized producer token."""
    path = Path(root) / "news_activation.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:                       # absent/denied/symlink(ELOOP)
        return _NEWS_GATE_OFF_SNAPSHOT
    try:
        info = os.fstat(fd)               # verify the opened object, not path
        if not (stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.geteuid()
                and info.st_size <= NEWS_GATE_MAX_BYTES):
            return _NEWS_GATE_OFF_SNAPSHOT
        raw = os.read(fd, NEWS_GATE_MAX_BYTES + 1)
    except OSError:
        return _NEWS_GATE_OFF_SNAPSHOT
    finally:
        os.close(fd)
    if len(raw) > NEWS_GATE_MAX_BYTES:
        return _NEWS_GATE_OFF_SNAPSHOT
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _NEWS_GATE_OFF_SNAPSHOT
    activation = activation_from_payload(payload)
    if not activation.enabled:
        return _NEWS_GATE_OFF_SNAPSHOT
    mode = "SHADOW" if payload.get("producer_mode") == "SHADOW" else "OFF"
    return NewsGateSnapshotV1(
        activation, mode, news_delivery_from_payload(payload, activation, mode))


def read_news_activation(root):
    """Equivalence-regression wrapper (R-GATE-11); no production caller."""
    return read_news_gate(root)[0]


def build_pulse_production_deps(
    *,
    root: Path,
    clock: Callable[[], datetime],
    watchlist_symbols: Mapping[Market, frozenset],
    dialogue_cutoff: datetime,
    lookahead_store: "CalendarLookaheadStoreV1 | None" = None,
    holdings_store: "CurrentHoldingsStore | None" = None,
    native_refs_store: "AccountNativeRefStoreV1 | None" = None,
    quote_store: "LatestQuoteStore | None" = None,
    ledger_store: "TradeLedgerStore | None" = None,
    scan_state_store: "OrderScanStateStore | None" = None,
    dialogue_store: "ExecutionDialogueStore | None" = None,
    news_ops_kernel: "OpsKernelStore | None" = None,
    token_fetch: "Callable | None" = None,
    quotes_fetch: "Callable | None" = None,
    orders_page_fetch: "Callable | None" = None,
    order_detail_fetch: "Callable | None" = None,
    conditional_orders_fetch: "Callable | None" = None,
    playbook_root: "Path | None" = None,
    ai_evaluator: "Callable | None" = None,
    telegram_sender: "Callable | None" = None,
    news_telegram_sender: "Callable | None" = None,
    updates_provider: "Callable | None" = None,
    callback_ack: "Callable | None" = None,
    dialogue_quote_reader: "Callable | None" = None,
    playbook_validator: "Callable | None" = None,
    quotes_provider_enabled: bool = False,
    execution_provider_enabled: bool = False,
    conditional_provider_enabled: bool = False,
    dialogue_activation_enabled: bool = False,
    quant_market_close_runner: "Callable | None" = None,
    quant_input_capture: "Callable | None" = None,
    opportunity_capture: "Callable | None" = None,
    tick_cleanup: "Callable | None" = None,
    news_gate: "tuple[DialogueActivationV1, str] | None" = None,
    news_documents_path: "Path | None" = None,
    watchlist_root: "Path | None" = None,
    official_evidence_db_path: "Path | None" = None,
    official_live_seam=None,
) -> PulseDepsV1:
    """Build PulseDepsV1 wired to isolated-root canonical stores.

    All stores default to paths under the isolated ``root``. Caller can
    inject pre-created stores (e.g. from production canonical root for
    read-only access) — these take precedence. Provider callbacks default
    to no-op stubs that are never live transport.
    """
    root = Path(root)

    # Default stores under isolated root
    if lookahead_store is None:
        lookahead_store = CalendarLookaheadStoreV1(
            root / "ops" / "calendar_lookahead.sqlite3", clock=clock)
    if holdings_store is None:
        holdings_store = CurrentHoldingsStore(
            root / "stores" / "current_holdings.json", clock=clock)
    if native_refs_store is None:
        native_refs_store = AccountNativeRefStoreV1(
            root / "ops" / "account_native_refs.sqlite3", clock=clock)
    if quote_store is None:
        quote_store = LatestQuoteStore(
            root / "stores" / "latest_quotes.sqlite3", clock=clock)
    if ledger_store is None:
        ledger_store = TradeLedgerStore(
            root / "stores" / "trade_ledger.sqlite3", clock=clock,
            id_factory=_make_unique_id_factory(clock))
    if scan_state_store is None:
        scan_state_store = OrderScanStateStore(
            root / "stores" / "order_scan_state.sqlite3", clock=clock)
    if dialogue_store is None:
        dialogue_store = ExecutionDialogueStore(
            root / "stores" / "execution_dialogue.sqlite3", clock=clock)
    news_callback_handler=None
    news_outbox_reconcile=None
    news_delivery_runner=None
    news_producer_deps=None
    if news_gate is None:
        news_gate=read_news_gate(root)
    news_activation,news_producer_mode=news_gate
    if news_activation.enabled:
        news_episode_store=NewsEpisodeStore(root / "stores" / "news_episodes.sqlite3")
        if news_ops_kernel is None:
            news_ops_kernel=OpsKernelStore(root / "ops" / "ops_kernel.sqlite3",stable_phase_ids=frozenset())
        news_callback_handler=build_news_callback_handler(news_episode_store,news_ops_kernel.read_outbox,now=clock)
        # G3 delivery judgment (approval packet §2.1/§2.2): TELEGRAM is
        # possible only with the approved canary gate AND the canonical
        # injected sender AND an unconsumed durable attempt ledger inside the
        # fixed window. Plain-tuple snapshots (legacy callers) carry no
        # delivery attribute and therefore stay OFF.
        news_delivery=getattr(news_gate,"delivery",NEWS_DELIVERY_OFF)
        if news_telegram_sender is None:
            news_telegram_sender = telegram_sender
        if news_delivery.mode in ("TELEGRAM_CANARY","TELEGRAM_LIVE") \
                and news_telegram_sender is None:
            news_delivery=NEWS_DELIVERY_OFF
        def news_delivery_enabled(observed_now):
            if news_delivery.mode=="TELEGRAM_LIVE":
                return live_delivery_authorized(
                    news_episode_store,news_delivery,observed_now)[0]
            return (news_delivery.canary_active(observed_now)
                    and news_episode_store.canary_attempt(
                        news_delivery.activation_id) is None)
        def news_outbox_reconcile(observed_now):
            enabled=news_delivery_enabled(observed_now)
            return reconcile_news_outbox(store=news_episode_store,ops_kernel=news_ops_kernel,
                role=ProducerRole.HERMES,activation_enabled=enabled,
                telegram_cutoff=news_delivery.cutoff if enabled else None,now=observed_now)
        def news_delivery_runner(observed_now):
            # In canary mode the canary runner itself enforces window, cap,
            # and reservation-before-transport, reporting the OFF reason; in
            # live mode the live runner enforces the durable predecessor
            # proof, redaction guard, and delivery-only trip semantics.
            if news_delivery.mode=="TELEGRAM_CANARY":
                return run_canary_alert_delivery(ops_kernel=news_ops_kernel,role=ProducerRole.HERMES,
                    ledger=news_episode_store,delivery=news_delivery,
                    telegram_send=news_telegram_sender,keyboard_resolver=lambda alert_id:None,
                    delivery_binder=news_delivery_binder,now=observed_now)
            if news_delivery.mode=="TELEGRAM_LIVE":
                return run_live_alert_delivery(ops_kernel=news_ops_kernel,role=ProducerRole.HERMES,
                    ledger=news_episode_store,delivery=news_delivery,
                    telegram_send=news_telegram_sender,keyboard_resolver=lambda alert_id:None,
                    delivery_binder=news_delivery_binder,now=observed_now)
            return run_alert_delivery(ops_kernel=news_ops_kernel,role=ProducerRole.HERMES,
                telegram_send=_no_transport,keyboard_resolver=lambda alert_id:None,
                delivery_binder=news_delivery_binder,now=observed_now)
        def news_delivery_binder(alert_id, provider_id, *, now):
            # The new worker reads canonical delivery receipts directly.
            if not alert_id.startswith("nda-"):
                return news_episode_store.bind_delivery_by_ops_alert(alert_id, provider_id, now=now)
        from tokmo.programs.news_decision_config import read_decision_config
        if news_producer_mode=="SHADOW" and not read_decision_config(root)["enabled"]:
            # Inert producer dependency bundle (SDS §6.8-1): supply seams and
            # bounds only — the executable stage lives solely inside
            # run_pulse_once's held-flock scope.
            news_documents_path = (Path(news_documents_path)
                                   if news_documents_path is not None else
                                   root / "news" / "external_news_v4.sqlite3")
            def news_documents_reader(window_from):
                return read_discovery_documents_v1(
                    news_documents_path,window_from=window_from,
                    max_rows=DISCOVERY_MAX_DOCUMENTS)
            def news_holding_terms():
                # Public instrument names only (SDS §9-6): scope, quantity,
                # and price data never leave this factory.
                terms=set()
                try:
                    state=holdings_store.read()
                except Exception:
                    return frozenset()
                if state is None:
                    return frozenset()
                for account in state.accounts:
                    for item in account.holdings:
                        terms.add(item.symbol)
                        if playbook_root is None:
                            continue
                        for market in ("KR","US"):
                            name=read_instrument_name(
                                playbook_root,account_scope=account.account_scope,
                                market=market,symbol=item.symbol)
                            if name:
                                terms.add(name)
                return frozenset(terms)
            def news_identity_pairs():
                # Official local identity pairs for taxonomy stage 1 (M2):
                # holdings + watchlist entries resolve through the canonical
                # registry — never a bare-symbol identity (SDS §9-6 scope
                # rule holds: public symbol/name only).
                entries=[]
                try:
                    state=holdings_store.read()
                except Exception:
                    state=None
                for account in (state.accounts if state is not None else ()):
                    for item in account.holdings:
                        market=classify_market(
                            getattr(item,"market_raw",None),
                            getattr(item,"market_field",None))
                        names=[]
                        matched=[]
                        if playbook_root is not None:
                            markets=(("KR","US") if market is None
                                     else (market.value,))
                            for market_token in markets:
                                name=read_instrument_name(
                                    playbook_root,
                                    account_scope=account.account_scope,
                                    market=market_token,symbol=item.symbol)
                                if name:
                                    names.append(name)
                                    matched.append(market_token)
                        if market is None and len(matched)==1:
                            market=Market(matched[0])
                        entries.append((item.symbol,market,tuple(names)))
                if watchlist_root is not None:
                    try:
                        identities=load_watchlist_identities(
                            Path(watchlist_root))
                    except WatchlistError:
                        identities={}
                    for market,rows in identities.items():
                        for symbol,name in rows:
                            entries.append((symbol,market,(name,)))
                return resolve_news_identity_pairs(entries)
            def news_interest_terms():
                # Typed interest terms (M3): watchlists + open YouTube typed
                # hypotheses via the canonical read-only enumerators.
                return enumerate_news_interest_terms(
                    news_ops_kernel,watchlist_root)
            def _classifier_unbound(request):
                raise NewsClassifierFailure("MODEL_UNAVAILABLE")
            news_producer_deps=NewsProducerDepsV1(
                episode_store=news_episode_store,
                ops_kernel=news_ops_kernel,
                role=ProducerRole.HERMES,
                documents_reader=news_documents_reader,
                holding_terms=news_holding_terms,
                classifier_call=_classifier_unbound,
                classifier_config_path=root / "news_classifier_model.json",
                admitted_catalog=MappingProxyType(
                    {DEFAULT_NEWS_CLASSIFIER_MODEL_ID:
                     NEWS_CLASSIFIER_PROVIDER}),
                gate_cutoff=news_activation.cutoff,
                slot_seconds=PRODUCER_INTERVAL_SECONDS,
                candidates_per_tick=PRODUCER_CANDIDATES_PER_TICK,
                lease_seconds=PRODUCER_LEASE_SECONDS,
                backoff_cap_seconds=PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS,
                ttl_seconds=PRODUCER_CANDIDATE_TTL_SECONDS,
                attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX,
                delivery_activation=news_delivery_enabled,
                classifier_queue=news_episode_store.enqueue_classifier_work,
                interest_terms=news_interest_terms,
                identity_pairs=news_identity_pairs,
                # A2 §Z7 (decision 18): the real two-tier official reader
                # is bound here. Tier 1 reads the canonical read-only bulk
                # store; the tier-2 live seam arrives only from
                # composition.py (live-import guard) and stays a lazy
                # closure — nothing is touched at assembly time (§V5).
                official_evidence=build_official_evidence_reader(
                    official_evidence_db_path
                    if official_evidence_db_path is not None
                    else (Path.home() / ".local" / "share" / "tokmo-v3").resolve()
                    / "opendart-bulk.sqlite3",
                    live=official_live_seam,
                    receipt_sink=build_official_receipt_sink(root)))

    def lookahead_reader(market: Market):
        return lookahead_store.read_days(market)  # type: ignore[union-attr]

    def holdings_reader():
        return holdings_store.read()  # type: ignore[union-attr]

    def native_refs_reader() -> NativeRefMappingV1:
        return native_refs_store.read_refs()  # type: ignore[union-attr]

    def dialogue_qr(symbol):
        if dialogue_quote_reader is not None:
            return dialogue_quote_reader(symbol)
        return None

    def pb_validator(pb_root):
        if playbook_validator is not None:
            return playbook_validator(pb_root)
        return []

    # Provider stubs: no live transport. These exist only so PulseDepsV1
    # construction doesn't fail — they must never be called without
    # explicit provider wiring.
    def _no_transport(*args, **kwargs):
        raise PulseProductionDepsError(
            "no provider transport wired in G2 DI factory")

    return PulseDepsV1(
        clock=clock,
        lookahead_reader=lookahead_reader,
        holdings_reader=holdings_reader,
        native_refs_reader=native_refs_reader,
        watchlist_symbols=MappingProxyType(dict(watchlist_symbols)),
        token_fetch=token_fetch if token_fetch is not None else _no_transport,
        quotes_fetch=quotes_fetch if quotes_fetch is not None else _no_transport,
        orders_page_fetch=(
            orders_page_fetch if orders_page_fetch is not None
            else _no_transport),
        order_detail_fetch=(
            order_detail_fetch if order_detail_fetch is not None
            else _no_transport),
        conditional_orders_fetch=(
            conditional_orders_fetch if conditional_orders_fetch is not None
            else _no_transport),
        quote_store=quote_store,
        ledger_store=ledger_store,
        scan_state_store=scan_state_store,
        dialogue_store=dialogue_store,
        dialogue_quote_reader=dialogue_qr,
        playbook_validator=pb_validator,
        dialogue_cutoff=dialogue_cutoff,
        playbook_root=playbook_root,
        ai_evaluator=ai_evaluator,
        telegram_sender=telegram_sender,
        updates_provider=updates_provider,
        callback_ack=callback_ack,
        news_callback_handler=news_callback_handler,
        news_outbox_reconcile=news_outbox_reconcile,
        news_delivery_runner=news_delivery_runner,
        quant_market_close_runner=quant_market_close_runner,
        quant_input_capture=quant_input_capture,
        opportunity_capture=opportunity_capture,
        news_producer_deps=news_producer_deps,
        dialogue_activation_enabled=dialogue_activation_enabled,
        quotes_provider_enabled=quotes_provider_enabled,
        execution_provider_enabled=execution_provider_enabled,
        conditional_provider_enabled=conditional_provider_enabled,
        lane_registry=OPERATION_LANE_REGISTRY_V1,
        tick_cleanup=(tick_cleanup if tick_cleanup is not None else lambda: None),
        cadence_boundary_gate=lambda slot: wait_for_cadence_complete(root, slot),
    )
