"""Composition roots for the fixture tracer and its bounded live slice."""

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.facts.eia import SOURCE, EiaRequest, HttpResponse, get_wti, query_wti_live
from tokmo.facts.eia_live import EiaLiveTransport
from tokmo.facts.krx import query_daily_trade
from tokmo.facts.krx_live import KrxLiveTransport
from tokmo.facts.official_membership_live import (
    EndpointKindV1,
    OfficialMembershipLiveTransport,
    OfficialMembershipRequestV1,
)
from tokmo.facts.opendart import query_corp_code, query_disclosure_list
from tokmo.facts.opendart_live import OpenDartLiveTransport
from tokmo.facts.quant_membership import (
    load_production_sic_snapshot,
    parse_nasdaqlisted,
    parse_otherlisted,
    parse_sec_issuer,
    parse_sec_registry,
)
from tokmo.facts.quant_recurring_membership import (
    OfficialMembershipRequestV1 as RecurringMembershipRequestV1,
)
from tokmo.facts.sec import query_company_tickers, query_submissions
from tokmo.facts.sec_live import SecLiveTransport
from tokmo.facts.sec_targeted import (
    MAX_ADMITTED_TARGETS,
    MAX_REQUESTS_PER_TICK,
    MIN_FILING_DATE,
    MIN_START_INTERVAL_SECONDS,
    STOP_CODES,
    TickerRegistryResult,
    merge_fallback_target_provenance,
    merge_sec_target_provenance,
    query_company_tickers_multi,
    query_submissions_all,
)
from tokmo.facts.telegram_dialogue_live import (
    answer_dialogue_callback,
    fetch_dialogue_updates,
    send_dialogue_message,
)
from tokmo.facts.toss import (
    TOSS_TRANSPORT_FAILURE_CODES,
    TossAccessToken,
    TossAccountList,
    TossCampaignOutcome,
    _TossTransportSuccess,
    build_accounts_request,
    build_auth_request,
    build_holdings_request,
    derive_account_identities,
    parse_accounts_response,
    parse_holdings_response,
    parse_token_response,
    query_toss_holdings,
)
from tokmo.facts.toss_auth_live import TossAuthLiveTransport
from tokmo.facts.toss_live import TossDataLiveTransport
from tokmo.facts.toss_prices import query_current_prices
from tokmo.facts.toss_prices_live import TossPricesLiveTransport
from tokmo.facts.toss_read_parsers import parse_envelope
from tokmo.facts.toss_reads import (
    TossReadOperation,
    TossReadOutcome,
    build_read_request,
    query_read,
)
from tokmo.facts.toss_reads_live import TossReadsLiveTransport
from tokmo.observability import TracerLogger
from tokmo.observability.shadow_lifecycle import (
    ENV_TELEGRAM_CHAT_ID,
    ENV_TELEGRAM_TOKEN,
)
from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
from tokmo.programs.cadence_wire import require_started_claim
from tokmo.programs.g2a_shadow_runner import (
    G2APreflightError,
    G2AStopError,
    _lsof_open_handle_count,
    build_g2a_manifest,
    preflight,
    run_a_one_hour,
    stable_run_id,
)
from tokmo.programs.g2b_canary_runner import run_b_canary, run_b_one_hour
from tokmo.programs.g2d_full_session_runner import (
    G2D_CAMPAIGN_DATA_CEILING,
    G2D_MAX_TICKS,
    _G2DInjectedModelCall,
    run_d_full_session,
)
from tokmo.programs.integrated_fake_cadence import IntegratedCadenceDepsV1
from tokmo.programs.production_cadence import (
    ProductionCadenceDepsV1,
    ProductionCadenceRefusal,
    run_production_cadence_once,
)
from tokmo.programs.quant_membership import (
    assemble_us_personal_membership_material,
    describe_us_personal_membership_cohort,
    proof_is_admissible,
)
from tokmo.programs.toss_holdings import (
    aggregate,
    assemble_complete_snapshot,
    diff_latest,
    diff_latest_typed,
    field_presence,
    format_capture_summary,
    format_masked_result,
    format_report,
    summarise_capture,
    summarise_typed_snapshot,
)
from tokmo.programs.tracer import ProgramResult, run_tracer
from tokmo.records.decision_journal import DecisionJournal
from tokmo.records.fact_store import FactStore
from tokmo.records.holdings_cutover import (
    CutoverContext,
    HoldingsRoute,
    resolve_route,
    with_family_lock,
)
from tokmo.records.holdings_persistence import (
    DenialToken,
    authorize_named_program,
    authorize_user_request,
    is_valid_uuid4_hex,
)
from tokmo.records.holdings_snapshots import (
    HoldingsSnapshotReadError,
    HoldingsSnapshotStore,
    snapshot_content_hash,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.private_sqlite import prepare_private_path
from tokmo.records.regular_session_history import (
    HistorySessionDatesV1,
    RegularSessionHistoryStore,
)
from tokmo.records.sec_targeted_store import (
    SecTargetedStore,
)
from tokmo.runtime.config import (
    ConfigError,
    load_config,
    load_krx_config,
    load_live_config,
    load_opendart_config,
    load_sec_config,
    load_toss_config,
)
from tokmo.runtime.wallclock import sleep_seconds, utc_now
from tokmo.values import (
    TOSS_SOURCE,
    KrxDailyTradeFact,
    OpenDartDisclosureFact,
    SaveStatus,
    SecFilingFact,
)
from tokmo.values.cadence_policy import TICK_REQUEST_LIMITS_V1
from tokmo.values.holdings import TossAccountHoldingsFact
from tokmo.values.official_membership_observation import (
    MembershipPolicyV1,
    ObservationSourceKindV1,
    ObservedSourcePayloadV1,
    OfficialMembershipAcquisitionV1,
    RequestIdentityV1,
)
from tokmo.values.ops_kernel import OpsKernelBudgetExceeded, ProducerRole
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.production_cadence import (
    CADENCE_MODE_LIVE_SHADOW,
    CADENCE_MODE_PRODUCTION,
    CadenceAccountsV1,
    null_lifecycle_sink,
)
from tokmo.values.request_parity import REQUEST_OPERATION_SPECS, canonical_params_digest
from tokmo.values.session_gate import Market, regular_session_for
from tokmo.values.toss_market_calendar import (
    KrMarketCalendarResponse,
    UsMarketCalendarResponse,
)
from tokmo.watchlists import WatchlistError, load_watchlists

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

# G1 OFFLINE: pension_registry_path is accepted on ProductionCadenceDepsV1
# but not yet wired into any composition root. This alias documents the
# concept so future wiring has a single named reference point.
PensionRegistryPath = Path | None


def execute_official_membership_sic_capture(
    *,
    campaign_store,
    capture_store,
    transport,
    sec_user_agent: str,
):
    """Fixed SIC capture composition; callers cannot override source identity."""
    reservation = campaign_store.reserve("SEC_SIC")
    outcome = transport.fetch(
        OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC),
        sec_user_agent=sec_user_agent,
    )
    if not outcome.ok:
        campaign_store.finish(reservation.ordinal, "FAILED")
        raise ValueError(f"sic_capture_failed:{outcome.code}")
    try:
        receipt = capture_store.append_success(
            body=outcome.body,
            captured_at=outcome.observed_at,
        )
    except BaseException:
        campaign_store.finish(reservation.ordinal, "FAILED")
        raise
    campaign_store.finish(reservation.ordinal, "SUCCEEDED")
    return capture_store.read(receipt.capture_id)


def execute_official_membership_canary(
    *,
    campaign_store,
    observation_store,
    transport,
    holdings_reader,
    sic_snapshot,
    sec_user_agent: str,
    clock,
    acquisition_id: str,
):
    """Fixed fake-testable exact-cohort caller; no runtime root is implicit."""
    first_holdings_bytes, holdings = holdings_reader()
    binding = describe_us_personal_membership_cohort(holdings, clock())
    if len(binding.expected_symbols) != 4 or campaign_store.usage() > 25:
        raise ValueError("canary_preflight")
    if observation_store.read_acquisition(acquisition_id) is not None:
        raise ValueError("canary_conflict")

    payloads = []

    def append_failed(reason):
        failed = OfficialMembershipAcquisitionV1(
            acquisition_id=acquisition_id,
            membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
            security_policy_version="official-security-name-v1",
            source_contract_version="official-membership-source-v1",
            sic_snapshot=sic_snapshot,
            listings=(),
            registry_rows=(),
            issuer_rows=(),
        )
        observation_store.append_failure(
            acquisition=failed,
            payloads=tuple(payloads),
            reason=reason,
            detail="sanitized",
        )

    def parse_or_fail(parser, *args, **kwargs):
        try:
            return parser(*args, **kwargs)
        except (ValueError, TypeError):
            append_failed("parse_failure")
            raise ValueError("canary_parse") from None

    def reserve_source(kind, identity, source_kind, subject_key):
        reservation = campaign_store.reserve(kind)
        outcome = transport.fetch(identity, sec_user_agent=sec_user_agent)
        if not outcome.ok:
            campaign_store.finish(reservation.ordinal, "FAILED")
            append_failed("source_failure")
            raise ValueError("source_failure")
        campaign_store.finish(reservation.ordinal, "SUCCEEDED")
        payloads.append(
            ObservedSourcePayloadV1(
                acquisition_id=acquisition_id,
                source_kind=source_kind,
                request_identity=RequestIdentityV1(
                    method="GET",
                    host=identity.url.split("/", 3)[2],
                    path_template="/" + identity.url.split("/", 3)[3],
                    source_schema_version="official-membership-source-v1",
                ),
                subject_key=subject_key,
                started_count=reservation.ordinal,
                observed_at=outcome.observed_at,
                payload_bytes=outcome.body,
            )
        )
        return outcome

    symbols = binding.expected_symbols
    listed = reserve_source(
        "NASDAQ_LISTED",
        OfficialMembershipRequestV1(EndpointKindV1.NASDAQ_LISTED),
        ObservationSourceKindV1.NASDAQ_LISTED,
        "nasdaqlisted",
    )
    other = reserve_source(
        "OTHER_LISTED",
        OfficialMembershipRequestV1(EndpointKindV1.OTHER_LISTED),
        ObservationSourceKindV1.OTHER_LISTED,
        "otherlisted",
    )
    registry_payload = reserve_source(
        "SEC_REGISTRY",
        OfficialMembershipRequestV1(EndpointKindV1.SEC_REGISTRY),
        ObservationSourceKindV1.SEC_REGISTRY,
        "registry",
    )
    listings = (
        *parse_or_fail(
            parse_nasdaqlisted,
            listed.body,
            observed_at=listed.observed_at,
            expected_symbols=symbols,
        ),
        *parse_or_fail(
            parse_otherlisted,
            other.body,
            observed_at=other.observed_at,
            expected_symbols=symbols,
        ),
    )
    registry_rows = parse_or_fail(
        parse_sec_registry,
        registry_payload.body,
        observed_at=registry_payload.observed_at,
        expected_symbols=symbols,
    )
    registry_by_symbol = {item.symbol: item for item in registry_rows}
    issuer_rows = []
    for symbol in sorted(symbols):
        registry = registry_by_symbol[symbol]
        issuer_payload = reserve_source(
            "SEC_SUBMISSIONS",
            OfficialMembershipRequestV1(EndpointKindV1.SEC_SUBMISSIONS, registry.cik),
            ObservationSourceKindV1.SEC_ISSUER,
            symbol,
        )
        issuer_rows.append(
            parse_or_fail(
                parse_sec_issuer,
                issuer_payload.body,
                observed_at=issuer_payload.observed_at,
                expected_symbol=symbol,
                expected_cik=registry.cik,
                expected_exchange=registry.exchange_token,
            )
        )
    acquisition = OfficialMembershipAcquisitionV1(
        acquisition_id=acquisition_id,
        membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
        security_policy_version="official-security-name-v1",
        source_contract_version="official-membership-source-v1",
        sic_snapshot=sic_snapshot,
        listings=tuple(listings),
        registry_rows=registry_rows,
        issuer_rows=tuple(issuer_rows),
    )
    evaluation_as_of = clock()
    assembled = assemble_us_personal_membership_material(
        holdings=holdings, evaluation_as_of=evaluation_as_of, acquisition=acquisition
    )
    if len(assembled.materials) != 4 or len(assembled.proofs) != 4:
        append_failed("assembly_failure")
        raise ValueError("canary_assembly")
    second_holdings_bytes, _ = holdings_reader()
    if first_holdings_bytes != second_holdings_bytes:
        raise ValueError("holdings_mutated")
    receipt = observation_store.append_success(
        acquisition=acquisition, payloads=tuple(payloads), materials=assembled.materials
    )
    read_acquisition = observation_store.read_acquisition(acquisition_id)
    read_receipt = observation_store.read_receipt(acquisition_id)
    read_materials = observation_store.read_materials(acquisition_id)
    if (
        read_acquisition != acquisition
        or read_receipt is None
        or len(read_materials) != 4
        or not all(
            proof_is_admissible(
                proof, binding=binding, evaluation_as_of=evaluation_as_of
            )
            for proof in assembled.proofs
        )
    ):
        raise ValueError("canary_readback")
    return receipt, read_materials, assembled.proofs


def _seoul_calendar_date(now: datetime) -> date:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_now_required")
    return now.astimezone(ZoneInfo("Asia/Seoul")).date()


def _default_snapshot_id_factory() -> str:
    """Production default: one secure UUID4 hex per capture."""
    return uuid.uuid4().hex


def execute_holdings_route(
    *,
    marker_path: Path,
    sqlite_path: Path,
    json_path: Path,
    lock_path: Path,
    context: CutoverContext,
    legacy_fn: Callable[[], object],
    sqlite_fn: Callable[[], object],
) -> object:
    """Run exactly one holdings adapter, or fail closed, under a shared lock.

    The first resolution and immediate pre-call recheck happen under the same
    family lock.  LEGACY independently repeats physical path safety before
    the injected callback may construct/call ``FactStore``; semantic safety
    has already been established by ``resolve_route``'s frozen validator.
    """

    def fingerprint(path: Path) -> tuple:
        try:
            value = os.lstat(path)
            if stat.S_ISREG(value.st_mode):
                with path.open("rb") as stream:
                    digest = hashlib.sha256(stream.read()).digest()
            else:
                digest = b""
            return (
                "present",
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_uid,
                value.st_size,
                value.st_mtime_ns,
                digest,
            )
        except FileNotFoundError:
            return ("absent",)
        except OSError:
            return ("unsafe",)

    paths = (marker_path, sqlite_path, json_path)
    before_lock = tuple(fingerprint(path) for path in paths)

    def operation() -> object:
        in_lock = tuple(fingerprint(path) for path in paths)
        if before_lock != in_lock:
            raise LiveRunError("cutover_incomplete")
        observed = resolve_route(
            marker_path=marker_path,
            sqlite_path=sqlite_path,
            json_path=json_path,
            context=context,
        )
        after_resolve = tuple(fingerprint(path) for path in paths)
        rechecked = resolve_route(
            marker_path=marker_path,
            sqlite_path=sqlite_path,
            json_path=json_path,
            context=context,
        )
        after_recheck = tuple(fingerprint(path) for path in paths)
        if (
            observed is not rechecked
            or in_lock != after_resolve
            or after_resolve != after_recheck
        ):
            raise LiveRunError("cutover_incomplete")
        if observed is HoldingsRoute.LEGACY:
            safety = prepare_private_path(json_path)
            if safety.unsafe:
                raise LiveRunError("cutover_incomplete")
            return legacy_fn()
        if observed is HoldingsRoute.SQLITE_V1:
            return sqlite_fn()
        raise LiveRunError("cutover_incomplete")

    try:
        return with_family_lock(lock_path, exclusive=False, fn=operation)
    except Exception as error:
        if getattr(error, "reason", "") == "family_locked":
            raise LiveRunError("family_locked") from None
        raise


def _holdings_route_paths(home: Path) -> tuple[Path, Path, Path, Path]:
    leaf = (home / ".local" / "share" / "tokmo-v3").resolve() / TOSS_LEAF_NAME
    return (
        leaf / "holdings_cutover_v1.json",
        leaf / "holdings_snapshots.sqlite3",
        leaf / TOSS_STORE_NAME,
        leaf / ".holdings_family.lock",
    )


MAX_ATTEMPTS = 11
MIN_START_INTERVAL = timedelta(seconds=5)
MAX_RETRY_AFTER = 3600
STATE_NAME = "eia-attempts.json"

# SEC Tracer 1a bounded probe campaign: hard request-start ceiling across
# both endpoints combined, one named finite hypothesis per endpoint.
SEC_MAX_STARTS = 10
SEC_STATE_NAME = "sec-attempts.json"
SEC_ENV_NAME = "sec.env"
SEC_TARGET_TICKER = "AAPL"
SEC_PROBES = {
    "tickers": ("sec_company_tickers", "confirm_aapl_ticker_map_schema"),
    "submissions": ("sec_submissions", "confirm_recent_column_alignment"),
}
# Fair-access outcomes stop the whole campaign until manually investigated.
_SEC_STOP_OUTCOMES = frozenset({"auth_rejection", "http_429"})

# OpenDART Tracer 1a bounded campaign: hard request-start ceiling across both
# endpoints combined, durable probes[] history appended before I/O, global
# 2-second spacing between starts, and simple tranche gates at 10 and 20.
DART_MAX_STARTS = 30
DART_TRANCHE_SIZE = 10
DART_MIN_START_INTERVAL_SECONDS = 2
DART_MAX_RETRY_AFTER = 3600
DART_STATE_NAME = "opendart-attempts.json"
DART_ENV_NAME = "opendart.env"
DART_TARGET_STOCK_CODE = "000660"
DART_LIST_WINDOW_DAYS = 90
DART_PROBES = {
    "corpcode": ("opendart_corp_code", "confirm_000660_corp_mapping_schema"),
    "list": ("opendart_list", "confirm_latest_disclosure_schema"),
}
# HTTP 403/429, key rejection, and provider status 020/021 stop everything.
_DART_STOP_OUTCOMES = frozenset(
    {
        "auth_rejection",
        "http_429",
        "provider_key_rejected",
        "provider_rate_limited",
    }
)
KRX_MAX_STARTS = 10
KRX_MIN_START_INTERVAL_SECONDS = 2
KRX_STATE_NAME = "krx-attempts.json"
KRX_ENV_NAME = "krx.env"
KRX_ENV_TEMPLATE = "KRX_API_KEY=\n"
_KRX_STOP_OUTCOMES = frozenset({"auth_rejection", "http_429"})
_DART_PROBE_KEYS = frozenset(
    {"sequence", "endpoint_id", "purpose", "started_at", "outcome"}
)
_DART_CHECKPOINT_KEYS = frozenset(
    {
        "tranche",
        "confirmed",
        "changed",
        "unresolved",
        "why_more_calls",
        "tests_green",
        "no_scope_expansion",
        "new_information",
        "recorded_at",
    }
)

# Toss current-holdings campaign. Request-starts are counted jointly across
# the token call, the accounts call, and every holdings call: at most one
# token + one accounts + five accounts' holdings, with one start of margin.
# The active gate derives from the one central policy declaration (SDS §7.1);
# changing the holdings limit changes this campaign ceiling with it.
TOSS_MAX_STARTS = TICK_REQUEST_LIMITS_V1.holdings_starts
TOSS_MAX_ACCOUNTS = 5
TOSS_MIN_START_INTERVAL_SECONDS = 2
TOSS_MAX_RETRY_AFTER = 3600
TOSS_LEAF_NAME = "toss"
# User-approved exception for this tracer only: the Toss credential file is
# project-local (`<project_root>/.env.toss`, gitignored, regular file, 0600,
# never a symlink). No Tokmo config directory and no home-level credential
# file exist for Toss, and the process environment is never consulted.
# Runtime state and captured artifacts stay outside the repository, under
# `home`, exactly as for the other tracers. `project_root` is an explicit
# argument everywhere; PROJECT_ROOT is the CLI's only default and is derived
# deterministically from this module's location (two levels above src/tokmo).
TOSS_ENV_NAME = ".env.toss"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOSS_STATE_NAME = "campaign_state.json"
TOSS_STORE_NAME = "holdings_store.json"
TOSS_DIFF_NAME = "latest_diff.json"
TOSS_POLICY_VERSION = "toss1-v1"
TOSS_ENV_TEMPLATE = "TOSS_CLIENT_ID=\nTOSS_CLIENT_SECRET=\n"
# Credential rejection and fair-access outcomes stop the whole campaign and
# keep the next one from starting until a human has looked at the ledger.
_TOSS_STOP_OUTCOMES = frozenset({"auth_rejection", "forbidden", "http_429"})
_TOSS_START_KEYS = frozenset(
    {"start_index", "stage", "account_ordinal", "planned_at", "outcome"}
)
_TOSS_STAGES = frozenset({"auth", "accounts", "holdings"})
# The logger rejects any value containing credential vocabulary, and two
# closed codes carry a scheme name; they are renamed rather than dropped.
_TOSS_LOG_REASONS = {"http_429": "rate_limited", "http_5xx": "server_error"}


class LiveRunError(Exception):
    """Masked live failure; never contains a URL, key, body, or provider text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LiveRunResult:
    code: str
    started: int
    decision_action: str
    decision_reason: str


@dataclass(frozen=True)
class DartRunResult:
    code: str
    endpoint: str
    started: int
    corp_code: str | None = None
    corp_name: str | None = None
    decision_action: str | None = None
    decision_reason: str | None = None
    disclosure: OpenDartDisclosureFact | None = None


@dataclass(frozen=True)
class SecRunResult:
    code: str
    endpoint: str
    started: int
    cik: str | None = None
    company_name: str | None = None
    decision_action: str | None = None
    decision_reason: str | None = None
    filing: SecFilingFact | None = None


@dataclass(frozen=True)
class KrxRunResult:
    code: str
    started: int
    decision_action: str
    decision_reason: str
    fact: KrxDailyTradeFact


@dataclass(frozen=True)
class TossRunResult:
    """Aggregate-only campaign result: counts and provider field names.

    No symbol, quantity, average price, or account identifier is carried here,
    so this object is safe to render anywhere a human or a relay can see.
    """

    code: str
    started: int
    account_count: int
    holdings_items: int
    currencies_observed: int
    markets_observed: int
    saved: int
    duplicate: int
    conflict: int
    avg_price_fields: tuple[str, ...]
    market_fields: tuple[str, ...]


def run_fixture(
    *,
    env_path: Path,
    fixture_path: Path,
    output_dir: Path,
    clock: Clock,
    id_factory: IdFactory,
    run_id: str,
) -> ProgramResult:
    config = load_config(env_path)
    body = fixture_path.read_bytes()

    def fixture_transport(_request: EiaRequest) -> HttpResponse:
        return HttpResponse(200, body, {})

    logger = TracerLogger(output_dir, clock=clock)
    logger.debug(
        run_id=run_id,
        stage="fixture",
        action="READ",
        reason="sanitized_fixture",
        source=SOURCE,
        decision_id=None,
    )
    fact = get_wti(api_key=config.eia_api_key, transport=fixture_transport, clock=clock)
    return run_tracer(
        fact,
        store=FactStore(output_dir / "facts.json", clock=clock, id_factory=id_factory),
        journal=DecisionJournal(output_dir / "decisions.jsonl"),
        clock=clock,
        id_factory=id_factory,
        run_id=run_id,
        logger=logger,
    )


def setup_live(*, home: Path) -> None:
    """Create only Tokmo-owned private directories and initial state."""
    config_dir, data_dir = _live_dirs(home)
    _private_dir(config_dir)
    _private_dir(data_dir)
    state_path = config_dir / STATE_NAME
    if not state_path.exists():
        _write_json(state_path, _empty_state())
    else:
        _read_state(state_path)


def run_live(
    *, home: Path, transport, clock: Clock, id_factory: IdFactory, run_id: str
) -> LiveRunResult:
    """Start at most one GET, after durably consuming one bounded attempt."""
    config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(config_dir)
    _require_private_owned_dir(data_dir)
    try:
        config_path = config_dir / "tokmo.env"
        if config_path.is_symlink() or (config_path.stat().st_mode & 0o777) != 0o600:
            raise ValueError("config_mode")
        config = load_live_config(config_path)
        now = _aware_utc(clock())
        state_path = config_dir / STATE_NAME
        state = _read_state(state_path)
        if state["success"]:
            raise LiveRunError("already_succeeded")
        if state["total_started"] >= MAX_ATTEMPTS:
            raise LiveRunError("ceiling_exhausted")
        if state["last_started_at"] is not None:
            last = _parse_utc(state["last_started_at"])
            wait = max(5, state.get("retry_after_seconds", 5))
            if now < last + timedelta(seconds=wait):
                raise LiveRunError("not_due")

        state["total_started"] += 1
        state["last_started_at"] = now.isoformat()
        state["last_outcome"] = "started"
        state["retry_after_seconds"] = 5
        _write_json(state_path, state)  # no refund, even if the opener fails

        query = query_wti_live(
            api_key=config.eia_api_key, transport=transport, clock=lambda: now
        )
        outcome = query.attempt
        code = outcome.code if outcome is not None else "protocol_failure"
        state["success"] = code == "success"
        state["last_outcome"] = code
        if code == "http_429" and outcome.retry_after is not None:
            state["retry_after_seconds"] = min(
                MAX_RETRY_AFTER, max(5, outcome.retry_after)
            )
        _write_json(state_path, state)

        program = run_tracer(
            query.fact,
            store=FactStore(
                data_dir / "facts.json", clock=clock, id_factory=id_factory
            ),
            journal=DecisionJournal(data_dir / "decisions.jsonl"),
            clock=clock,
            id_factory=id_factory,
            run_id=run_id,
            logger=TracerLogger(data_dir, clock=clock),
        )
        if code != "success":
            raise LiveRunError(code)
        return LiveRunResult(
            code,
            state["total_started"],
            program.decision.action,
            program.decision.reason,
        )
    except LiveRunError:
        raise
    except (
        ConfigError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        raise LiveRunError("private_state_invalid") from None


def setup_sec(*, home: Path) -> None:
    """Create only Tokmo-owned private directories and initial SEC state."""
    config_dir, data_dir = _live_dirs(home)
    _private_dir(config_dir)
    _private_dir(data_dir)
    state_path = config_dir / SEC_STATE_NAME
    if not state_path.exists():
        _write_json(state_path, _empty_sec_state())
    else:
        _read_sec_state(state_path)


def run_sec(
    *,
    home: Path,
    endpoint: str,
    transport,
    clock: Clock,
    id_factory: IdFactory,
    run_id: str,
) -> SecRunResult:
    """Start at most one SEC GET, after durably consuming one bounded
    campaign attempt (write-before-I/O, no refund)."""
    if endpoint not in SEC_PROBES:
        raise LiveRunError("unknown_endpoint")
    endpoint_id, purpose = SEC_PROBES[endpoint]
    config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(config_dir)
    _require_private_owned_dir(data_dir)
    try:
        config_path = config_dir / SEC_ENV_NAME
        if config_path.is_symlink() or (config_path.stat().st_mode & 0o777) != 0o600:
            raise ValueError("config_mode")
        config = load_sec_config(config_path)
        now = _aware_utc(clock())
        state_path = config_dir / SEC_STATE_NAME
        state = _read_sec_state(state_path)
        if state["last_outcome"] in _SEC_STOP_OUTCOMES:
            raise LiveRunError("fair_access_stop")
        if endpoint == "tickers" and state["aapl_cik"] is not None:
            raise LiveRunError("already_confirmed")
        if endpoint == "submissions":
            if state["submissions_confirmed"]:
                raise LiveRunError("already_confirmed")
            if state["aapl_cik"] is None:
                raise LiveRunError("mapping_missing")
        if state["total_started"] >= SEC_MAX_STARTS:
            raise LiveRunError("ceiling_exhausted")
        if state["last_started_at"] is not None:
            last = _parse_utc(state["last_started_at"])
            if now < last + MIN_START_INTERVAL:
                raise LiveRunError("not_due")

        state.update(
            total_started=state["total_started"] + 1,
            last_started_at=now.isoformat(),
            last_endpoint=endpoint_id,
            last_purpose=purpose,
            last_outcome="started",
        )
        _write_json(state_path, state)  # no refund, even if the opener fails

        if endpoint == "tickers":
            query = query_company_tickers(
                ticker=SEC_TARGET_TICKER,
                user_agent=config.user_agent,
                transport=transport,
                clock=lambda: now,
            )
        else:
            query = query_submissions(
                ticker=SEC_TARGET_TICKER,
                cik=state["aapl_cik"],
                company_name=state["aapl_company_name"],
                user_agent=config.user_agent,
                transport=transport,
                clock=lambda: now,
            )
        del config
        outcome = query.attempt
        code = outcome.code if outcome is not None else "protocol_failure"
        state["last_outcome"] = code

        if endpoint == "tickers":
            if code == "success":
                state["aapl_cik"] = query.result.cik
                state["aapl_company_name"] = query.result.company_name
            _write_json(state_path, state)
            if code != "success":
                raise LiveRunError(code)
            return SecRunResult(
                code,
                endpoint,
                state["total_started"],
                cik=state["aapl_cik"],
                company_name=state["aapl_company_name"],
            )

        state["submissions_confirmed"] = code == "success"
        _write_json(state_path, state)
        program = run_tracer(
            query.result,
            store=FactStore(
                data_dir / "sec_facts.json", clock=clock, id_factory=id_factory
            ),
            journal=DecisionJournal(data_dir / "sec_decisions.jsonl"),
            clock=clock,
            id_factory=id_factory,
            run_id=run_id,
            logger=TracerLogger(data_dir, clock=clock),
        )
        if code != "success":
            raise LiveRunError(code)
        return SecRunResult(
            code,
            endpoint,
            state["total_started"],
            cik=state["aapl_cik"],
            company_name=state["aapl_company_name"],
            decision_action=program.decision.action,
            decision_reason=program.decision.reason,
            filing=query.result,
        )
    except LiveRunError:
        raise
    except (
        ConfigError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        raise LiveRunError("private_state_invalid") from None


def setup_dart(*, home: Path) -> None:
    """Create only Tokmo-owned private directories and initial OpenDART state."""
    config_dir, data_dir = _live_dirs(home)
    _private_dir(config_dir)
    _private_dir(data_dir)
    state_path = config_dir / DART_STATE_NAME
    if not state_path.exists():
        _write_json(state_path, _empty_dart_state())
    else:
        _read_dart_state(state_path)


def setup_krx(*, home: Path) -> None:
    config_dir, data_dir = _live_dirs(home)
    _private_dir(config_dir)
    _private_dir(data_dir)
    env_path = config_dir / KRX_ENV_NAME
    if not env_path.exists():
        _write_private_text(env_path, KRX_ENV_TEMPLATE)
    state_path = config_dir / KRX_STATE_NAME
    if not state_path.exists():
        _write_json(state_path, _empty_krx_state())
    else:
        _read_krx_state(state_path)


def run_krx(
    *,
    home: Path,
    base_date: str,
    transport,
    clock: Clock,
    id_factory: IdFactory,
    run_id: str,
) -> KrxRunResult:
    config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(config_dir)
    _require_private_owned_dir(data_dir)
    try:
        now = _aware_utc(clock())
        requested = datetime.strptime(base_date, "%Y%m%d").date()
        korean_today = (now + timedelta(hours=9)).date()
        if (
            requested.strftime("%Y%m%d") != base_date
            or requested.weekday() >= 5
            or requested >= korean_today
        ):
            raise LiveRunError("invalid_date")
        config_path = config_dir / KRX_ENV_NAME
        if config_path.is_symlink() or (config_path.stat().st_mode & 0o777) != 0o600:
            raise ValueError("config_mode")
        config = load_krx_config(config_path)
        state_path = config_dir / KRX_STATE_NAME
        state = _read_krx_state(state_path)
        if state["last_outcome"] in _KRX_STOP_OUTCOMES:
            raise LiveRunError("fair_access_stop")
        if state["total_started"] >= KRX_MAX_STARTS:
            raise LiveRunError("ceiling_exhausted")
        if state["last_started_at"] is not None and now < (
            _parse_utc(state["last_started_at"])
            + timedelta(seconds=KRX_MIN_START_INTERVAL_SECONDS)
        ):
            raise LiveRunError("not_due")
        state.update(
            total_started=state["total_started"] + 1,
            last_started_at=now.isoformat(),
            requested_base_date=base_date,
            last_outcome="started",
        )
        _write_json(state_path, state)
        query = query_daily_trade(
            base_date=base_date,
            api_key=config.api_key,
            transport=transport,
            clock=lambda: now,
        )
        del config
        state["last_outcome"] = query.attempt.code
        _write_json(state_path, state)
        program = run_tracer(
            query.result,
            store=FactStore(
                data_dir / "krx_facts.json", clock=clock, id_factory=id_factory
            ),
            journal=DecisionJournal(data_dir / "krx_decisions.jsonl"),
            clock=clock,
            id_factory=id_factory,
            run_id=run_id,
            logger=TracerLogger(data_dir, clock=clock),
        )
        if query.attempt.code != "success":
            raise LiveRunError(query.attempt.code)
        return KrxRunResult(
            "success",
            state["total_started"],
            program.decision.action,
            program.decision.reason,
            query.result,
        )
    except LiveRunError:
        raise
    except (
        ConfigError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        raise LiveRunError("private_state_invalid") from None


def checkpoint_dart(
    *,
    home: Path,
    tranche: int,
    confirmed: str,
    changed: str,
    unresolved: str,
    why_more_calls: str,
    tests_green: bool,
    no_scope_expansion: bool,
    new_information: str | None,
    clock: Clock,
) -> None:
    """Durably record one tranche-gate checkpoint object (no probe consumed)."""
    config_dir, _data_dir = _live_dirs(home)
    _require_private_owned_dir(config_dir)
    try:
        now = _aware_utc(clock())
        state_path = config_dir / DART_STATE_NAME
        state = _read_dart_state(state_path)
        state["checkpoints"].append(
            {
                "tranche": tranche,
                "confirmed": confirmed,
                "changed": changed,
                "unresolved": unresolved,
                "why_more_calls": why_more_calls,
                "tests_green": tests_green,
                "no_scope_expansion": no_scope_expansion,
                "new_information": new_information,
                "recorded_at": now.isoformat(),
            }
        )
        _validate_dart_checkpoints(state["checkpoints"])
        _write_json(state_path, state)
    except LiveRunError:
        raise
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise LiveRunError("private_state_invalid") from None


def run_dart(
    *,
    home: Path,
    endpoint: str,
    transport,
    clock: Clock,
    id_factory: IdFactory,
    run_id: str,
) -> DartRunResult:
    """Start at most one OpenDART GET, after durably appending one bounded
    probes[] record (write-before-I/O, no refund)."""
    if endpoint not in DART_PROBES:
        raise LiveRunError("unknown_endpoint")
    endpoint_id, purpose = DART_PROBES[endpoint]
    config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(config_dir)
    _require_private_owned_dir(data_dir)
    try:
        config_path = config_dir / DART_ENV_NAME
        if config_path.is_symlink() or (config_path.stat().st_mode & 0o777) != 0o600:
            raise ValueError("config_mode")
        config = load_opendart_config(config_path)
        now = _aware_utc(clock())
        state_path = config_dir / DART_STATE_NAME
        state = _read_dart_state(state_path)
        probes = state["probes"]
        if probes and probes[-1]["outcome"] in _DART_STOP_OUTCOMES:
            raise LiveRunError("fair_access_stop")
        if endpoint == "corpcode" and state["corp_code"] is not None:
            raise LiveRunError("already_confirmed")
        if endpoint == "list":
            if state["list_confirmed"]:
                raise LiveRunError("already_confirmed")
            if state["corp_code"] is None:
                raise LiveRunError("mapping_missing")
        sequence = len(probes) + 1
        if sequence > DART_MAX_STARTS:
            raise LiveRunError("ceiling_exhausted")
        for tranche, boundary in ((2, DART_TRANCHE_SIZE), (3, 2 * DART_TRANCHE_SIZE)):
            if sequence > boundary and not _dart_gate_open(state, tranche):
                raise LiveRunError("tranche_gate")
        if probes:
            last = _parse_utc(probes[-1]["started_at"])
            wait = max(DART_MIN_START_INTERVAL_SECONDS, state["retry_after_seconds"])
            if now < last + timedelta(seconds=wait):
                raise LiveRunError("not_due")

        probes.append(
            {
                "sequence": sequence,
                "endpoint_id": endpoint_id,
                "purpose": purpose,
                "started_at": now.isoformat(),
                "outcome": "started",
            }
        )
        _write_json(state_path, state)  # no refund, even if the opener fails

        if endpoint == "corpcode":
            query = query_corp_code(
                stock_code=DART_TARGET_STOCK_CODE,
                api_key=config.api_key,
                transport=transport,
                clock=lambda: now,
            )
        else:
            end_date = now.date()
            begin_date = end_date - timedelta(days=DART_LIST_WINDOW_DAYS)
            query = query_disclosure_list(
                corp_code=state["corp_code"],
                stock_code=DART_TARGET_STOCK_CODE,
                begin_date=f"{begin_date:%Y%m%d}",
                end_date=f"{end_date:%Y%m%d}",
                api_key=config.api_key,
                transport=transport,
                clock=lambda: now,
            )
        del config
        outcome = query.attempt
        code = outcome.code if outcome is not None else "protocol_failure"
        probes[-1]["outcome"] = code
        if code == "http_429" and outcome.retry_after is not None:
            state["retry_after_seconds"] = min(
                DART_MAX_RETRY_AFTER,
                max(DART_MIN_START_INTERVAL_SECONDS, outcome.retry_after),
            )

        if endpoint == "corpcode":
            if code == "success":
                state["corp_code"] = query.result.corp_code
                state["corp_name"] = query.result.corp_name
                state["modify_date"] = query.result.modify_date.isoformat()
            _write_json(state_path, state)
            if code != "success":
                raise LiveRunError(code)
            return DartRunResult(
                code,
                endpoint,
                sequence,
                corp_code=state["corp_code"],
                corp_name=state["corp_name"],
            )

        state["list_confirmed"] = code == "success"
        _write_json(state_path, state)
        program = run_tracer(
            query.result,
            store=FactStore(
                data_dir / "opendart_facts.json", clock=clock, id_factory=id_factory
            ),
            journal=DecisionJournal(data_dir / "opendart_decisions.jsonl"),
            clock=clock,
            id_factory=id_factory,
            run_id=run_id,
            logger=TracerLogger(data_dir, clock=clock),
        )
        if code != "success":
            raise LiveRunError(code)
        return DartRunResult(
            code,
            endpoint,
            sequence,
            corp_code=state["corp_code"],
            corp_name=state["corp_name"],
            decision_action=program.decision.action,
            decision_reason=program.decision.reason,
            disclosure=query.result,
        )
    except LiveRunError:
        raise
    except (
        ConfigError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        raise LiveRunError("private_state_invalid") from None


def setup_toss(*, home: Path, project_root: Path) -> None:
    """Create the Tokmo-owned private runtime leaf under `home`, the empty
    project-local credential template, and the initial campaign ledger.

    No Toss config directory is created under `home`. No value is ever
    accepted or printed, and an existing credential file — at any path, of
    any shape — is left untouched.
    """
    _config_dir, data_dir = _live_dirs(home)
    _private_dir(data_dir)
    leaf = data_dir / TOSS_LEAF_NAME
    _private_dir(leaf)
    env_path = project_root / TOSS_ENV_NAME
    if not os.path.lexists(env_path):  # a symlink counts as present
        _write_private_text(env_path, TOSS_ENV_TEMPLATE)
    state_path = leaf / TOSS_STATE_NAME
    if not state_path.exists():
        _write_json(state_path, _empty_toss_state())
    else:
        _read_toss_state(state_path)


def run_toss(
    *,
    home: Path,
    project_root: Path,
    auth_transport,
    data_transport,
    clock: Clock,
    sleeper,
    id_factory: IdFactory,
    run_id: str,
    snapshot_store=None,
    content_hash_fn=None,
    purpose: str | None = None,
    snapshot_id_factory: IdFactory | None = None,
) -> TossRunResult:
    """One bounded current-holdings capture.

    Credentials are read from `project_root/.env.toss` and from nowhere else:
    no home-level credential file and no process environment. Every
    request-start is durably appended to the ledger before it is sent and is
    never refunded; starts are paced globally; nothing is retried. The whole
    capture is persisted through one atomic batch only after every account
    has parsed, so a stopped campaign leaves no snapshot at all.

    When ``snapshot_store`` is injected (typed/SQLite path), the snapshot ID
    is generated, the purpose is resolved, and the seam combination is
    validated **before** any transport call or campaign-state write — so a
    denial or a bad seam wastes no API budget and starts no campaign.
    ``snapshot_id_factory`` is a keyword-only seam for deterministic tests;
    in production it defaults to ``uuid.uuid4().hex`` (one call per capture).
    """
    _config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(data_dir)
    leaf = data_dir / TOSS_LEAF_NAME
    _require_private_owned_dir(leaf)
    try:
        config_path = project_root / TOSS_ENV_NAME
        _require_private_credential_file(config_path)
        config = load_toss_config(config_path)

        # --- pre-query context for the typed path (MATERIAL 2) ---
        # Resolve snapshot_id, purpose/authorization, and seam validity BEFORE
        # any transport call or campaign-state write.  A denial or bad seam
        # wastes no API budget and starts no campaign ledger entry.
        snapshot_id = None
        permit = None
        if snapshot_store is not None:
            if content_hash_fn is None or not callable(content_hash_fn):
                raise ValueError("content_hash_fn_required")
            factory = snapshot_id_factory or _default_snapshot_id_factory
            snapshot_id = factory()
            if not is_valid_uuid4_hex(snapshot_id):
                raise ValueError("invalid_snapshot_id")
            if purpose is not None:
                permit = authorize_named_program(
                    snapshot_id=snapshot_id,
                    program_name=purpose,
                    clock=lambda: _aware_utc(clock()),
                )
            else:
                permit = authorize_user_request(
                    snapshot_id=snapshot_id,
                    clock=lambda: _aware_utc(clock()),
                )
            if isinstance(permit, DenialToken):
                raise LiveRunError(permit.reason)

        state_path = leaf / TOSS_STATE_NAME
        state = _read_toss_state(state_path)
        if state["last_outcome"] in _TOSS_STOP_OUTCOMES:
            raise LiveRunError("fair_access_stop")
        if len(state["starts"]) >= TOSS_MAX_STARTS:
            raise LiveRunError("ceiling_exhausted")
        state["campaign_index"] += 1
        state["starts"] = []
        state["field_presence"] = None
        _write_json(state_path, state)

        def before_start(stage: str, account_ordinal: int | None) -> None:
            if len(state["starts"]) >= TOSS_MAX_STARTS:
                raise LiveRunError("ceiling_exhausted")
            now = _aware_utc(clock())
            if state["starts"]:
                due = _parse_utc(state["starts"][-1]["planned_at"]) + timedelta(
                    seconds=TOSS_MIN_START_INTERVAL_SECONDS
                )
                if now < due:
                    sleeper((due - now).total_seconds())
                    now = _aware_utc(clock())
            state["starts"].append(
                {
                    "start_index": len(state["starts"]) + 1,
                    "stage": stage,
                    "account_ordinal": account_ordinal,
                    "planned_at": now.isoformat(),
                    "outcome": "started",
                }
            )
            state["total_started"] += 1
            _write_json(state_path, state)  # no refund, even if the opener fails

        def checkpoint(account_count: int) -> None:
            if len(state["starts"]) + account_count > TOSS_MAX_STARTS:
                raise LiveRunError("ceiling_exhausted")

        outcome = query_toss_holdings(
            client_id=config.client_id,
            client_secret=config.client_secret,
            max_accounts=TOSS_MAX_ACCOUNTS,
            auth_transport=auth_transport,
            data_transport=data_transport,
            clock=lambda: _aware_utc(clock()),
            before_start=before_start,
            checkpoint=checkpoint,
        )
        del config

        for entry, attempt in zip(state["starts"], outcome.attempts):
            entry["outcome"] = attempt.code
            if attempt.code == "http_429" and attempt.retry_after is not None:
                state["retry_after_seconds"] = min(
                    TOSS_MAX_RETRY_AFTER,
                    max(TOSS_MIN_START_INTERVAL_SECONDS, attempt.retry_after),
                )
        state["last_outcome"] = outcome.code
        state["rate_limit_remaining"] = outcome.rate_limit_remaining
        logger = TracerLogger(leaf, clock=lambda: _aware_utc(clock()))
        if outcome.code != "success":
            _write_json(state_path, state)
            _log_toss(logger, run_id, "SKIP", outcome.code)
            raise LiveRunError(outcome.code)

        presence = field_presence(outcome.facts)
        state["field_presence"] = presence
        state["last_captured_at"] = outcome.facts[0].captured_at.isoformat()
        _write_json(state_path, state)

        if snapshot_store is not None:
            snapshot = assemble_complete_snapshot(
                outcome.facts,
                snapshot_id=snapshot_id,
                content_hash_fn=content_hash_fn,
            )
            receipt = snapshot_store.append(snapshot, permit)
            if receipt.status != "SAVED":
                _log_toss(logger, run_id, "SKIP", receipt.reason)
                raise LiveRunError(receipt.reason)
            saved_count = receipt.inserted_count
            dup_count = receipt.duplicate_count
            conf_count = receipt.conflict_count
        else:
            store = FactStore(
                leaf / TOSS_STORE_NAME,
                clock=lambda: _aware_utc(clock()),
                id_factory=id_factory,
            )
            results = store.save_batch(
                outcome.facts, policy_version=TOSS_POLICY_VERSION
            )
            tally = {
                status: sum(1 for result in results if result.status is status)
                for status in SaveStatus
            }
            if tally[SaveStatus.SAVED] != len(outcome.facts):
                blocked = _first_unsaved(results)
                _log_toss(logger, run_id, "SKIP", blocked.reason)
                raise LiveRunError(blocked.reason)
            saved_count = tally[SaveStatus.SAVED]
            dup_count = tally[SaveStatus.DUPLICATE]
            conf_count = tally[SaveStatus.CONFLICT]
            _write_private_text(
                leaf / TOSS_DIFF_NAME,
                json.dumps(diff_latest(store.records()), separators=(",", ":")),
            )

        counts = aggregate(outcome.facts)
        _log_toss(logger, run_id, "SAVE", outcome.code)
        return TossRunResult(
            code=outcome.code,
            started=len(state["starts"]),
            account_count=outcome.account_count,
            holdings_items=counts["holdings_items"],
            currencies_observed=counts["currencies_observed"],
            markets_observed=counts["markets_observed"],
            saved=saved_count,
            duplicate=dup_count,
            conflict=conf_count,
            avg_price_fields=tuple(presence["avg_price_fields"]),
            market_fields=tuple(presence["market_fields"]),
        )
    except LiveRunError:
        raise
    except (
        ConfigError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        raise LiveRunError("private_state_invalid") from None


def toss_report(*, home: Path, snapshot_store=None) -> None:
    """Print the latest capture's aggregate and the observational diff counts.

    The detailed comparison stays in the private leaf; only counts, provider
    field names, and capture instants are printed.

    When ``snapshot_store`` is injected (keyword-only), the typed path uses
    ``read_latest_pair()`` to obtain ``(previous, current)`` typed snapshots
    and never touches JSON records. Without injection the R0 default JSON
    FactStore path is used unchanged.
    """
    _config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(data_dir)
    leaf = data_dir / TOSS_LEAF_NAME
    _require_private_owned_dir(leaf)

    if snapshot_store is not None:
        try:
            previous, current = snapshot_store.read_latest_pair()
        except HoldingsSnapshotReadError as error:
            raise LiveRunError(error.reason) from None
        if current is not None:
            print(format_capture_summary(summarise_typed_snapshot(current)))
            print(format_report(diff_latest_typed(previous, current)))
        else:
            print(
                format_capture_summary(
                    {
                        "captures": 0,
                        "captured_at": None,
                        "accounts": 0,
                        "holdings_items": 0,
                        "currencies_observed": 0,
                        "markets_observed": 0,
                    }
                )
            )
            print(format_report(diff_latest_typed(None, None)))
        return

    # Read-only: no record is created here, so the id factory is never called.
    store = FactStore(
        leaf / TOSS_STORE_NAME,
        clock=lambda: _aware_utc(utc_now()),
        id_factory=lambda: "",
    )
    try:
        records = store.records()
    except (ValueError, OSError):
        raise LiveRunError("store_corrupt") from None
    print(format_capture_summary(summarise_capture(records)))
    print(format_report(diff_latest(records)))


def run_toss_routed(
    *,
    home: Path,
    project_root: Path,
    auth_transport,
    data_transport,
    clock: Clock,
    sleeper,
    id_factory: IdFactory,
    run_id: str,
    purpose: str | None = None,
    snapshot_id_factory: IdFactory | None = None,
    context: CutoverContext | None = None,
) -> TossRunResult:
    """Production R1 capture entrypoint with one physical-state-owned route."""
    marker, sqlite, legacy, lock = _holdings_route_paths(home)
    route_context = context or CutoverContext(first_use_legacy=False)
    common = dict(
        home=home,
        project_root=project_root,
        auth_transport=auth_transport,
        data_transport=data_transport,
        clock=clock,
        sleeper=sleeper,
        id_factory=id_factory,
        run_id=run_id,
        purpose=purpose,
        snapshot_id_factory=snapshot_id_factory,
    )
    return execute_holdings_route(
        marker_path=marker,
        sqlite_path=sqlite,
        json_path=legacy,
        lock_path=lock,
        context=route_context,
        legacy_fn=lambda: run_toss(**common),
        sqlite_fn=lambda: run_toss(
            **common,
            snapshot_store=HoldingsSnapshotStore(sqlite, clock=clock),
            content_hash_fn=snapshot_content_hash,
        ),
    )


def toss_report_routed(
    *,
    home: Path,
    context: CutoverContext | None = None,
) -> None:
    """Production R1 report entrypoint held under the family shared lock."""
    marker, sqlite, legacy, lock = _holdings_route_paths(home)
    route_context = context or CutoverContext(first_use_legacy=False)
    return execute_holdings_route(
        marker_path=marker,
        sqlite_path=sqlite,
        json_path=legacy,
        lock_path=lock,
        context=route_context,
        legacy_fn=lambda: toss_report(home=home),
        sqlite_fn=lambda: toss_report(
            home=home,
            snapshot_store=HoldingsSnapshotStore(sqlite, clock=utc_now),
        ),
    )


# =========================================================================
# G2-A bounded read-only live-shadow wiring.
#
# This composition root is the only sanctioned importer of the live Toss
# adapters, so the G2-A live provider factories are built here and injected
# into the runner harness in `programs/g2a_shadow_runner.py`.  The auth call is
# a single metered acquisition per hour (one memory-only bearer reused across
# all twelve ticks, its TOKEN start charging the UTC-day telemetry counter —
# historically capped at 3744, now monotonic telemetry only (G-10) — like any
# other provider start); calendar reads are GET only and are accounted through
# the request gate.  Every data-collector factory is fail-closed: on a closed
# session the collector never reaches it, and if it ever fires the runner STOPs
# before any data GET.
# =========================================================================


def _g2a_fail_closed_factory(label: str):
    def factory(_gate):
        def closure(*_args, **_kwargs):
            raise G2AStopError(f"provider_call_on_closed_session:{label}")

        return closure

    return factory


def _g2a_account_calendar_start(
    gate, tick, market: Market, local_trade_date: date, now: datetime
) -> None:
    operation = "CALENDAR_DAY_KR" if market is Market.KR else "CALENDAR_DAY_US"
    ordinal = 0 if market is Market.KR else 1
    params = {"market": market.value, "local_trade_date": local_trade_date.isoformat()}
    spec = REQUEST_OPERATION_SPECS[operation]
    invocation = f"{tick.tick_id:032x}"
    gate.ops_kernel.plan_requests(
        ProducerRole.COLLECTOR,
        tick.tick_id,
        "CALENDAR",
        (
            {
                "ordinal": ordinal,
                "operation": operation,
                "params_digest": canonical_params_digest(operation, params),
                "method": spec[0],
                "host": "openapi.tossinvest.com",
                "normalized_path_template": spec[1],
                "account_ordinal": None,
                "market": market.value,
                "local_trade_date": local_trade_date.isoformat(),
                "canonical_params": params,
            },
        ),
        invocation_id=invocation,
        now=now,
    )
    # Reserve the start durably before any calendar I/O (no refund on failure).
    gate.ops_kernel.start_request(
        ProducerRole.COLLECTOR,
        tick.tick_id,
        "CALENDAR",
        ordinal,
        invocation_id=invocation,
        now=now,
    )


def _g2a_account_calendar_settle(gate, tick, market: Market, now: datetime) -> None:
    ordinal = 0 if market is Market.KR else 1
    invocation = f"{tick.tick_id:032x}"
    gate.ops_kernel.settle_request(
        ProducerRole.COLLECTOR,
        tick.tick_id,
        "CALENDAR",
        ordinal,
        "OK",
        invocation_id=invocation,
        now=now,
    )


# The campaign bearer is acquired exactly once per hour under a fixed
# pre-cadence identity.  Tick id 0 is unreachable by any live cadence tick
# (cadence ids are epoch//300, ~5.9M for 2026), so the metered TOKEN row can
# neither collide with a real tick nor be rearmed on resume.
_G2A_TOKEN_TICK_ID = 0
_G2A_TOKEN_SCOPE = "CALENDAR"
_G2A_TOKEN_ORDINAL = 0
_G2A_TOKEN_INVOCATION = f"{_G2A_TOKEN_TICK_ID:032x}"


def _g2a_plan_start_token(ops_kernel, now: datetime) -> None:
    """Reserve the metered TOKEN row before the auth transport.

    The start is durable and charges the shared UTC-day telemetry counter
    (the historical 3744 ceiling is removed; the counter is monotonic
    telemetry only) exactly as any other provider start; there is no
    unmetered auth exception.
    """
    spec = REQUEST_OPERATION_SPECS["TOKEN"]
    params: dict = {}
    ops_kernel.plan_requests(
        ProducerRole.COLLECTOR,
        _G2A_TOKEN_TICK_ID,
        _G2A_TOKEN_SCOPE,
        (
            {
                "ordinal": _G2A_TOKEN_ORDINAL,
                "operation": "TOKEN",
                "params_digest": canonical_params_digest("TOKEN", params),
                "method": spec[0],
                "host": "openapi.tossinvest.com",
                "normalized_path_template": spec[1],
                "account_ordinal": None,
                "market": None,
                "local_trade_date": None,
                "canonical_params": params,
            },
        ),
        invocation_id=_G2A_TOKEN_INVOCATION,
        now=now,
    )
    ops_kernel.start_request(
        ProducerRole.COLLECTOR,
        _G2A_TOKEN_TICK_ID,
        _G2A_TOKEN_SCOPE,
        _G2A_TOKEN_ORDINAL,
        invocation_id=_G2A_TOKEN_INVOCATION,
        now=now,
    )


def _g2a_settle_token(ops_kernel, exit_code: str, now: datetime) -> None:
    ops_kernel.settle_request(
        ProducerRole.COLLECTOR,
        _G2A_TOKEN_TICK_ID,
        _G2A_TOKEN_SCOPE,
        _G2A_TOKEN_ORDINAL,
        exit_code,
        invocation_id=_G2A_TOKEN_INVOCATION,
        now=now,
    )


def build_g2a_live_deps(
    *,
    project_root: Path,
    clock: Clock,
    https_handler_factory=None,
) -> IntegratedCadenceDepsV1:
    """Construct the production G2-A deps from the real Toss adapters.

    ``https_handler_factory`` is a keyword-only seam for offline wire-parity
    tests (a zero-arg callable returning a fresh recording/scripted HTTPS
    handler per adapter); production leaves it ``None`` so the live adapters
    open their own hardened one-shot HTTPS opener.  A fresh handler per adapter
    keeps each opener's error-processing chain independent.
    """

    def _handler():
        return None if https_handler_factory is None else https_handler_factory()

    auth_transport = TossAuthLiveTransport(https_handler=_handler())
    reads_transport = TossReadsLiveTransport(https_handler=_handler())
    # Data and prices adapters are constructed for wiring parity but are only
    # reachable through the fail-closed factories (never called on a closed
    # session); they are GET-only and name no write method.
    TossDataLiveTransport(https_handler=_handler())
    TossPricesLiveTransport(https_handler=_handler())
    config = load_toss_config(project_root / TOSS_ENV_NAME)
    # Memory-only bearer state, shared across all twelve ticks of the hour.  The
    # value is never written to disk (bearer disk persistence is forbidden), so a
    # process that stays alive reuses it and a restart cannot recover it.
    token_state: dict = {}

    def _campaign_token(request_gate, now: datetime) -> str:
        # Same-process reuse: one acquisition serves every tick until the real
        # expiry.  If the bearer has expired mid-hour we STOP (never refresh).
        if token_state:
            if now >= token_state["expires_at"]:
                raise G2AStopError("token_expired")
            return token_state["value"]
        ops_kernel = request_gate.ops_kernel
        # A durable TOKEN row with no in-memory bearer means the process
        # restarted; the bearer is memory-only, so resume STOPs rather than
        # re-acquiring (which would double-charge the ceiling and rearm auth).
        prior = [
            row
            for row in ops_kernel.read_planned_requests(
                _G2A_TOKEN_TICK_ID, _G2A_TOKEN_SCOPE
            )
            if row["operation"] == "TOKEN"
        ]
        if prior:
            raise G2AStopError("token_unrecoverable")
        _g2a_plan_start_token(ops_kernel, now)
        outcome = auth_transport(
            build_auth_request(
                client_id=config.client_id, client_secret=config.client_secret
            )
        )
        if not isinstance(outcome, _TossTransportSuccess):
            _g2a_settle_token(ops_kernel, "AUTH_FAIL", now)
            raise G2AStopError(getattr(outcome, "code", "auth_transport_failure"))
        token = parse_token_response(outcome.body, observed_at=now)
        if not isinstance(token, TossAccessToken):
            _g2a_settle_token(ops_kernel, "PARSE_FAIL", now)
            raise G2AStopError("auth_parse")
        _g2a_settle_token(ops_kernel, "OK", now)
        token_state["value"] = token.value
        token_state["expires_at"] = now + timedelta(seconds=token.expires_in)
        return token.value

    def calendar_provider(*, market, local_trade_date, tick, request_gate):
        now = _aware_utc(clock())
        token = _campaign_token(request_gate, now)
        operation = (
            TossReadOperation.KR_MARKET_CALENDAR
            if market is Market.KR
            else TossReadOperation.US_MARKET_CALENDAR
        )
        request = build_read_request(
            operation, token=token, date=local_trade_date.isoformat()
        )
        _g2a_account_calendar_start(request_gate, tick, market, local_trade_date, now)
        outcome = reads_transport(request)
        if not isinstance(outcome, _TossTransportSuccess):
            raise G2AStopError(getattr(outcome, "code", "calendar_transport_failure"))
        response = parse_envelope(outcome.body, operation.value)
        if not isinstance(
            response, (KrMarketCalendarResponse, UsMarketCalendarResponse)
        ):
            raise G2AStopError("calendar_parse")
        _g2a_account_calendar_settle(request_gate, tick, market, now)
        return response.today

    return IntegratedCadenceDepsV1(
        calendar_provider=calendar_provider,
        holdings_provider_factory=_g2a_fail_closed_factory("holdings"),
        quotes_provider_factory=_g2a_fail_closed_factory("quotes"),
        orders_provider_factory=_g2a_fail_closed_factory("orders"),
        detail_provider_factory=_g2a_fail_closed_factory("detail"),
        glm_call=lambda *a, **k: None,
        sol_call=lambda *a, **k: None,
        regular_session_dates=object(),
        clock=lambda: _aware_utc(clock()),
    )


# =========================================================================
# G2-B bounded read-only US-open one-tick canary wiring.
#
# This is the first live run of the real *data* path (holdings, US holding
# quotes, completed CLOSED-order trades).  It reuses the G2-A shadow root and
# durable ops kernel, so its provider starts add to the same non-rearmable
# UTC-day counter rather than re-arming it.  One memory-only bearer is acquired
# under a single new B TOKEN identity, distinct from G2-A's terminal TOKEN row,
# and shared across every provider of the tick.  Each real provider closure
# reserves its request durably before I/O, enforces the hard canary ceilings,
# and never carries a raw body, token, account seq, URL, or exception text.
# =========================================================================

# The B bearer lives under a fixed pre-cadence identity distinct from G2-A's:
# tick id 0 (unreachable by any live cadence tick) under scope HOLDINGS, whereas
# G2-A scoped its TOKEN under CALENDAR.  Charging this row moves the shared day
# counter forward by one (e.g. 1 -> 2) without reusing the terminal A row.
#
# The bearer is memory-only, so a STOP + fresh process loses it.  Rather than
# hard-refusing every restart (which would make a code-repair rerun impossible),
# a repaired invocation acquires a *new* TOKEN identity: its ordinal is the count
# of durable prior B TOKEN rows (0, then 1, then 2), so each attempt is a unique,
# startable row that charges the shared day counter once and never re-arms it.
# The number of B token acquisitions per UTC day is hard-capped so a repair loop
# cannot mint tokens without bound.
_G2B_TOKEN_TICK_ID = 0
_G2B_TOKEN_SCOPE = "HOLDINGS"
_G2B_TOKEN_ORDINAL = 0
_G2B_TOKEN_INVOCATION = f"{_G2B_TOKEN_TICK_ID:032x}"
_G2B_TOKEN_MAX_ATTEMPTS = 10  # bounded explicit B invocations per UTC day

# Hard per-invocation canary ceilings.  They compose to at most
# 1 (TOKEN) + 1 (ACCOUNTS) + 5 (holdings) + 5 (quotes) + 12 (trades) = 24 <= 25.
# Per-tick data ceilings.  Across the twelve one-hour ticks (each run at most
# once, since a restart resumes durable progress) these compose to a campaign
# upper bound of up to ten TOKEN attempts + 12 * (1 ACCOUNTS + 5 holdings +
# 5 quotes + 12 trades) = 10 + 12 * 23 = 286 non-calendar provider starts.
_G2B_ACCOUNT_CEILING = 5
_G2B_QUOTE_BATCH_CEILING = 5
_G2B_TRADE_CEILING = 12  # CLOSED order pages + order details, combined
_G2B_PER_TICK_DATA_CEILING = (
    1 + _G2B_ACCOUNT_CEILING + _G2B_QUOTE_BATCH_CEILING + _G2B_TRADE_CEILING
)
_G2B_CAMPAIGN_DATA_CEILING = (
    _G2B_TOKEN_MAX_ATTEMPTS + 12 * _G2B_PER_TICK_DATA_CEILING
)  # 286 incl. up to 10 TOKENs

# The completed-trades window needs a real sequence of past regular-session
# dates.  This canary derives it from the *official* Toss market calendar: it
# walks descending local calendar dates, asking the same wired calendar
# transport whether each date carries a regular session, until twenty are
# found.  A hard walk bound and a campaign calendar ceiling keep it bounded.
_G2B_SESSION_DATE_MAX_WALK = 35  # descending calendar dates examined per market
_G2B_CALENDAR_CEILING = 70  # actual calendar wire attempts across the campaign


def _g2b_session_calendar_ordinal(market: Market) -> int:
    return 0 if market is Market.KR else 1


def _g2b_plan_start_session_calendar(
    ops_kernel, tick_id: int, market: Market, local_date: date, now: datetime
) -> None:
    """Reserve one descending-date calendar probe under a stable CALENDAR identity.

    The synthetic tick id is the queried date's proleptic-Gregorian ordinal
    (≈740k), unreachable by any real five-minute cadence tick (≈5.9M for 2026)
    and by the pre-cadence TOKEN row (tick 0), so the probe can neither collide
    with a live tick nor be re-armed on restart.  KR takes ordinal 0 and US
    ordinal 1, so both markets of one date share a synthetic tick within the
    kernel's two-per-tick calendar budget.
    """
    operation = "CALENDAR_DAY_KR" if market is Market.KR else "CALENDAR_DAY_US"
    ordinal = _g2b_session_calendar_ordinal(market)
    params = {"market": market.value, "local_trade_date": local_date.isoformat()}
    spec = REQUEST_OPERATION_SPECS[operation]
    invocation = f"{tick_id:032x}"
    ops_kernel.plan_requests(
        ProducerRole.COLLECTOR,
        tick_id,
        "CALENDAR",
        (
            {
                "ordinal": ordinal,
                "operation": operation,
                "params_digest": canonical_params_digest(operation, params),
                "method": spec[0],
                "host": "openapi.tossinvest.com",
                "normalized_path_template": spec[1],
                "account_ordinal": None,
                "market": market.value,
                "local_trade_date": local_date.isoformat(),
                "canonical_params": params,
            },
        ),
        invocation_id=invocation,
        now=now,
    )
    ops_kernel.start_request(
        ProducerRole.COLLECTOR,
        tick_id,
        "CALENDAR",
        ordinal,
        invocation_id=invocation,
        now=now,
    )


def _g2b_settle_session_calendar(
    ops_kernel, tick_id: int, market: Market, exit_code: str, now: datetime
) -> None:
    ops_kernel.settle_request(
        ProducerRole.COLLECTOR,
        tick_id,
        "CALENDAR",
        _g2b_session_calendar_ordinal(market),
        exit_code,
        invocation_id=f"{tick_id:032x}",
        now=now,
    )


class _G2BSessionDateCache:
    """Durable, owner-only cache of sanitized ``(market, date) -> is_open`` facts.

    It records only whether a local calendar date carries a regular session —
    never a token, body, URL, or account identity — so the completed-trades
    window can be rebuilt across ticks and restarts without re-hitting the
    calendar wire.  The file lives inside the same shadow root at mode 0600.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            os.chmod(path, 0o600)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS session_open("
                "market TEXT NOT NULL CHECK(market IN ('KR','US')), "
                "local_date TEXT NOT NULL, "
                "is_open INTEGER NOT NULL CHECK(is_open IN (0,1)), "
                "PRIMARY KEY(market, local_date)) STRICT"
            )
            connection.commit()
        finally:
            connection.close()

    def get(self, market: str, local_date: date):
        connection = sqlite3.connect(self._path)
        try:
            row = connection.execute(
                "SELECT is_open FROM session_open WHERE market=? AND local_date=?",
                (market, local_date.isoformat()),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else bool(row[0])

    def put(self, market: str, local_date: date, is_open: bool) -> None:
        connection = sqlite3.connect(self._path)
        try:
            connection.execute(
                "INSERT INTO session_open VALUES(?,?,?) "
                "ON CONFLICT(market, local_date) DO UPDATE SET is_open=excluded.is_open",
                (market, local_date.isoformat(), 1 if is_open else 0),
            )
            connection.commit()
            os.chmod(self._path, 0o600)
        finally:
            connection.close()


class _G2BCalendarSessionDates:
    """Real ``regular_session_dates`` backed by the official Toss calendar.

    ``run_trades_sync`` asks for twenty past regular-session dates per market.
    This source walks descending local calendar dates from the anchor, asking
    the wired calendar transport whether each date carries a regular session,
    caching every sanitized answer durably, and stopping once twenty are found.
    It never fabricates a calendar: if twenty regular sessions are not found
    within thirty-five examined dates (or the campaign's seventy-attempt
    calendar ceiling is reached) it STOPs ``session_dates_incomplete``, and any
    transport failure or redirect STOPs without retry (its start consumed).
    """

    def __init__(
        self,
        *,
        token_acquire,
        reads_transport,
        clock,
        gate_state,
        calendar_attempts,
        pace_seconds: float = 0.0,
    ) -> None:
        self._token_acquire = token_acquire
        self._reads_transport = reads_transport
        self._clock = clock
        self._gate_state = gate_state
        self._calendar_attempts = calendar_attempts
        self._pace_seconds = pace_seconds
        self._cache: _G2BSessionDateCache | None = None

    def _cache_for(self, ops_kernel) -> _G2BSessionDateCache:
        if self._cache is None:
            root_ops = Path(ops_kernel.path).parent
            self._cache = _G2BSessionDateCache(root_ops / "g2b_session_dates.sqlite3")
        return self._cache

    def regular_session_dates(
        self, market: Market, anchor_date: date, count: int
    ) -> tuple[date, ...]:
        if (
            not isinstance(market, Market)
            or type(anchor_date) is not date
            or type(count) is not int
            or count < 1
        ):
            raise ValueError("invalid session-date query")
        gate = self._gate_state.get("gate")
        if gate is None:
            raise G2AStopError("session_dates_no_gate")
        ops_kernel = gate.ops_kernel
        cache = self._cache_for(ops_kernel)
        now = _aware_utc(self._clock())
        token = self._token_acquire(gate, now)
        sessions: list[date] = []
        walked = 0
        cursor = anchor_date
        while len(sessions) < count:
            if walked >= _G2B_SESSION_DATE_MAX_WALK:
                raise G2AStopError("session_dates_incomplete")
            is_open = cache.get(market.value, cursor)
            if is_open is None:
                is_open = self._probe(ops_kernel, market, cursor, token, now)
                cache.put(market.value, cursor, is_open)
            walked += 1
            if is_open:
                sessions.append(cursor)
            cursor = cursor - timedelta(days=1)
        return tuple(sessions)

    def _probe(
        self, ops_kernel, market: Market, local_date: date, token: str, now: datetime
    ) -> bool:
        # Campaign calendar ceiling: block the seventy-first actual attempt
        # before any transport (a refusal never consumes a durable start).
        if self._calendar_attempts["n"] + 1 > _G2B_CALENDAR_CEILING:
            raise G2AStopError("session_dates_incomplete")
        if self._calendar_attempts["n"] and self._pace_seconds:
            time.sleep(self._pace_seconds)
        self._calendar_attempts["n"] += 1
        tick_id = local_date.toordinal()
        operation = (
            TossReadOperation.KR_MARKET_CALENDAR
            if market is Market.KR
            else TossReadOperation.US_MARKET_CALENDAR
        )
        request = build_read_request(
            operation, token=token, date=local_date.isoformat()
        )
        _g2b_plan_start_session_calendar(ops_kernel, tick_id, market, local_date, now)
        outcome = self._reads_transport(request)
        if not isinstance(outcome, _TossTransportSuccess):
            code = getattr(outcome, "code", "calendar_transport_failure")
            _g2b_settle_session_calendar(
                ops_kernel, tick_id, market, _g2b_exit_for_code(code), now
            )
            raise G2AStopError(code)
        response = parse_envelope(outcome.body, operation.value)
        if not isinstance(
            response, (KrMarketCalendarResponse, UsMarketCalendarResponse)
        ):
            _g2b_settle_session_calendar(ops_kernel, tick_id, market, "PARSE_FAIL", now)
            raise G2AStopError("calendar_parse")
        # The official response must describe *the date we asked for*.  A calendar
        # that echoes an adjacent business day (e.g. a weekend/holiday probe
        # answered with the neighbouring session's date) would otherwise be
        # cached as this date's open/closed fact — a silent off-by-one in the
        # completed-trades window.  Mismatch is a hard STOP before any cache put.
        if response.today.date != local_date:
            _g2b_settle_session_calendar(ops_kernel, tick_id, market, "PARSE_FAIL", now)
            raise G2AStopError("calendar_date_mismatch")
        try:
            session = regular_session_for(response.today, market, now)
        except ValueError:
            _g2b_settle_session_calendar(ops_kernel, tick_id, market, "PARSE_FAIL", now)
            raise G2AStopError("calendar_parse") from None
        _g2b_settle_session_calendar(ops_kernel, tick_id, market, "OK", now)
        return session is not None


class _G2BCanaryLedger:
    """Campaign-durable admission ledger for a bounded B run (one or twelve ticks).

    The **campaign** total — every non-calendar provider start across the whole
    run — is capped at 286 (up to ten TOKEN attempts plus twelve ticks of 23
    data starts, each tick run once across the campaign).  The
    **per-tick** sub-ceilings (5 account holdings, 5 quote batches, 12 combined
    CLOSED pages + order details) are reset at each tick boundary by
    ``begin_tick``.  Every ``admit_*`` checks its ceiling *before* the N+1
    durable start / network I/O; a refusal never charges a durable counter, and
    a failure after admission (a bad transport reply) has already consumed its
    slot.  (Calendar probes are bounded separately by the session-date source.)

    ``campaign_ceiling`` is injectable so a longer campaign can dedicate its
    own total (G2-D injects 2310 per §10.5) without re-arming G2-B's 286.
    """

    def __init__(self, campaign_ceiling: int = _G2B_CAMPAIGN_DATA_CEILING) -> None:
        self.campaign_ceiling = campaign_ceiling
        self.total = 0  # campaign-wide non-calendar provider starts
        self._tick: int | None = None
        self.accounts = 0  # per-tick, reset by begin_tick
        self.quote_batches = 0  # per-tick
        self.trades = 0  # per-tick

    def begin_tick(self, tick_id: int) -> None:
        if tick_id != self._tick:
            self._tick = tick_id
            self.accounts = 0
            self.quote_batches = 0
            self.trades = 0

    def _admit_total(self) -> None:
        if self.total + 1 > self.campaign_ceiling:
            raise OpsKernelBudgetExceeded("g2b_ceiling_total")
        self.total += 1

    def admit_token(self) -> None:
        self._admit_total()

    def admit_accounts(self) -> None:
        self._admit_total()

    def admit_account_holdings(self) -> None:
        if self.accounts + 1 > _G2B_ACCOUNT_CEILING:
            raise OpsKernelBudgetExceeded("g2b_ceiling_accounts")
        self._admit_total()
        self.accounts += 1

    def admit_quote_batch(self) -> None:
        if self.quote_batches + 1 > _G2B_QUOTE_BATCH_CEILING:
            raise OpsKernelBudgetExceeded("g2b_ceiling_quotes")
        self._admit_total()
        self.quote_batches += 1

    def admit_trade(self) -> None:
        if self.trades + 1 > _G2B_TRADE_CEILING:
            raise OpsKernelBudgetExceeded("g2b_ceiling_trades")
        self._admit_total()
        self.trades += 1


def _g2b_plan_start_token(
    ops_kernel, now: datetime, ordinal: int = _G2B_TOKEN_ORDINAL
) -> None:
    """Plan and start one B TOKEN attempt at ``ordinal`` (a unique retry slot).

    Each repaired invocation acquires a fresh identity at the next unused
    ordinal, so a prior invocation's terminal TOKEN row never blocks the new
    start and the shared day counter advances exactly once per attempt.
    """
    spec = REQUEST_OPERATION_SPECS["TOKEN"]
    params: dict = {}
    ops_kernel.plan_requests(
        ProducerRole.COLLECTOR,
        _G2B_TOKEN_TICK_ID,
        _G2B_TOKEN_SCOPE,
        (
            {
                "ordinal": ordinal,
                "operation": "TOKEN",
                "params_digest": canonical_params_digest("TOKEN", params),
                "method": spec[0],
                "host": "openapi.tossinvest.com",
                "normalized_path_template": spec[1],
                "account_ordinal": None,
                "market": None,
                "local_trade_date": None,
                "canonical_params": params,
            },
        ),
        invocation_id=_G2B_TOKEN_INVOCATION,
        now=now,
    )
    ops_kernel.start_request(
        ProducerRole.COLLECTOR,
        _G2B_TOKEN_TICK_ID,
        _G2B_TOKEN_SCOPE,
        ordinal,
        invocation_id=_G2B_TOKEN_INVOCATION,
        now=now,
    )


def _g2b_settle_token(
    ops_kernel, exit_code: str, now: datetime, ordinal: int = _G2B_TOKEN_ORDINAL
) -> None:
    ops_kernel.settle_request(
        ProducerRole.COLLECTOR,
        _G2B_TOKEN_TICK_ID,
        _G2B_TOKEN_SCOPE,
        ordinal,
        exit_code,
        invocation_id=_G2B_TOKEN_INVOCATION,
        now=now,
    )


def _g2b_plan_start(
    ops_kernel,
    tick_id: int,
    scope: str,
    *,
    ordinal: int,
    operation: str,
    params: dict,
    account_ordinal: int | None,
    now: datetime,
) -> None:
    spec = REQUEST_OPERATION_SPECS[operation]
    invocation = f"{tick_id:032x}"
    ops_kernel.plan_requests(
        ProducerRole.COLLECTOR,
        tick_id,
        scope,
        (
            {
                "ordinal": ordinal,
                "operation": operation,
                "params_digest": canonical_params_digest(operation, params),
                "method": spec[0],
                "host": "openapi.tossinvest.com",
                "normalized_path_template": spec[1],
                "account_ordinal": account_ordinal,
                "market": None,
                "local_trade_date": None,
                "canonical_params": params,
            },
        ),
        invocation_id=invocation,
        now=now,
    )
    ops_kernel.start_request(
        ProducerRole.COLLECTOR,
        tick_id,
        scope,
        ordinal,
        invocation_id=invocation,
        now=now,
    )


def _g2b_settle(
    ops_kernel, tick_id: int, scope: str, ordinal: int, exit_code: str, now: datetime
) -> None:
    ops_kernel.settle_request(
        ProducerRole.COLLECTOR,
        tick_id,
        scope,
        ordinal,
        exit_code,
        invocation_id=f"{tick_id:032x}",
        now=now,
    )


def _g2b_plan_arm_account_holdings(
    ops_kernel, tick_id: int, count: int, now: datetime
) -> None:
    """Plan the full five-slot ACCOUNT_HOLDINGS set and arm the first ``count``.

    The kernel's account lifecycle requires all five ordinals planned together;
    arming keeps 1..count (ARMED, startable) and cancels count+1..5
    (CANCELLED_UNSTARTED), so only the real accounts can ever be started and
    charge the day counter.
    """
    spec = REQUEST_OPERATION_SPECS["ACCOUNT_HOLDINGS"]
    invocation = f"{tick_id:032x}"
    rows = tuple(
        {
            "ordinal": account_ordinal + 1,
            "operation": "ACCOUNT_HOLDINGS",
            "params_digest": canonical_params_digest(
                "ACCOUNT_HOLDINGS", {"account_ordinal": account_ordinal}
            ),
            "method": spec[0],
            "host": "openapi.tossinvest.com",
            "normalized_path_template": spec[1],
            "account_ordinal": account_ordinal,
            "market": None,
            "local_trade_date": None,
            "canonical_params": {"account_ordinal": account_ordinal},
        }
        for account_ordinal in range(1, 6)
    )
    ops_kernel.plan_requests(
        ProducerRole.COLLECTOR,
        tick_id,
        "HOLDINGS",
        rows,
        invocation_id=invocation,
        now=now,
    )
    ops_kernel.arm_account_rows(
        ProducerRole.COLLECTOR, tick_id, count, invocation_id=invocation, now=now
    )


def _g2b_exit_for_code(code: str) -> str:
    """Map a masked provider/transport code to a durable settlement exit."""
    return {
        "http_429": "RATE_LIMITED",
        "redirect": "REDIRECT",
        "auth_rejection": "AUTH_FAIL",
        "forbidden": "PROVIDER_ERROR",
        "parse_error": "PARSE_FAIL",
        "contract_mismatch": "PARSE_FAIL",
        "http_5xx": "TRANSPORT_FAIL",
        "bad_request": "TRANSPORT_FAIL",
    }.get(code, "TRANSPORT_FAIL")


def build_g2b_live_deps(
    *,
    project_root: Path,
    clock: Clock,
    https_handler_factory=None,
) -> IntegratedCadenceDepsV1:
    """Construct the production G2-B data-path deps from the real Toss adapters.

    A single memory-only bearer is acquired lazily and shared by every provider
    of the tick.  Holdings, US quotes, and CLOSED-order trades each reserve
    their requests on the shared durable kernel and are bounded by the canary
    ledger.  ``https_handler_factory`` is the keyword-only offline seam (a
    zero-arg callable minting a fresh scripted handler per adapter); production
    leaves it ``None`` so the live adapters open their own hardened opener.
    """

    def _handler():
        return None if https_handler_factory is None else https_handler_factory()

    auth_transport = TossAuthLiveTransport(https_handler=_handler())
    data_transport = TossDataLiveTransport(https_handler=_handler())
    prices_transport = TossPricesLiveTransport(https_handler=_handler())
    reads_transport = TossReadsLiveTransport(https_handler=_handler())
    config = load_toss_config(project_root / TOSS_ENV_NAME)

    ledger = _G2BCanaryLedger()
    token_state: dict = {}
    seq_by_scope: dict[str, str] = {}
    tick_state: dict = {}
    gate_state: dict = {}
    calendar_attempts = {"n": 0}
    quote_ordinal = {"n": 0}
    detail_ordinal = {"n": 10}  # ORDER_DETAIL occupies TRADES ordinals 10..11

    def _pinned_tick(now: datetime):
        # Pin the tick identity *within* one cadence tick (so every provider of
        # a tick shares one tick id) but refresh it when the cadence advances to
        # a new tick — the calendar provider is cache-gated and may not run each
        # tick, so a stale pin would make later ticks replan under an already
        # terminal tick-0 row ("not startable").  Deriving from the shared clock
        # keeps each tick's planned identities unique and startable.
        current = tick_id_from_utc(now)
        pinned = tick_state.get("tick")
        if pinned is None or pinned.tick_id != current.tick_id:
            tick_state["tick"] = current
        return tick_state["tick"]

    def _begin_tick(tick) -> None:
        # A tick boundary resets the per-tick ordinals and per-tick ceilings so
        # each of the twelve one-hour ticks gets a fresh 5/5/12 budget and its
        # own kernel ordinal namespace, while the campaign total keeps climbing.
        if tick_state.get("current") != tick.tick_id:
            tick_state["current"] = tick.tick_id
            quote_ordinal["n"] = 0
            detail_ordinal["n"] = 10
            seq_by_scope.clear()
        ledger.begin_tick(tick.tick_id)

    def _campaign_token(request_gate, now: datetime) -> str:
        if token_state:
            if now >= token_state["expires_at"]:
                raise G2AStopError("token_expired")
            return token_state["value"]
        ops_kernel = request_gate.ops_kernel
        # The attempt ordinal is the number of durable prior B TOKEN rows (any
        # state — a crashed prior attempt was reconciled to a terminal UNKNOWN
        # at runner startup, so it still counts).  Once the cap is consumed the
        # canary STOPs *before* any auth transport, so a repair loop can never
        # mint an unbounded number of tokens; the day counter is never reset.
        attempt = len(
            [
                row
                for row in ops_kernel.read_planned_requests(
                    _G2B_TOKEN_TICK_ID, _G2B_TOKEN_SCOPE
                )
                if row["operation"] == "TOKEN"
            ]
        )
        if attempt >= _G2B_TOKEN_MAX_ATTEMPTS:
            raise G2AStopError("token_attempts_exhausted")
        ledger.admit_token()
        _g2b_plan_start_token(ops_kernel, now, attempt)
        outcome = auth_transport(
            build_auth_request(
                client_id=config.client_id, client_secret=config.client_secret
            )
        )
        if not isinstance(outcome, _TossTransportSuccess):
            _g2b_settle_token(ops_kernel, "AUTH_FAIL", now, attempt)
            raise G2AStopError(getattr(outcome, "code", "auth_transport_failure"))
        token = parse_token_response(outcome.body, observed_at=now)
        if not isinstance(token, TossAccessToken):
            _g2b_settle_token(ops_kernel, "PARSE_FAIL", now, attempt)
            raise G2AStopError("auth_parse")
        _g2b_settle_token(ops_kernel, "OK", now, attempt)
        token_state["value"] = token.value
        token_state["expires_at"] = now + timedelta(seconds=token.expires_in)
        return token.value

    def calendar_provider(*, market, local_trade_date, tick, request_gate):
        now = _aware_utc(clock())
        tick_state["tick"] = tick
        gate_state["gate"] = request_gate
        _begin_tick(tick)
        token = _campaign_token(request_gate, now)
        operation = (
            TossReadOperation.KR_MARKET_CALENDAR
            if market is Market.KR
            else TossReadOperation.US_MARKET_CALENDAR
        )
        request = build_read_request(
            operation, token=token, date=local_trade_date.isoformat()
        )
        _g2a_account_calendar_start(request_gate, tick, market, local_trade_date, now)
        outcome = reads_transport(request)
        if not isinstance(outcome, _TossTransportSuccess):
            raise G2AStopError(getattr(outcome, "code", "calendar_transport_failure"))
        response = parse_envelope(outcome.body, operation.value)
        if not isinstance(
            response, (KrMarketCalendarResponse, UsMarketCalendarResponse)
        ):
            raise G2AStopError("calendar_parse")
        # The gate's open/closed verdict must be built from the calendar day we
        # actually requested; a date echo mismatch STOPs rather than judging the
        # session from the wrong day.
        if response.today.date != local_trade_date:
            raise G2AStopError("calendar_date_mismatch")
        _g2a_account_calendar_settle(request_gate, tick, market, now)
        return response.today

    def holdings_provider_factory(gate):
        gate_state["gate"] = gate

        def provider():
            now = _aware_utc(clock())
            tick = _pinned_tick(now)
            _begin_tick(tick)
            token = _campaign_token(gate, now)
            ops_kernel = gate.ops_kernel
            # 1) Account discovery (ACCOUNTS, ordinal 0 under HOLDINGS).
            ledger.admit_accounts()
            _g2b_plan_start(
                ops_kernel,
                tick.tick_id,
                "HOLDINGS",
                ordinal=0,
                operation="ACCOUNTS",
                params={},
                account_ordinal=None,
                now=now,
            )
            reply = data_transport(build_accounts_request(token=token))
            if not isinstance(reply, _TossTransportSuccess):
                _g2b_settle(
                    ops_kernel,
                    tick.tick_id,
                    "HOLDINGS",
                    0,
                    _g2b_exit_for_code(getattr(reply, "code", "")),
                    now,
                )
                return TossCampaignOutcome(
                    getattr(reply, "code", "accounts_transport_failure"),
                    "accounts",
                    0,
                    (),
                    (),
                )
            account_list = parse_accounts_response(reply.body, observed_at=now)
            if not isinstance(account_list, TossAccountList):
                _g2b_settle(ops_kernel, tick.tick_id, "HOLDINGS", 0, "PARSE_FAIL", now)
                return TossCampaignOutcome("accounts_parse", "accounts", 0, (), ())
            _g2b_settle(ops_kernel, tick.tick_id, "HOLDINGS", 0, "OK", now)
            seqs = account_list.seqs
            count = len(seqs)
            if not 1 <= count <= _G2B_ACCOUNT_CEILING:
                return TossCampaignOutcome(
                    "account_count_unexpected", "accounts", 0, (), ()
                )
            identities, fingerprints = derive_account_identities(account_list)
            # 2) Per-account holdings (ACCOUNT_HOLDINGS, ordinal account+1).  The
            # kernel account lifecycle needs all five slots planned then armed;
            # only the real accounts are started and charged.
            _g2b_plan_arm_account_holdings(ops_kernel, tick.tick_id, count, now)
            facts = []
            for index, seq in enumerate(seqs):
                account_ordinal = index + 1
                ledger.admit_account_holdings()
                ops_kernel.start_request(
                    ProducerRole.COLLECTOR,
                    tick.tick_id,
                    "HOLDINGS",
                    account_ordinal + 1,
                    invocation_id=f"{tick.tick_id:032x}",
                    now=now,
                )
                held = data_transport(
                    build_holdings_request(token=token, account_seq=seq)
                )
                if not isinstance(held, _TossTransportSuccess):
                    _g2b_settle(
                        ops_kernel,
                        tick.tick_id,
                        "HOLDINGS",
                        account_ordinal + 1,
                        _g2b_exit_for_code(getattr(held, "code", "")),
                        now,
                    )
                    return TossCampaignOutcome(
                        getattr(held, "code", "holdings_transport_failure"),
                        "holdings",
                        0,
                        (),
                        (),
                    )
                fact = parse_holdings_response(
                    held.body,
                    account_ordinal=account_ordinal,
                    account_count=count,
                    captured_at=now,
                )
                if not isinstance(fact, TossAccountHoldingsFact):
                    _g2b_settle(
                        ops_kernel,
                        tick.tick_id,
                        "HOLDINGS",
                        account_ordinal + 1,
                        "PARSE_FAIL",
                        now,
                    )
                    return TossCampaignOutcome("holdings_parse", "holdings", 0, (), ())
                _g2b_settle(
                    ops_kernel, tick.tick_id, "HOLDINGS", account_ordinal + 1, "OK", now
                )
                facts.append(fact)
                seq_by_scope[identities[index].account_scope] = seq
            return TossCampaignOutcome(
                "success",
                "holdings",
                count,
                tuple(facts),
                (),
                account_identities=identities,
                _identity_fingerprints=fingerprints,
            )

        return provider

    def quotes_provider_factory(gate):
        gate_state["gate"] = gate

        def provider(symbols):
            batch = tuple(symbols)
            now = _aware_utc(clock())
            tick = _pinned_tick(now)
            _begin_tick(tick)
            token = _campaign_token(gate, now)
            ops_kernel = gate.ops_kernel
            # KR is closed for this canary, so the collector only ever selects US
            # symbols; the batch is labelled US for parity.
            ledger.admit_quote_batch()
            ordinal = quote_ordinal["n"]
            quote_ordinal["n"] += 1
            params = {
                "market": "US",
                "sorted_unique_symbols": tuple(sorted(set(batch))),
            }
            _g2b_plan_start(
                ops_kernel,
                tick.tick_id,
                "QUOTES",
                ordinal=ordinal,
                operation="PRICE_BATCH",
                params=params,
                account_ordinal=None,
                now=now,
            )
            outcome = query_current_prices(
                symbols=batch,
                token=token,
                transport=prices_transport,
                clock=lambda: now,
            )
            _g2b_settle(
                ops_kernel,
                tick.tick_id,
                "QUOTES",
                ordinal,
                "OK" if outcome.code == "success" else _g2b_exit_for_code(outcome.code),
                now,
            )
            return outcome

        return provider

    def orders_provider_factory(gate):
        gate_state["gate"] = gate

        def provider(*, account_scope, status, from_date, to_date, cursor, limit):
            # run_trades_sync reserves the ORDERS_PAGE row itself (plan/start/
            # settle) around this call; the closure only performs the single
            # CLOSED read.  The ledger caps pages+details defensively.
            now = _aware_utc(clock())
            token = _campaign_token(gate, now)
            seq = seq_by_scope.get(account_scope)
            if seq is None:
                return TossReadOutcome("account_unmapped")
            ledger.admit_trade()
            # ``build_read_request`` names the window bounds ``from``/``to`` (the
            # wire query keys), so translate the provider's from_date/to_date.
            request = build_read_request(
                TossReadOperation.ORDERS,
                token=token,
                account_seq=seq,
                status=status,
                cursor=cursor,
                limit=limit,
                **{"from": from_date, "to": to_date},
            )
            return query_read(request, transport=reads_transport, clock=lambda: now)

        return provider

    def detail_provider_factory(gate):
        gate_state["gate"] = gate

        def provider(*, account_scope, order_id):
            now = _aware_utc(clock())
            tick = _pinned_tick(now)
            _begin_tick(tick)
            token = _campaign_token(gate, now)
            ops_kernel = gate.ops_kernel
            seq = seq_by_scope.get(account_scope)
            if seq is None:
                return None
            ledger.admit_trade()
            ordinal = detail_ordinal["n"]
            detail_ordinal["n"] += 1
            params = {"account_scope": account_scope, "order_id": order_id}
            _g2b_plan_start(
                ops_kernel,
                tick.tick_id,
                "TRADES",
                ordinal=ordinal,
                operation="ORDER_DETAIL",
                params=params,
                account_ordinal=None,
                now=now,
            )
            request = build_read_request(
                TossReadOperation.ORDER_DETAIL,
                token=token,
                account_seq=seq,
                order_id=order_id,
            )
            outcome = query_read(request, transport=reads_transport, clock=lambda: now)
            _g2b_settle(
                ops_kernel,
                tick.tick_id,
                "TRADES",
                ordinal,
                "OK" if outcome.code == "success" else _g2b_exit_for_code(outcome.code),
                now,
            )
            return outcome

        return provider

    session_dates = _G2BCalendarSessionDates(
        token_acquire=_campaign_token,
        reads_transport=reads_transport,
        clock=clock,
        gate_state=gate_state,
        calendar_attempts=calendar_attempts,
        pace_seconds=0.3 if https_handler_factory is None else 0.0,
    )

    return IntegratedCadenceDepsV1(
        calendar_provider=calendar_provider,
        holdings_provider_factory=holdings_provider_factory,
        quotes_provider_factory=quotes_provider_factory,
        orders_provider_factory=orders_provider_factory,
        detail_provider_factory=detail_provider_factory,
        glm_call=lambda *a, **k: None,
        sol_call=lambda *a, **k: None,
        regular_session_dates=session_dates,
        clock=lambda: _aware_utc(clock()),
    )


def build_g2d_live_deps(
    *,
    project_root: Path,
    clock: Clock,
    glm_call,
    sol_call,
    https_handler_factory=None,
) -> IntegratedCadenceDepsV1:
    """Construct the production G2-D full-session deps: G2-B's live data path
    with a *bounded* in-process campaign bearer, and caller-injected scripted
    GLM/SOL model slots (no live model provider is ever imported or called).

    G2-D reuses every G2-B data provider and durable request helper, dedicates
    the campaign ledger to the §10.5 total (2310, not G2-B's 286), and bounds
    the bearer per §10.4: within the one process, an expired memory bearer is
    re-acquired under a fresh durable TOKEN identity up to ten TOTAL durable
    TOKEN calls (each charging the ledger and the shared day counter); the
    eleventh call, or any acquisition failure, is the terminal
    ``token_lost_incomplete`` STOP.  Restart, resume, and replay stay forbidden
    — a new process never inherits this campaign's bearer or grid.  ``glm_call``
    and ``sol_call`` are required, wrapped in the injection marker the runner
    checks, so a live model provider can never reach these slots (§10.7).
    """
    if not callable(glm_call) or not callable(sol_call):
        raise ValueError("glm/sol model slots must be caller-injected callables")

    def _handler():
        return None if https_handler_factory is None else https_handler_factory()

    auth_transport = TossAuthLiveTransport(https_handler=_handler())
    data_transport = TossDataLiveTransport(https_handler=_handler())
    prices_transport = TossPricesLiveTransport(https_handler=_handler())
    reads_transport = TossReadsLiveTransport(https_handler=_handler())
    config = load_toss_config(project_root / TOSS_ENV_NAME)

    ledger = _G2BCanaryLedger(campaign_ceiling=G2D_CAMPAIGN_DATA_CEILING)
    token_state: dict = {}
    seq_by_scope: dict[str, str] = {}
    tick_state: dict = {}
    gate_state: dict = {}
    calendar_attempts = {"n": 0}
    quote_ordinal = {"n": 0}
    detail_ordinal = {"n": 10}

    def _pinned_tick(now: datetime):
        current = tick_id_from_utc(now)
        pinned = tick_state.get("tick")
        if pinned is None or pinned.tick_id != current.tick_id:
            tick_state["tick"] = current
        return tick_state["tick"]

    def _begin_tick(tick) -> None:
        if tick_state.get("current") != tick.tick_id:
            tick_state["current"] = tick.tick_id
            quote_ordinal["n"] = 0
            detail_ordinal["n"] = 10
            seq_by_scope.clear()
        ledger.begin_tick(tick.tick_id)

    def _campaign_token(request_gate, now: datetime) -> str:
        # Bounded in-process bearer (§10.4): a valid memory bearer is reused;
        # an expired one is re-acquired under a fresh durable TOKEN identity.
        # The campaign allows at most _G2B_TOKEN_MAX_ATTEMPTS (10) TOTAL
        # durable TOKEN calls — the token allowance the 2310 formula reserves —
        # each charging the dedicated ledger and the shared day counter.  The
        # eleventh call, or any transport/parse failure, is the terminal
        # ``token_lost_incomplete`` STOP; the failure is latched in
        # ``token_state`` so the runner reports the true cause even when a
        # stage degrades the raised control signal into a failed receipt.
        # Restart/resume/replay stay forbidden — this never survives the
        # process.
        if token_state.get("lost"):
            raise G2AStopError("token_lost_incomplete")
        if "value" in token_state and now < token_state["expires_at"]:
            return token_state["value"]
        token_state.pop("value", None)
        token_state.pop("expires_at", None)
        ops_kernel = request_gate.ops_kernel
        attempt = len(
            [
                row
                for row in ops_kernel.read_planned_requests(
                    _G2B_TOKEN_TICK_ID, _G2B_TOKEN_SCOPE
                )
                if row["operation"] == "TOKEN"
            ]
        )
        if attempt >= _G2B_TOKEN_MAX_ATTEMPTS:
            token_state["lost"] = True
            raise G2AStopError("token_lost_incomplete")
        ledger.admit_token()
        _g2b_plan_start_token(ops_kernel, now, attempt)
        outcome = auth_transport(
            build_auth_request(
                client_id=config.client_id, client_secret=config.client_secret
            )
        )
        if not isinstance(outcome, _TossTransportSuccess):
            _g2b_settle_token(ops_kernel, "AUTH_FAIL", now, attempt)
            token_state["lost"] = True
            raise G2AStopError("token_lost_incomplete")
        token = parse_token_response(outcome.body, observed_at=now)
        if not isinstance(token, TossAccessToken):
            _g2b_settle_token(ops_kernel, "PARSE_FAIL", now, attempt)
            token_state["lost"] = True
            raise G2AStopError("token_lost_incomplete")
        _g2b_settle_token(ops_kernel, "OK", now, attempt)
        token_state["value"] = token.value
        token_state["expires_at"] = now + timedelta(seconds=token.expires_in)
        return token.value

    def calendar_provider(*, market, local_trade_date, tick, request_gate):
        now = _aware_utc(clock())
        tick_state["tick"] = tick
        gate_state["gate"] = request_gate
        _begin_tick(tick)
        token = _campaign_token(request_gate, now)
        operation = (
            TossReadOperation.KR_MARKET_CALENDAR
            if market is Market.KR
            else TossReadOperation.US_MARKET_CALENDAR
        )
        request = build_read_request(
            operation, token=token, date=local_trade_date.isoformat()
        )
        _g2a_account_calendar_start(request_gate, tick, market, local_trade_date, now)
        outcome = reads_transport(request)
        if not isinstance(outcome, _TossTransportSuccess):
            raise G2AStopError(getattr(outcome, "code", "calendar_transport_failure"))
        response = parse_envelope(outcome.body, operation.value)
        if not isinstance(
            response, (KrMarketCalendarResponse, UsMarketCalendarResponse)
        ):
            raise G2AStopError("calendar_parse")
        if response.today.date != local_trade_date:
            raise G2AStopError("calendar_date_mismatch")
        _g2a_account_calendar_settle(request_gate, tick, market, now)
        return response.today

    def holdings_provider_factory(gate):
        gate_state["gate"] = gate

        def provider():
            now = _aware_utc(clock())
            tick = _pinned_tick(now)
            _begin_tick(tick)
            token = _campaign_token(gate, now)
            ops_kernel = gate.ops_kernel
            ledger.admit_accounts()
            _g2b_plan_start(
                ops_kernel,
                tick.tick_id,
                "HOLDINGS",
                ordinal=0,
                operation="ACCOUNTS",
                params={},
                account_ordinal=None,
                now=now,
            )
            reply = data_transport(build_accounts_request(token=token))
            if not isinstance(reply, _TossTransportSuccess):
                _g2b_settle(
                    ops_kernel,
                    tick.tick_id,
                    "HOLDINGS",
                    0,
                    _g2b_exit_for_code(getattr(reply, "code", "")),
                    now,
                )
                return TossCampaignOutcome(
                    getattr(reply, "code", "accounts_transport_failure"),
                    "accounts",
                    0,
                    (),
                    (),
                )
            account_list = parse_accounts_response(reply.body, observed_at=now)
            if not isinstance(account_list, TossAccountList):
                _g2b_settle(ops_kernel, tick.tick_id, "HOLDINGS", 0, "PARSE_FAIL", now)
                return TossCampaignOutcome("accounts_parse", "accounts", 0, (), ())
            _g2b_settle(ops_kernel, tick.tick_id, "HOLDINGS", 0, "OK", now)
            seqs = account_list.seqs
            count = len(seqs)
            if not 1 <= count <= _G2B_ACCOUNT_CEILING:
                return TossCampaignOutcome(
                    "account_count_unexpected", "accounts", 0, (), ()
                )
            identities, fingerprints = derive_account_identities(account_list)
            _g2b_plan_arm_account_holdings(ops_kernel, tick.tick_id, count, now)
            facts = []
            for index, seq in enumerate(seqs):
                account_ordinal = index + 1
                ledger.admit_account_holdings()
                ops_kernel.start_request(
                    ProducerRole.COLLECTOR,
                    tick.tick_id,
                    "HOLDINGS",
                    account_ordinal + 1,
                    invocation_id=f"{tick.tick_id:032x}",
                    now=now,
                )
                held = data_transport(
                    build_holdings_request(token=token, account_seq=seq)
                )
                if not isinstance(held, _TossTransportSuccess):
                    _g2b_settle(
                        ops_kernel,
                        tick.tick_id,
                        "HOLDINGS",
                        account_ordinal + 1,
                        _g2b_exit_for_code(getattr(held, "code", "")),
                        now,
                    )
                    return TossCampaignOutcome(
                        getattr(held, "code", "holdings_transport_failure"),
                        "holdings",
                        0,
                        (),
                        (),
                    )
                fact = parse_holdings_response(
                    held.body,
                    account_ordinal=account_ordinal,
                    account_count=count,
                    captured_at=now,
                )
                if not isinstance(fact, TossAccountHoldingsFact):
                    _g2b_settle(
                        ops_kernel,
                        tick.tick_id,
                        "HOLDINGS",
                        account_ordinal + 1,
                        "PARSE_FAIL",
                        now,
                    )
                    return TossCampaignOutcome("holdings_parse", "holdings", 0, (), ())
                _g2b_settle(
                    ops_kernel, tick.tick_id, "HOLDINGS", account_ordinal + 1, "OK", now
                )
                facts.append(fact)
                seq_by_scope[identities[index].account_scope] = seq
            return TossCampaignOutcome(
                "success",
                "holdings",
                count,
                tuple(facts),
                (),
                account_identities=identities,
                _identity_fingerprints=fingerprints,
            )

        return provider

    def quotes_provider_factory(gate):
        gate_state["gate"] = gate

        def provider(symbols):
            batch = tuple(symbols)
            now = _aware_utc(clock())
            tick = _pinned_tick(now)
            _begin_tick(tick)
            token = _campaign_token(gate, now)
            ops_kernel = gate.ops_kernel
            ledger.admit_quote_batch()
            ordinal = quote_ordinal["n"]
            quote_ordinal["n"] += 1
            params = {
                "market": "KR",
                "sorted_unique_symbols": tuple(sorted(set(batch))),
            }
            _g2b_plan_start(
                ops_kernel,
                tick.tick_id,
                "QUOTES",
                ordinal=ordinal,
                operation="PRICE_BATCH",
                params=params,
                account_ordinal=None,
                now=now,
            )
            outcome = query_current_prices(
                symbols=batch,
                token=token,
                transport=prices_transport,
                clock=lambda: now,
            )
            _g2b_settle(
                ops_kernel,
                tick.tick_id,
                "QUOTES",
                ordinal,
                "OK" if outcome.code == "success" else _g2b_exit_for_code(outcome.code),
                now,
            )
            return outcome

        return provider

    def orders_provider_factory(gate):
        gate_state["gate"] = gate

        def provider(*, account_scope, status, from_date, to_date, cursor, limit):
            now = _aware_utc(clock())
            token = _campaign_token(gate, now)
            seq = seq_by_scope.get(account_scope)
            if seq is None:
                return TossReadOutcome("account_unmapped")
            ledger.admit_trade()
            request = build_read_request(
                TossReadOperation.ORDERS,
                token=token,
                account_seq=seq,
                status=status,
                cursor=cursor,
                limit=limit,
                **{"from": from_date, "to": to_date},
            )
            return query_read(request, transport=reads_transport, clock=lambda: now)

        return provider

    def detail_provider_factory(gate):
        gate_state["gate"] = gate

        def provider(*, account_scope, order_id):
            now = _aware_utc(clock())
            tick = _pinned_tick(now)
            _begin_tick(tick)
            token = _campaign_token(gate, now)
            ops_kernel = gate.ops_kernel
            seq = seq_by_scope.get(account_scope)
            if seq is None:
                return None
            ledger.admit_trade()
            ordinal = detail_ordinal["n"]
            detail_ordinal["n"] += 1
            params = {"account_scope": account_scope, "order_id": order_id}
            _g2b_plan_start(
                ops_kernel,
                tick.tick_id,
                "TRADES",
                ordinal=ordinal,
                operation="ORDER_DETAIL",
                params=params,
                account_ordinal=None,
                now=now,
            )
            request = build_read_request(
                TossReadOperation.ORDER_DETAIL,
                token=token,
                account_seq=seq,
                order_id=order_id,
            )
            outcome = query_read(request, transport=reads_transport, clock=lambda: now)
            _g2b_settle(
                ops_kernel,
                tick.tick_id,
                "TRADES",
                ordinal,
                "OK" if outcome.code == "success" else _g2b_exit_for_code(outcome.code),
                now,
            )
            return outcome

        return provider

    session_dates = _G2BCalendarSessionDates(
        token_acquire=_campaign_token,
        reads_transport=reads_transport,
        clock=clock,
        gate_state=gate_state,
        calendar_attempts=calendar_attempts,
        pace_seconds=0.3 if https_handler_factory is None else 0.0,
    )

    deps_clock = lambda: _aware_utc(clock())
    # Masked per-tick numeric evidence (§10.9): the analysis stage's explicit
    # metric lands here, keyed by tick id, and the runner aggregates it into
    # the report — never the GLM projection body.
    numeric_evidence: dict[int, dict] = {}
    deps = IntegratedCadenceDepsV1(
        calendar_provider=calendar_provider,
        holdings_provider_factory=holdings_provider_factory,
        quotes_provider_factory=quotes_provider_factory,
        orders_provider_factory=orders_provider_factory,
        detail_provider_factory=detail_provider_factory,
        glm_call=_G2DInjectedModelCall(glm_call, deps_clock),
        sol_call=_G2DInjectedModelCall(sol_call, deps_clock),
        regular_session_dates=session_dates,
        clock=deps_clock,
        numeric_evidence_sink=lambda payload: numeric_evidence.__setitem__(
            payload["tick_id"], payload
        ),
    )
    # Frozen-dataclass side-channels: the bounded acquirer for the focused
    # token test, the collected numeric evidence and the latched token-loss
    # flag for the runner's report.  Nothing in the stage path reads them.
    object.__setattr__(deps, "_g2d_token_acquire", _campaign_token)
    object.__setattr__(deps, "_g2d_numeric_evidence", numeric_evidence)
    object.__setattr__(deps, "_g2d_token_lost", lambda: bool(token_state.get("lost")))
    return deps


def g2d_scripted_glm_call(body: bytes) -> bytes:
    """Production-safe deterministic scripted GLM stub for the G2-D pilot.

    It is pure local computation over the projected prompt body — no socket,
    no live provider import, no state: every candidate row is screened to
    relevance NONE, so the SOL stage receives nothing model-selected.  This is
    the only GLM supply the ``g2d`` CLI ever wires (§10.7, real model calls 0).
    """
    rows = json.loads(body)["candidates"]
    return json.dumps(
        {
            "schema_version": 1,
            "annotations": [
                {
                    "candidate_id": row["candidate_id"],
                    "relevance": "NONE",
                    "materiality": "LOW",
                    "urgency": "NONE",
                    "evidence_unknowns": [],
                    "summary": "g2d-scripted-screen",
                }
                for row in rows
            ],
        },
        separators=(",", ":"),
    ).encode()


def g2d_scripted_sol_call(body: bytes) -> bytes:
    """Production-safe deterministic scripted SOL stub for the G2-D pilot:
    a pure local empty-analyses reply, so no analysis is ever fabricated."""
    del body
    return json.dumps(
        {"schema_version": 1, "analyses": []}, separators=(",", ":")
    ).encode()


def log_reason(code: str) -> str:
    """Campaign codes carrying a scheme name are renamed for the event log,
    whose value filter rejects credential and URL vocabulary outright."""
    return _TOSS_LOG_REASONS.get(code, code)


def _log_toss(logger: TracerLogger, run_id: str, action: str, code: str) -> None:
    """An observability failure must not mask a completed campaign."""
    with contextlib.suppress(Exception):
        logger.info(
            run_id=run_id,
            stage="capture",
            action=action,
            reason=log_reason(code),
            source=TOSS_SOURCE,
            decision_id=None,
        )


def _first_unsaved(results):
    """Conflicts and write failures outrank a duplicate when reporting why a
    batch did not apply."""
    severe = [
        result
        for result in results
        if result.status in (SaveStatus.CONFLICT, SaveStatus.FAILED)
    ]
    return (
        severe
        or [result for result in results if result.status is not SaveStatus.SAVED]
    )[0]


def _empty_toss_state() -> dict:
    return {
        "campaign_index": 0,
        "starts": [],
        "total_started": 0,
        "last_outcome": "never_started",
        "last_captured_at": None,
        "field_presence": None,
        "rate_limit_remaining": None,
        "retry_after_seconds": TOSS_MIN_START_INTERVAL_SECONDS,
    }


def _read_toss_state(path: Path) -> dict:
    if path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("invalid_state")
    raw = path.read_bytes()
    if len(raw) > 65_536:
        raise ValueError("invalid_state")
    state = json.loads(raw)
    if not isinstance(state, dict) or set(state) != set(_empty_toss_state()):
        raise ValueError("invalid_state")
    starts = state["starts"]
    if not isinstance(starts, list) or len(starts) > TOSS_MAX_STARTS:
        raise ValueError("invalid_state")
    for index, start in enumerate(starts):
        if not isinstance(start, dict) or set(start) != _TOSS_START_KEYS:
            raise ValueError("invalid_state")
        if start["start_index"] != index + 1 or start["stage"] not in _TOSS_STAGES:
            raise ValueError("invalid_state")
        ordinal = start["account_ordinal"]
        if start["stage"] == "holdings":
            if type(ordinal) is not int or not 1 <= ordinal <= TOSS_MAX_ACCOUNTS:
                raise ValueError("invalid_state")
        elif ordinal is not None:
            raise ValueError("invalid_state")
        if not isinstance(start["outcome"], str) or not start["outcome"]:
            raise ValueError("invalid_state")
        _parse_utc(start["planned_at"])
    for key in ("campaign_index", "total_started"):
        if type(state[key]) is not int or state[key] < 0:
            raise ValueError("invalid_state")
    if (
        not isinstance(state["last_outcome"], str)
        or not state["last_outcome"]
        or type(state["retry_after_seconds"]) is not int
        or not (
            TOSS_MIN_START_INTERVAL_SECONDS
            <= state["retry_after_seconds"]
            <= TOSS_MAX_RETRY_AFTER
        )
    ):
        raise ValueError("invalid_state")
    remaining = state["rate_limit_remaining"]
    if remaining is not None and (type(remaining) is not int or remaining < 0):
        raise ValueError("invalid_state")
    if state["last_captured_at"] is not None:
        _parse_utc(state["last_captured_at"])
    _validate_toss_field_presence(state["field_presence"])
    return state


def _validate_toss_field_presence(presence) -> None:
    """Provider field names and item counts only; the closed key set means a
    quantity or a price could never round-trip through the ledger."""
    if presence is None:
        return
    if not isinstance(presence, dict) or set(presence) != {
        "avg_price_fields",
        "market_fields",
        "item_counts",
    }:
        raise ValueError("invalid_state")
    for key in ("avg_price_fields", "market_fields"):
        names = presence[key]
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name for name in names
        ):
            raise ValueError("invalid_state")
    counts = presence["item_counts"]
    if not isinstance(counts, list) or not all(
        type(count) is int and count >= 0 for count in counts
    ):
        raise ValueError("invalid_state")


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", closefd=False) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _live_dirs(home: Path) -> tuple[Path, Path]:
    # Resolved: both roots may be owner-owned symlinks into the project (relocated 2026-09-24)
    # and strict stores refuse symlinked ancestors.
    return (home / ".config" / "tokmo-v3").resolve(), (
        home / ".local" / "share" / "tokmo-v3"
    ).resolve()


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _require_private_credential_file(path: Path) -> None:
    """Existence and mode only — the file is never opened here.

    A symlink, a directory or other non-regular file, and any mode wider than
    owner read/write all fail closed before the loader ever sees the path.
    """
    if path.is_symlink() or not path.is_file():
        raise ValueError("config_mode")
    if (path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("config_mode")


def _require_private_owned_dir(path: Path) -> None:
    """Owner-only directory. The data/config roots may be owner-owned symlinks into the
    project (relocated 2026-09-24); the link itself and its target must both belong to the
    caller and the target must be a 0700 directory. File-level guards still refuse symlinks."""
    if path.is_symlink() and path.lstat().st_uid != os.getuid():
        raise LiveRunError("private_state_invalid")
    real = path.resolve()
    if (
        not real.is_dir()
        or real.is_symlink()
        or (real.stat().st_mode & 0o777) != 0o700
        or real.stat().st_uid != os.getuid()
    ):
        raise LiveRunError("private_state_invalid")


def _empty_state() -> dict:
    return {
        "total_started": 0,
        "last_started_at": None,
        "success": False,
        "last_outcome": "never_started",
        "retry_after_seconds": 5,
    }


def _empty_sec_state() -> dict:
    return {
        "total_started": 0,
        "last_started_at": None,
        "last_endpoint": None,
        "last_purpose": None,
        "last_outcome": "never_started",
        "aapl_cik": None,
        "aapl_company_name": None,
        "submissions_confirmed": False,
    }


def _empty_dart_state() -> dict:
    return {
        "probes": [],
        "checkpoints": [],
        "corp_code": None,
        "corp_name": None,
        "modify_date": None,
        "list_confirmed": False,
        "retry_after_seconds": DART_MIN_START_INTERVAL_SECONDS,
    }


def _empty_krx_state() -> dict:
    return {
        "total_started": 0,
        "last_started_at": None,
        "requested_base_date": None,
        "last_outcome": "never_started",
    }


def _read_krx_state(path: Path) -> dict:
    if path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("invalid_state")
    raw = path.read_bytes()
    if len(raw) > 16_384:
        raise ValueError("invalid_state")
    state = json.loads(raw)
    if not isinstance(state, dict) or set(state) != set(_empty_krx_state()):
        raise ValueError("invalid_state")
    if (
        type(state["total_started"]) is not int
        or not 0 <= state["total_started"] <= KRX_MAX_STARTS
    ):
        raise ValueError("invalid_state")
    if not isinstance(state["last_outcome"], str) or not state["last_outcome"]:
        raise ValueError("invalid_state")
    if state["last_started_at"] is None:
        if state["total_started"] != 0 or state["requested_base_date"] is not None:
            raise ValueError("invalid_state")
    else:
        _parse_utc(state["last_started_at"])
        datetime.strptime(state["requested_base_date"], "%Y%m%d")
    return state


def _dart_gate_open(state: dict, tranche: int) -> bool:
    return any(
        checkpoint["tranche"] == tranche
        and checkpoint["tests_green"] is True
        and checkpoint["no_scope_expansion"] is True
        for checkpoint in state["checkpoints"]
    )


def _read_dart_state(path: Path) -> dict:
    if path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("invalid_state")
    raw = path.read_bytes()
    if len(raw) > 65_536:
        raise ValueError("invalid_state")
    state = json.loads(raw)
    if not isinstance(state, dict) or set(state) != set(_empty_dart_state()):
        raise ValueError("invalid_state")
    probes = state["probes"]
    if not isinstance(probes, list) or len(probes) > DART_MAX_STARTS:
        raise ValueError("invalid_state")
    for index, probe in enumerate(probes):
        if not isinstance(probe, dict) or set(probe) != _DART_PROBE_KEYS:
            raise ValueError("invalid_state")
        if probe["sequence"] != index + 1:
            raise ValueError("invalid_state")
        for key in ("endpoint_id", "purpose", "outcome"):
            if not isinstance(probe[key], str) or not probe[key]:
                raise ValueError("invalid_state")
        _parse_utc(probe["started_at"])
    _validate_dart_checkpoints(state["checkpoints"])
    if (
        type(state["list_confirmed"]) is not bool
        or type(state["retry_after_seconds"]) is not int
        or not (
            DART_MIN_START_INTERVAL_SECONDS
            <= state["retry_after_seconds"]
            <= DART_MAX_RETRY_AFTER
        )
    ):
        raise ValueError("invalid_state")
    corp_code = state["corp_code"]
    if corp_code is not None and (
        not isinstance(corp_code, str)
        or len(corp_code) != 8
        or not corp_code.isascii()
        or not corp_code.isdigit()
    ):
        raise ValueError("invalid_state")
    corp_name = state["corp_name"]
    if corp_name is not None and (not isinstance(corp_name, str) or not corp_name):
        raise ValueError("invalid_state")
    modify_date = state["modify_date"]
    if modify_date is not None:
        if not isinstance(modify_date, str):
            raise ValueError("invalid_state")
        date.fromisoformat(modify_date)
    if len({corp_code is None, corp_name is None, modify_date is None}) != 1:
        raise ValueError("invalid_state")
    return state


def _validate_dart_checkpoints(checkpoints) -> None:
    """At most one checkpoint per tranche, recorded in order (2 then 3)."""
    if not isinstance(checkpoints, list):
        raise ValueError("invalid_state")
    tranches = []
    for entry in checkpoints:
        if not isinstance(entry, dict) or set(entry) != _DART_CHECKPOINT_KEYS:
            raise ValueError("invalid_state")
        if type(entry["tranche"]) is not int or entry["tranche"] not in (2, 3):
            raise ValueError("invalid_state")
        for key in ("confirmed", "changed", "unresolved", "why_more_calls"):
            if not isinstance(entry[key], str) or not entry[key]:
                raise ValueError("invalid_state")
        for key in ("tests_green", "no_scope_expansion"):
            if type(entry[key]) is not bool:
                raise ValueError("invalid_state")
        info = entry["new_information"]
        if entry["tranche"] == 3:
            if not isinstance(info, str) or not info:
                raise ValueError("invalid_state")
        elif info is not None:
            raise ValueError("invalid_state")
        _parse_utc(entry["recorded_at"])
        tranches.append(entry["tranche"])
    if tranches not in ([], [2], [2, 3]):
        raise ValueError("invalid_state")


def _read_sec_state(path: Path) -> dict:
    if path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("invalid_state")
    raw = path.read_bytes()
    if len(raw) > 16_384:
        raise ValueError("invalid_state")
    state = json.loads(raw)
    if not isinstance(state, dict) or set(state) != set(_empty_sec_state()):
        raise ValueError("invalid_state")
    if (
        type(state["total_started"]) is not int
        or not 0 <= state["total_started"] <= SEC_MAX_STARTS
        or not isinstance(state["last_outcome"], str)
        or not state["last_outcome"]
        or type(state["submissions_confirmed"]) is not bool
    ):
        raise ValueError("invalid_state")
    for key in ("last_endpoint", "last_purpose", "aapl_company_name"):
        if state[key] is not None and (
            not isinstance(state[key], str) or not state[key]
        ):
            raise ValueError("invalid_state")
    cik = state["aapl_cik"]
    if cik is not None and (
        not isinstance(cik, str) or len(cik) != 10 or not cik.isdigit()
    ):
        raise ValueError("invalid_state")
    if (cik is None) != (state["aapl_company_name"] is None):
        raise ValueError("invalid_state")
    if state["last_started_at"] is None:
        if state["total_started"] != 0:
            raise ValueError("invalid_state")
    else:
        _parse_utc(state["last_started_at"])
    return state


def _read_state(path: Path) -> dict:
    if path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
        raise ValueError("invalid_state")
    raw = path.read_bytes()
    if len(raw) > 16_384:
        raise ValueError("invalid_state")
    state = json.loads(raw)
    if set(state) != set(_empty_state()):
        raise ValueError("invalid_state")
    if (
        type(state["total_started"]) is not int
        or not 0 <= state["total_started"] <= MAX_ATTEMPTS
        or type(state["success"]) is not bool
        or not isinstance(state["last_outcome"], str)
        or not isinstance(state["retry_after_seconds"], int)
        or not 5 <= state["retry_after_seconds"] <= MAX_RETRY_AFTER
    ):
        raise ValueError("invalid_state")
    if state["last_started_at"] is None:
        if state["total_started"] != 0:
            raise ValueError("invalid_state")
    else:
        _parse_utc(state["last_started_at"])
    return state


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock")
    return value.astimezone(UTC)


def _parse_utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp")
    return value


def build_production_cadence_deps(
    *,
    project_root: Path,
    clock: Clock,
    shadow_root: Path,
    https_handler_factory=None,
    lifecycle_sink=None,
    failure_sink=None,
    mode=CADENCE_MODE_LIVE_SHADOW,
    pension_composition_enabled=False,
    pension_reminder_send_enabled=False,
    pension_unquotable_symbols=frozenset(),
    telegram_send=None,
) -> ProductionCadenceDepsV1:
    """Real read-only live-shadow deps from the official Toss adapters.

    Construction performs zero provider calls: transports are built lazily
    against the injected handler factory and the only I/O is reading the
    existing ``.env.toss`` credential file (never logged). Every per-wire
    provider validates its executor-issued claim before touching a
    transport, so no wire can run outside the kernel-admitted executor.

    The trades session-date source is the shadow root's durable official
    regular-session history (M2): it reads only facts the cadence itself
    accumulated through admitted calendar wires, never a raw transport or a
    weekday guess, and an insufficient history is a truthful
    ``failed_calendar``.
    """

    def _handler():
        return None if https_handler_factory is None else https_handler_factory()

    auth_transport = TossAuthLiveTransport(https_handler=_handler())
    data_transport = TossDataLiveTransport(https_handler=_handler())
    prices_transport = TossPricesLiveTransport(https_handler=_handler())
    reads_transport = TossReadsLiveTransport(https_handler=_handler())
    config = load_toss_config(project_root / TOSS_ENV_NAME)
    watchlist_symbols = load_watchlists(project_root / "watchlists")

    def now() -> datetime:
        return _aware_utc(clock())

    membership_transport = OfficialMembershipLiveTransport(clock=now)

    def sec_user_agent():
        return load_sec_config(
            Path.home() / ".config" / "tokmo-v3" / "sec.env"
        ).user_agent

    def membership_fetch(*, claim, identity):
        if not isinstance(identity, RecurringMembershipRequestV1):
            raise ValueError("membership request identity")
        require_started_claim(claim, claim.operation)
        live_identity = OfficialMembershipRequestV1(
            EndpointKindV1(identity.endpoint_kind.value), identity.cik
        )
        return membership_transport.fetch(
            live_identity, sec_user_agent=sec_user_agent()
        )

    session_dates = HistorySessionDatesV1(
        RegularSessionHistoryStore(
            Path(shadow_root).resolve() / "ops" / "regular_session_history.sqlite3",
            clock=now,
        )
    )

    def token_fetch(claim):
        require_started_claim(claim, "TOKEN")
        outcome = auth_transport(
            build_auth_request(
                client_id=config.client_id, client_secret=config.client_secret
            )
        )
        if not isinstance(outcome, _TossTransportSuccess):
            raise RuntimeError("token_transport_failed")
        token = parse_token_response(outcome.body, observed_at=now())
        if not isinstance(token, TossAccessToken):
            raise RuntimeError("token_parse_failed")
        return token.value

    def calendar_fetch(*, market, local_trade_date, receipt, bearer):
        # Calendar admission is the kernel's one-time receipt claim, consumed
        # by the refresh program before this fetcher runs.
        del receipt
        operation = (
            TossReadOperation.KR_MARKET_CALENDAR
            if market is Market.KR
            else TossReadOperation.US_MARKET_CALENDAR
        )
        request = build_read_request(
            operation, token=bearer, date=local_trade_date.isoformat()
        )
        outcome = reads_transport(request)
        if not isinstance(outcome, _TossTransportSuccess):
            raise RuntimeError("calendar_transport_failed")
        response = parse_envelope(outcome.body, operation.value)
        if not isinstance(
            response, (KrMarketCalendarResponse, UsMarketCalendarResponse)
        ):
            raise RuntimeError("calendar_parse_failed")
        # G2.5: return the full response so the caller can preserve
        # next_business_day for the lookahead writer. The refresh program
        # extracts .today; the lookahead writer reads .next_business_day.
        return response

    def accounts_fetch(*, claim, bearer):
        require_started_claim(claim, "ACCOUNTS")
        reply = data_transport(build_accounts_request(token=bearer))
        if not isinstance(reply, _TossTransportSuccess):
            if failure_sink is not None:
                code = getattr(reply, "code", "provider_other")
                category = (
                    code if code in TOSS_TRANSPORT_FAILURE_CODES else "provider_other"
                )
                failure_sink("ACCOUNTS", category)
            raise RuntimeError("accounts_transport_failed")
        account_list = parse_accounts_response(reply.body, observed_at=now())
        if not isinstance(account_list, TossAccountList):
            if failure_sink is not None:
                failure_sink("ACCOUNTS", "parse_failed")
            raise RuntimeError("accounts_parse_failed")
        identities, fingerprints = derive_account_identities(account_list)
        return CadenceAccountsV1(
            tuple(identities),
            {
                identity.account_scope: seq
                for identity, seq in zip(identities, account_list.seqs)
            },
            dict(fingerprints),
        )

    def account_holdings_fetch(*, claim, bearer, identity, native_ref, ordinal, count):
        require_started_claim(claim, "ACCOUNT_HOLDINGS")
        del identity
        held = data_transport(
            build_holdings_request(token=bearer, account_seq=native_ref)
        )
        if not isinstance(held, _TossTransportSuccess):
            raise RuntimeError("holdings_transport_failed")
        fact = parse_holdings_response(
            held.body, account_ordinal=ordinal, account_count=count, captured_at=now()
        )
        if not isinstance(fact, TossAccountHoldingsFact):
            raise RuntimeError("holdings_parse_failed")
        return fact

    def quotes_fetch(*, claim, bearer, symbols):
        require_started_claim(claim, "PRICE_BATCH")
        return query_current_prices(
            symbols=tuple(symbols), token=bearer, transport=prices_transport, clock=now
        )

    def orders_page_fetch(
        *,
        claim,
        bearer,
        account_scope,
        native_ref,
        status,
        from_date,
        to_date,
        cursor,
        limit,
    ):
        require_started_claim(claim, "ORDERS_PAGE")
        del account_scope
        if native_ref is None:
            return TossReadOutcome("account_unmapped")
        request = build_read_request(
            TossReadOperation.ORDERS,
            token=bearer,
            account_seq=native_ref,
            status=status,
            cursor=cursor,
            limit=limit,
            **{"from": from_date, "to": to_date},
        )
        return query_read(request, transport=reads_transport, clock=now)

    def order_detail_fetch(*, claim, bearer, account_scope, native_ref, order_id):
        require_started_claim(claim, "ORDER_DETAIL")
        del account_scope
        if native_ref is None:
            return TossReadOutcome("account_unmapped")
        request = build_read_request(
            TossReadOperation.ORDER_DETAIL,
            token=bearer,
            account_seq=native_ref,
            order_id=order_id,
        )
        return query_read(request, transport=reads_transport, clock=now)

    def stock_candles_fetch(*, claim, bearer, symbol, before, adjusted):
        require_started_claim(claim, "STOCK_CANDLES_DAILY")
        request = build_read_request(
            TossReadOperation.STOCK_CANDLES,
            token=bearer,
            symbol=symbol,
            interval="1d",
            count=200,
            before=before,
            adjusted=adjusted,
        )
        return query_read(request, transport=reads_transport, clock=now)

    def stocks_membership_fetch(*, claim, bearer, symbols):
        # KR profile membership (Plan/SDS 2026-08-22 §5.1): exactly one
        # authenticated STOCKS batch read through the existing reads
        # transport; composed only here, claim-gated before any transport.
        require_started_claim(claim, "MEMBERSHIP_TOSS_STOCKS")
        request = build_read_request(
            TossReadOperation.STOCKS,
            token=bearer,
            symbols=tuple(symbols),
        )
        return query_read(request, transport=reads_transport, clock=now)

    deps = ProductionCadenceDepsV1(
        mode=mode,
        clock=now,
        token_fetch=token_fetch,
        calendar_fetch=calendar_fetch,
        accounts_fetch=accounts_fetch,
        account_holdings_fetch=account_holdings_fetch,
        quotes_fetch=quotes_fetch,
        orders_page_fetch=orders_page_fetch,
        order_detail_fetch=order_detail_fetch,
        stock_candles_fetch=stock_candles_fetch,
        regular_session_dates=session_dates,
        lifecycle_sink=(
            null_lifecycle_sink if lifecycle_sink is None else lifecycle_sink
        ),
        watchlist_symbols=watchlist_symbols,
        pension_registry_path=(
            shadow_root / "stores" / "pension_registry.sqlite3"
            if pension_composition_enabled
            else None
        ),
        pension_composition_enabled=pension_composition_enabled,
        pension_reminder_send_enabled=pension_reminder_send_enabled,
        pension_unquotable_symbols=frozenset(pension_unquotable_symbols),
        membership_fetch=membership_fetch,
        sec_user_agent=sec_user_agent,
        sic_snapshot_loader=load_production_sic_snapshot,
        stocks_membership_fetch=stocks_membership_fetch,
    )
    if pension_reminder_send_enabled:
        if not callable(telegram_send):
            raise ValueError("telegram sender required when reminder delivery enabled")
        object.__setattr__(deps, "telegram_send", telegram_send)
    return deps


def _cadence_summary(result) -> dict:
    def _slot(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    return {
        "outcome": result.outcome.value,
        "reason": result.reason,
        "exit_code": result.exit_code,
        "policy_version": result.policy_version,
        "scheduled_for_utc": _slot(result.scheduled_for_utc),
        "tick_id": result.tick_id,
        "lag_seconds": result.lag_seconds,
        "mask_digest": result.mask_digest,
        "open_markets": list(result.open_markets),
        "local_dates": dict(result.local_dates),
        "calendar": dict(result.calendar),
        "stages": [
            {
                "stage": status.stage,
                "status": status.status,
                "reason": status.reason,
                "latency_ms": status.latency_ms,
            }
            for status in result.stages
        ],
        "wire": {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in dict(result.wire).items()
        },
    }


def cadence_main(argv: list[str] | None = None) -> int:
    """`tokmo-cadence`: exactly one aligned shadow or production tick.

    stdout carries one machine-readable JSON summary line; stderr carries
    logs. Exit 0 is a truthful COMPLETE/SKIPPED_CLOSED, 1 is a
    configuration/preflight refusal before provider work, 2 is every
    duplicate/missed/overlap/partial/failed terminal. No secret is ever
    printed or serialized.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="tokmo-cadence", add_help=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--mode", default="live-shadow")
    parser.add_argument("--policy-version", default="cadence-300s-v1")
    parser.add_argument("--scheduled-for", "--scheduled-for-utc", dest="scheduled_for")
    parser.add_argument("--observed-now")
    parser.add_argument("--root")
    parser.add_argument("--fixture")
    parser.add_argument("--replay", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    def refuse(reason: str) -> int:
        print(f"tokmo-cadence refusal={reason}", file=sys.stderr)
        return 1

    if not args.once:
        return refuse("once_required")
    if args.mode not in ("live-shadow", "production"):
        return refuse("unsupported_mode")
    if not args.scheduled_for or not args.root:
        return refuse("scheduled_for_and_root_required")
    if args.mode == "production":
        if args.fixture is not None:
            return refuse("production_mode_forbids_fixture")
        if args.replay:
            return refuse("production_mode_forbids_replay")
        if args.observed_now is not None:
            return refuse("production_mode_forbids_observed_now")
    try:
        scheduled = _parse_utc(args.scheduled_for.replace("Z", "+00:00"))
    except ValueError:
        return refuse("invalid_scheduled_for")
    if args.observed_now is None:
        observed = _aware_utc(utc_now())
    else:
        try:
            observed = _parse_utc(args.observed_now.replace("Z", "+00:00"))
        except ValueError:
            return refuse("invalid_observed_now")

    if args.fixture is not None:
        try:
            fixture = json.loads(Path(args.fixture).read_text())
        except (OSError, ValueError):
            return refuse("fixture_unreadable")
        deps, _probe = build_fixture_cadence_deps(
            fixture=fixture,
            clock=lambda: observed,
            failure_sink=lambda operation, category: print(
                f"provider_failure operation={operation} category={category}",
                file=sys.stderr,
            ),
        )
    else:
        try:
            deps = build_production_cadence_deps(
                project_root=PROJECT_ROOT,
                clock=utc_now,
                shadow_root=Path(args.root),
                mode=(
                    CADENCE_MODE_PRODUCTION
                    if args.mode == "production"
                    else CADENCE_MODE_LIVE_SHADOW
                ),
                # Re-enabled 2026-08-05 after the real root cause was fixed:
                # two wrong KRX short codes in _PENSION_SYMBOL_MAP
                # (442300→0204D0, 457540→269540) made the Toss price API return
                # contract_mismatch. Corrected + re-migrated; all 7 pension
                # symbols now quote. 069500 (held in both PERSONAL and PENSION)
                # is deduped naturally by the set-based quote target selection,
                # so pension_unquotable_symbols stays empty.
                pension_composition_enabled=True,
                failure_sink=lambda operation, category: print(
                    f"provider_failure operation={operation} category={category}",
                    file=sys.stderr,
                ),
            )
        except ConfigError:
            return refuse("credential_preflight_failed")
        except WatchlistError as error:
            detail = str(error)
            if not detail or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in detail
            ):
                detail = "invalid"
            return refuse(f"watchlist_preflight_failed_{detail}")
        except OSError:
            return refuse("production_preflight_io_failed")
        except ValueError:
            return refuse("production_preflight_validation_failed")

    try:
        result = run_production_cadence_once(
            root=Path(args.root),
            policy_version=args.policy_version,
            scheduled_for_utc=scheduled,
            observed_now_utc=observed,
            deps=deps,
            replay=args.replay,
        )
    except ProductionCadenceRefusal as error:
        return refuse(str(error).replace(" ", "_"))
    except Exception as error:
        # Masked failure: the type name only, never a message or value.
        masked = type(error).__name__
        print(f"tokmo-cadence failure={masked}", file=sys.stderr)
        print(
            json.dumps(
                {"outcome": "FAILED", "reason": f"exception_{masked}", "exit_code": 2},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(_cadence_summary(result), sort_keys=True))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    home, project_root = Path.home(), PROJECT_ROOT
    while len(args) >= 3 and args[-2] in ("--home", "--project-root"):
        if args[-2] == "--home":
            home = Path(args[-1])
        else:
            project_root = Path(args[-1])
        args = args[:-2]
    if args == ["sec-setup"]:
        try:
            setup_sec(home=home)
        except (OSError, ValueError):
            print("sec-tracer-1a setup=private_state_invalid")
            return 1
        print("sec-tracer-1a setup=complete")
        return 0
    if len(args) == 2 and args[0] == "sec-probe" and args[1] in SEC_PROBES:
        try:
            result = run_sec(
                home=home,
                endpoint=args[1],
                transport=SecLiveTransport(),
                clock=utc_now,
                id_factory=lambda: uuid.uuid4().hex,
                run_id=f"sec-{uuid.uuid4().hex[:12]}",
            )
        except LiveRunError as error:
            print(f"sec-tracer-1a endpoint={args[1]} result={error.code}")
            return 3 if error.code == "not_due" else 1
        if result.filing is None:
            print(
                f"sec-tracer-1a endpoint={result.endpoint} result=success"
                f" started={result.started} cik={result.cik}"
                f" company={result.company_name}"
            )
        else:
            filing = result.filing
            report = filing.report_date.isoformat() if filing.report_date else "none"
            print(
                f"sec-tracer-1a endpoint={result.endpoint} result=success"
                f" started={result.started} action={result.decision_action}"
                f" reason={result.decision_reason}"
                f" accession={filing.accession_number} form={filing.form}"
                f" filed={filing.filing_date.isoformat()} report={report}"
                f" document={filing.primary_document} xbrl={filing.is_xbrl}"
            )
        return 0
    if args == ["dart-setup"]:
        try:
            setup_dart(home=home)
        except (OSError, ValueError):
            print("opendart-tracer-1a setup=private_state_invalid")
            return 1
        print("opendart-tracer-1a setup=complete")
        return 0
    if args == ["krx-setup"]:
        try:
            setup_krx(home=home)
        except (OSError, ValueError, TypeError):
            print("krx-tracer setup=private_state_invalid")
            return 1
        print("krx-tracer setup=complete")
        return 0
    if len(args) == 4 and args[:2] == ["krx-probe", "daily"] and args[2] == "--date":
        try:
            result = run_krx(
                home=home,
                base_date=args[3],
                transport=KrxLiveTransport(),
                clock=utc_now,
                id_factory=lambda: uuid.uuid4().hex,
                run_id=f"krx-{uuid.uuid4().hex[:12]}",
            )
        except LiveRunError as error:
            print(f"krx-tracer endpoint=daily result={error.code}")
            return 3 if error.code == "not_due" else 1
        print(
            f"krx-tracer endpoint=daily result=success started={result.started}"
            f" action={result.decision_action} reason={result.decision_reason}"
            f" date={result.fact.base_date.isoformat()} market={result.fact.market_name}"
            f" issue_code={result.fact.issue_code}"
        )
        return 0
    if len(args) == 2 and args[0] == "dart-probe" and args[1] in DART_PROBES:
        try:
            result = run_dart(
                home=home,
                endpoint=args[1],
                transport=OpenDartLiveTransport(),
                clock=utc_now,
                id_factory=lambda: uuid.uuid4().hex,
                run_id=f"dart-{uuid.uuid4().hex[:12]}",
            )
        except LiveRunError as error:
            print(f"opendart-tracer-1a endpoint={args[1]} result={error.code}")
            return 3 if error.code == "not_due" else 1
        if result.disclosure is None:
            print(
                f"opendart-tracer-1a endpoint={result.endpoint} result=success"
                f" started={result.started} corp_code={result.corp_code}"
                f" corp_name={result.corp_name}"
            )
        else:
            row = result.disclosure
            print(
                f"opendart-tracer-1a endpoint={result.endpoint} result=success"
                f" started={result.started} action={result.decision_action}"
                f" reason={result.decision_reason} corp_cls={row.corp_cls}"
                f" rcept_no={row.receipt_number}"
                f" rcept_dt={row.receipt_date.isoformat()}"
                f" report={row.report_name!r} filer={row.filer_name!r}"
                f" remark={row.remark!r}"
            )
        return 0
    if args == ["toss-setup"]:
        try:
            setup_toss(home=home, project_root=project_root)
        except (OSError, ValueError):
            print("toss-holdings setup=private_state_invalid")
            return 1
        print("toss-holdings setup=complete")
        return 0
    if args == ["toss-holdings"]:
        try:
            result = run_toss_routed(
                home=home,
                project_root=project_root,
                auth_transport=TossAuthLiveTransport(),
                data_transport=TossDataLiveTransport(),
                clock=utc_now,
                sleeper=sleep_seconds,
                id_factory=lambda: uuid.uuid4().hex,
                run_id=f"toss-{uuid.uuid4().hex[:12]}",
            )
        except LiveRunError as error:
            print(f"toss-holdings result={error.code}")
            return 1
        print(format_masked_result(result))
        return 0
    if args == ["toss-report"]:
        try:
            toss_report_routed(home=home)
        except LiveRunError as error:
            print(f"toss-holdings report={error.code}")
            return 1
        return 0
    if args == ["g2a", "--preflight"]:
        utc_day = _aware_utc(utc_now()).date()
        try:
            report = preflight(
                home=home,
                utc_day=utc_day,
                credential_path=project_root / TOSS_ENV_NAME,
                clock=lambda: _aware_utc(utc_now()),
                open_handle_probe=_lsof_open_handle_count,
            )
        except G2APreflightError as error:
            print(
                f"g2a preflight=refused reason={error.code} run_id={stable_run_id(utc_day)}"
            )
            return 1
        manifest = build_g2a_manifest(utc_day)
        print(
            f"g2a preflight=ok run_id={report.run_id}"
            f" ceiling_consuming_upper_bound={manifest.ceiling_consuming_upper_bound}"
            f" calendar_budget_upper_bound={manifest.calendar_budget_upper_bound}"
            f" auth_exception_upper_bound={manifest.auth_exception_upper_bound}"
            f" total_upper_bound={manifest.total_upper_bound}"
        )
        return 0
    if args == ["g2a", "--run-a-one-hour"]:
        utc_day = _aware_utc(utc_now()).date()
        try:
            deps = build_g2a_live_deps(
                project_root=project_root, clock=lambda: _aware_utc(utc_now())
            )
            report = run_a_one_hour(
                home=home,
                utc_day=utc_day,
                deps=deps,
                credential_path=project_root / TOSS_ENV_NAME,
                clock=lambda: _aware_utc(utc_now()),
                sleeper=sleep_seconds,
                open_handle_probe=_lsof_open_handle_count,
            )
        except G2APreflightError as error:
            print(f"g2a run=refused reason={error.code}")
            return 1
        except G2AStopError as error:
            print(
                f"g2a run=stopped reason={error.code} run_id={stable_run_id(utc_day)}"
            )
            return 2
        # Live socket/DNS are not instrumented on this path; report the truthful
        # wire-attempt count (durable started TOKEN/calendar rows) instead of a
        # fabricated measured zero.
        print(
            f"g2a run=complete run_id={report.run_id} ticks={report.tick_count}"
            f" provider_started={report.provider_started_count}"
            f" calendar_started={report.calendar_started_count}"
            f" wire_attempts={report.wire_attempt_count}"
        )
        return 0
    if args == ["g2b", "--run-b-canary"]:
        utc_day = _aware_utc(utc_now()).date()
        try:
            deps = build_g2b_live_deps(
                project_root=project_root, clock=lambda: _aware_utc(utc_now())
            )
            report = run_b_canary(
                home=home,
                utc_day=utc_day,
                deps=deps,
                credential_path=project_root / TOSS_ENV_NAME,
                clock=lambda: _aware_utc(utc_now()),
                open_handle_probe=_lsof_open_handle_count,
            )
        except G2APreflightError as error:
            print(f"g2b run=refused reason={error.code}")
            return 1
        except G2AStopError as error:
            print(
                f"g2b run=stopped reason={error.code} run_id={stable_run_id(utc_day)}"
            )
            return 2
        # Masked aggregate counts only; no symbol, quantity, account seq, or URL.
        print(
            f"g2b run=complete run_id={report.run_id} ticks={report.tick_count}"
            f" provider_started={report.provider_started_count}"
            f" token_started={report.token_started_count}"
            f" accounts={report.accounts_started_count}"
            f" holdings={report.account_holdings_started_count}"
            f" us_quote_batches={report.us_quote_batch_count}"
            f" kr_quotes={report.kr_quote_count}"
            f" orders_pages={report.orders_page_count}"
            f" order_details={report.order_detail_count}"
            f" calendar_started={report.calendar_started_count}"
            f" wire_attempts={report.wire_attempt_count}"
            f" production_ok={report.production_invariant_ok}"
        )
        return 0
    if args == ["g2b", "--run-b-one-hour"]:
        utc_day = _aware_utc(utc_now()).date()
        try:
            deps = build_g2b_live_deps(
                project_root=project_root, clock=lambda: _aware_utc(utc_now())
            )
            report = run_b_one_hour(
                home=home,
                utc_day=utc_day,
                deps=deps,
                credential_path=project_root / TOSS_ENV_NAME,
                clock=lambda: _aware_utc(utc_now()),
                sleeper=sleep_seconds,
                open_handle_probe=_lsof_open_handle_count,
            )
        except G2APreflightError as error:
            print(f"g2b run=refused reason={error.code}")
            return 1
        except G2AStopError as error:
            print(
                f"g2b run=stopped reason={error.code} run_id={stable_run_id(utc_day)}"
            )
            return 2
        # Masked aggregate counts only; no symbol, quantity, account seq, or URL.
        print(
            f"g2b run=complete run_id={report.run_id} ticks={report.tick_count}"
            f" provider_started={report.provider_started_count}"
            f" token_started={report.token_started_count}"
            f" accounts={report.accounts_started_count}"
            f" holdings={report.account_holdings_started_count}"
            f" us_quote_batches={report.us_quote_batch_count}"
            f" kr_quotes={report.kr_quote_count}"
            f" orders_pages={report.orders_page_count}"
            f" order_details={report.order_detail_count}"
            f" calendar_started={report.calendar_started_count}"
            f" wire_attempts={report.wire_attempt_count}"
            f" production_ok={report.production_invariant_ok}"
        )
        return 0
    if args == ["g2d", "--preflight"]:
        utc_day = _aware_utc(utc_now()).date()
        try:
            report = preflight(
                home=home,
                utc_day=utc_day,
                credential_path=project_root / TOSS_ENV_NAME,
                clock=lambda: _aware_utc(utc_now()),
                open_handle_probe=_lsof_open_handle_count,
            )
        except G2APreflightError as error:
            print(
                f"g2d preflight=refused reason={error.code} run_id={stable_run_id(utc_day)}"
            )
            return 1
        print(
            f"g2d preflight=ok run_id={report.run_id}"
            f" campaign_data_ceiling={G2D_CAMPAIGN_DATA_CEILING}"
            f" token_call_allowance={_G2B_TOKEN_MAX_ATTEMPTS}"
            f" calendar_ceiling={_G2B_CALENDAR_CEILING}"
            f" max_ticks={G2D_MAX_TICKS}"
        )
        return 0
    if args in (
        ["g2d", "--run-d-full-session"],
        ["g2d", "--run-d-full-session", "--approved-rerun"],
    ):
        approved = args[-1] == "--approved-rerun"
        utc_day = _aware_utc(utc_now()).date()
        try:
            deps = build_g2d_live_deps(
                project_root=project_root,
                clock=lambda: _aware_utc(utc_now()),
                glm_call=g2d_scripted_glm_call,
                sol_call=g2d_scripted_sol_call,
                https_handler_factory=None,
            )
            report = run_d_full_session(
                home=home,
                utc_day=utc_day,
                deps=deps,
                credential_path=project_root / TOSS_ENV_NAME,
                clock=lambda: _aware_utc(utc_now()),
                sleeper=sleep_seconds,
                open_handle_probe=_lsof_open_handle_count,
                approved_rerun=approved,
            )
        except G2APreflightError as error:
            print(f"g2d run=refused reason={error.code}")
            return 1
        except G2AStopError as error:
            print(
                f"g2d run=stopped reason={error.code} run_id={stable_run_id(utc_day)}"
            )
            return 2
        # Masked aggregate counts only; no symbol, quantity, account seq, token,
        # or URL vocabulary ever reaches this line.  CLI success must agree with
        # the durable run-state contract: only a clean session terminus is exit 0.
        clean = (
            not report.incomplete
            and report.production_invariant_ok
            and report.stop_reason in (None, "session_ended")
        )
        outcome = (
            "complete" if clean else "incomplete" if report.incomplete else "stopped"
        )
        print(
            f"g2d run={outcome} run_id={report.run_id}"
            f" stop_reason={report.stop_reason}"
            f" ticks={report.tick_count}"
            f" green_ticks={report.green_tick_count}"
            f" provider_started={report.provider_started_count}"
            f" token_started={report.token_started_count}"
            f" wire_attempts={report.wire_attempt_count}"
            f" glm_calls={report.glm_calls} sol_calls={report.sol_calls}"
            f" numeric_established={report.numeric_state_established_count}"
            f" trigger_fires={sum(report.trigger_counts.values())}"
            f" evidence_missing={report.numeric_evidence_missing_tick_count}"
            f" production_ok={report.production_invariant_ok}"
        )
        return 0 if clean else 2
    if args == ["setup"]:
        try:
            setup_live(home=home)
        except (OSError, ValueError):
            print("tracer-1a-live setup=private_state_invalid")
            return 1
        print("tracer-1a-live setup=complete")
        return 0
    if args == ["live"]:
        try:
            result = run_live(
                home=home,
                transport=EiaLiveTransport(),
                clock=utc_now,
                id_factory=lambda: uuid.uuid4().hex,
                run_id=f"live-{uuid.uuid4().hex[:12]}",
            )
        except LiveRunError as error:
            print(f"tracer-1a-live result={error.code}")
            return 3 if error.code == "not_due" else 1
        print(
            f"tracer-1a-live result=success started={result.started}"
            f" action={result.decision_action} reason={result.decision_reason}"
        )
        return 0
    print(
        "usage: tokmo-live setup|live|sec-setup"
        "|sec-probe tickers|submissions|dart-setup"
        "|dart-probe corpcode|list|krx-setup|krx-probe daily --date YYYYMMDD"
        "|toss-setup|toss-holdings|toss-report"
        "|g2a --preflight|g2a --run-a-one-hour"
        "|g2b --run-b-canary|g2b --run-b-one-hour"
        "|g2d --preflight|g2d --run-d-full-session [--approved-rerun]"
        " [--home PATH] [--project-root PATH]",
        file=sys.stderr,
    )
    return 64


# ---------------------------------------------------------------------------
# SEC holdings-targeted collector (separate from the one-shot AAPL tracer)
# ---------------------------------------------------------------------------

SEC_TARGETED_DB_NAME = "sec-targeted.sqlite3"
SEC_TARGETED_REGISTRY_REFRESH_DAYS = 1


@dataclass(frozen=True)
class SecTargetedRunResult:
    code: str
    started: int
    target_count: int
    filings_saved: int
    duplicates: int
    conflicts: int
    unresolved: tuple[str, ...]


def _holdings_snapshot_store_path(home: Path) -> Path:
    """Locate the canonical latest COMPLETE holdings snapshot store."""
    leaf = (home / ".local" / "share" / "tokmo-v3").resolve() / TOSS_LEAF_NAME
    return leaf / "holdings_snapshots.sqlite3"


def setup_sec_targeted(*, home: Path) -> None:
    """Create only Tokmo-owned private directories for the targeted collector."""
    config_dir, data_dir = _live_dirs(home)
    _private_dir(config_dir)
    _private_dir(data_dir)


def run_sec_targeted(
    *,
    home: Path,
    snapshot_store_path: Path | None,
    transport,
    clock: Clock,
    run_id: str,
    user_agent: str,
    sleeper: Callable[[float], None] = sleep_seconds,
    us_watchlist_symbols: frozenset[str] = frozenset(),
) -> SecTargetedRunResult:
    """One recurring SEC holdings-targeted filings tick.

    1. Read canonical latest COMPLETE holdings snapshot (or fail closed).
    2. Extract US-market symbols (dedup across all accounts).
    3. Merge injected active US watchlist symbols (dedup, provenance-tracked).
       KR watchlist is excluded by construction (US-only extraction).
    4. Resolve tickers -> CIKs via official company_tickers.json (cache daily).
    5. Per resolved target, fetch submissions and parse ALL recent filings
       with filing_date >= 2026-08-03.
    6. Persist additive filings, universe, registry, attempts.

    Network: at most 1 registry + N submissions, hard cap 20 targets / 21
    requests, 2s spacing between starts.  401/403/429 stop remaining and
    the run fails closed nonzero.

    Caps are applied after the union of holdings + watchlist symbols, so
    a watchlist-only run with zero holdings proceeds normally.
    """
    config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(config_dir)
    _require_private_owned_dir(data_dir)

    # Validate injected watchlist before any store/SQL operations
    if not isinstance(us_watchlist_symbols, frozenset):
        raise LiveRunError("invalid_watchlist")
    for sym in us_watchlist_symbols:
        if not isinstance(sym, str) or not sym:
            raise LiveRunError("invalid_watchlist")

    db_path = data_dir / SEC_TARGETED_DB_NAME
    store = SecTargetedStore(db_path, clock=clock)

    now = _aware_utc(clock())

    # 1. Read canonical latest COMPLETE holdings snapshot
    target_symbols: frozenset[str]
    target_provenance: dict[str, str]
    source_snapshot_ref: str
    captured_at: datetime

    if snapshot_store_path is not None and snapshot_store_path.exists():
        try:
            snapshot_store = HoldingsSnapshotStore(
                snapshot_store_path,
                clock=clock,
            )
            snapshot = snapshot_store.read_latest()
            # Merge holdings + watchlist with provenance
            target_provenance = merge_sec_target_provenance(
                snapshot,
                us_watchlist_symbols,
            )
            target_symbols = frozenset(target_provenance)
            captured_at = snapshot.captured_at
            source_snapshot_ref = (
                f"{snapshot.source}:{snapshot.captured_at.isoformat()}"
            )
        except HoldingsSnapshotReadError:
            # Snapshot failed/partial/missing: use last COMPLETE target universe
            last = store.load_last_universe_with_provenance()
            if last is not None:
                last_symbols, last_provenance = last
                # Re-merge with injected watchlist on fallback, preserving
                # the last known per-symbol provenance (a stored
                # watchlist-only symbol must never gain a HOLDING claim).
                target_provenance = merge_fallback_target_provenance(
                    last_symbols,
                    last_provenance,
                    us_watchlist_symbols,
                )
                target_symbols = frozenset(target_provenance)
                captured_at = now
                source_snapshot_ref = "last_complete_universe"
            elif us_watchlist_symbols:
                # No holdings snapshot but watchlist present: proceed
                target_symbols = us_watchlist_symbols
                target_provenance = {sym: "WATCHLIST" for sym in target_symbols}
                captured_at = now
                source_snapshot_ref = "watchlist_only"
            else:
                # No prior COMPLETE and no watchlist: fail closed, network 0
                claim = store.claim_run(run_id=run_id, started_at=now)
                if claim.status == "overlap":
                    raise LiveRunError("overlap")
                store.complete_run(
                    run_id=run_id,
                    code="failed_no_universe",
                    started_count=0,
                    started_at=now,
                )
                return SecTargetedRunResult("failed_no_universe", 0, 0, 0, 0, 0, ())
    else:
        # No snapshot store path: use last COMPLETE if available
        last = store.load_last_universe_with_provenance()
        if last is not None:
            last_symbols, last_provenance = last
            target_provenance = merge_fallback_target_provenance(
                last_symbols,
                last_provenance,
                us_watchlist_symbols,
            )
            target_symbols = frozenset(target_provenance)
            captured_at = now
            source_snapshot_ref = "last_complete_universe"
        elif us_watchlist_symbols:
            target_symbols = us_watchlist_symbols
            target_provenance = {sym: "WATCHLIST" for sym in target_symbols}
            captured_at = now
            source_snapshot_ref = "watchlist_only"
        else:
            claim = store.claim_run(run_id=run_id, started_at=now)
            if claim.status == "overlap":
                raise LiveRunError("overlap")
            store.complete_run(
                run_id=run_id,
                code="failed_no_universe",
                started_count=0,
                started_at=now,
            )
            return SecTargetedRunResult("failed_no_universe", 0, 0, 0, 0, 0, ())

    # Enforce hard admitted target cap (after union)
    if len(target_symbols) > MAX_ADMITTED_TARGETS:
        claim = store.claim_run(run_id=run_id, started_at=now)
        if claim.status == "overlap":
            raise LiveRunError("overlap")
        store.complete_run(
            run_id=run_id,
            code="failed_target_cap",
            started_count=0,
            started_at=now,
        )
        return SecTargetedRunResult(
            "failed_target_cap", 0, len(target_symbols), 0, 0, 0, ()
        )

    # Attempt-before-I/O: claim the run
    claim = store.claim_run(run_id=run_id, started_at=now)
    if claim.status == "overlap":
        raise LiveRunError("overlap")

    # Store target universe provenance
    universe_id = hashlib.sha256(
        json.dumps(sorted(target_symbols)).encode()
    ).hexdigest()[:64]
    store.store_universe(
        universe_id=universe_id,
        captured_at=captured_at,
        symbols=target_symbols,
        source_snapshot=source_snapshot_ref,
        provenance=target_provenance,
    )

    started_count = 0
    seq = 0

    def pace_next_start() -> None:
        """Conservatively keep request starts at least two seconds apart."""
        if started_count:
            sleeper(MIN_START_INTERVAL_SECONDS)

    # 2. Resolve tickers -> CIKs via official registry
    registry_cache = store.load_registry()
    mapped: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []

    # Determine which tickers need a registry refresh (daily KST or new ticker)
    kst_date = _seoul_calendar_date(now)
    needs_registry = False
    for sym in target_symbols:
        if sym not in registry_cache:
            needs_registry = True
            break
        # Check daily freshness: refresh at most once per KST date
        cached_refreshed = registry_cache[sym][2]
        try:
            cached_kst = (
                _parse_utc(cached_refreshed).astimezone(ZoneInfo("Asia/Seoul")).date()
            )
        except (ValueError, TypeError):
            needs_registry = True
            break
        if cached_kst != kst_date:
            needs_registry = True
            break
    if needs_registry:
        pace_next_start()
        seq += 1
        started_count += 1
        registry_now = _aware_utc(clock())
        store.record_attempt(
            run_id=run_id,
            seq=seq,
            endpoint="sec_company_tickers",
            cik=None,
            ticker=None,
            started_at=registry_now,
            outcome_code="started",
        )
        registry_result = query_company_tickers_multi(
            tickers=target_symbols,
            user_agent=user_agent,
            transport=transport,
            clock=lambda: registry_now,
        )
        outcome_code = (
            registry_result.attempt.code
            if registry_result.attempt is not None
            else "protocol_failure"
        )
        store.record_attempt(
            run_id=run_id,
            seq=seq,
            endpoint="sec_company_tickers",
            cik=None,
            ticker=None,
            started_at=registry_now,
            outcome_code=outcome_code,
        )

        if registry_result.attempt is not None and (
            registry_result.attempt.code in STOP_CODES
        ):
            store.complete_run(
                run_id=run_id,
                code="failed_registry_stop",
                started_count=started_count,
                started_at=now,
            )
            raise LiveRunError("fair_access_stop")

        if isinstance(registry_result.result, TickerRegistryResult):
            for ticker, mapping in registry_result.result.mappings.items():
                store.store_registry(
                    ticker=ticker,
                    cik=mapping.cik,
                    company_name=mapping.company_name,
                    refreshed_at=registry_now,
                )
                mapped[ticker] = (mapping.cik, mapping.company_name)
            unresolved = sorted(registry_result.result.unmapped)
        else:
            store.complete_run(
                run_id=run_id,
                code="failed_registry",
                started_count=started_count,
                started_at=now,
            )
            return SecTargetedRunResult(
                "failed_registry",
                started_count,
                len(target_symbols),
                0,
                0,
                0,
                (),
            )
    else:
        mapped = {
            t: (registry_cache[t][0], registry_cache[t][1])
            for t in target_symbols
            if t in registry_cache
        }
        unresolved = sorted(t for t in target_symbols if t not in mapped)

    del registry_cache

    # 3. Per resolved target: fetch submissions
    filings_saved = 0
    duplicates = 0
    conflicts = 0

    for ticker in sorted(mapped):
        cik, company_name = mapped[ticker]

        # Enforce request cap
        if started_count >= MAX_REQUESTS_PER_TICK:
            store.complete_run(
                run_id=run_id,
                code="failed_request_cap",
                started_count=started_count,
                started_at=now,
            )
            return SecTargetedRunResult(
                "failed_request_cap",
                started_count,
                len(target_symbols),
                filings_saved,
                duplicates,
                conflicts,
                tuple(unresolved),
            )

        pace_next_start()
        seq += 1
        started_count += 1
        sub_now = _aware_utc(clock())
        store.record_attempt(
            run_id=run_id,
            seq=seq,
            endpoint="sec_submissions",
            cik=cik,
            ticker=ticker,
            started_at=sub_now,
            outcome_code="started",
        )
        sub_result = query_submissions_all(
            ticker=ticker,
            cik=cik,
            company_name=company_name,
            min_filing_date=MIN_FILING_DATE,
            user_agent=user_agent,
            transport=transport,
            clock=lambda: sub_now,
        )
        outcome_code = (
            sub_result.attempt.code
            if sub_result.attempt is not None
            else "protocol_failure"
        )
        store.record_attempt(
            run_id=run_id,
            seq=seq,
            endpoint="sec_submissions",
            cik=cik,
            ticker=ticker,
            started_at=sub_now,
            outcome_code=outcome_code,
        )

        if sub_result.attempt is not None and (sub_result.attempt.code in STOP_CODES):
            store.complete_run(
                run_id=run_id,
                code="failed_submissions_stop",
                started_count=started_count,
                started_at=now,
            )
            raise LiveRunError("fair_access_stop")

        if isinstance(sub_result.result, tuple):
            ticker_provenance = target_provenance.get(ticker, "HOLDING")
            for filing in sub_result.result:
                result = store.save_filing(
                    filing=filing,
                    run_id=run_id,
                    stored_at=sub_now,
                    provenance=ticker_provenance,
                )
                if result.status == "saved":
                    filings_saved += 1
                elif result.status == "duplicate":
                    duplicates += 1
                else:
                    conflicts += 1
        else:
            store.complete_run(
                run_id=run_id,
                code="failed_submissions",
                started_count=started_count,
                started_at=now,
            )
            return SecTargetedRunResult(
                "failed_submissions",
                started_count,
                len(target_symbols),
                filings_saved,
                duplicates,
                conflicts,
                tuple(unresolved),
            )

    # 4. Complete: only if registry/cache valid and every resolved target completes
    if conflicts:
        code = "failed_conflict"
    elif unresolved:
        code = "complete_with_unresolved"
    else:
        code = "complete"

    store.complete_run(
        run_id=run_id,
        code=code,
        started_count=started_count,
        started_at=now,
    )

    return SecTargetedRunResult(
        code=code,
        started=started_count,
        target_count=len(target_symbols),
        filings_saved=filings_saved,
        duplicates=duplicates,
        conflicts=conflicts,
        unresolved=tuple(unresolved),
    )


# ---------------------------------------------------------------------------
# SEC targeted operation seam (future cron target: 10,40 * * * * KST)
#
# This is the composition-root operation wrapper for run_sec_targeted.  It
# wires ambient dependencies (clock, sleeper, transport) and derives a
# deterministic run_id from the most recent scheduled slot, so that the
# canonical store's PK overlap prevents same-slot duplicate runs without
# any hidden retry.
#
# Design contract:
# - Success is silent (stdout/stderr empty at the CLI layer).
# - Retry count is zero: one attempt, one result.
# - AI, alerts, orders, and OpenDART are never involved.
# - The canonical snapshot path is derived from home only; there is no
#   arbitrary snapshot path CLI argument and no custom run_id CLI argument.
# - The SEC_USER_AGENT value is loaded from sec.env but never printed,
#   persisted outside the canonical store, or repr'd.
# ---------------------------------------------------------------------------

SEC_SLOT_MINUTES = (10, 40)


def _sec_scheduled_slot_kst(now: datetime) -> str:
    """Derive a deterministic run_id from the most recent ``10,40 * * * *``
    scheduled slot in KST.

    Returns ``YYYYMMDDTHHMM`` with date/hour/minute only — no symbols,
    account identifiers, or secrets.  Boundary semantics:

    - minute in [10, 40)  → slot is HH:10 of the same hour
    - minute in [40, 60)  → slot is HH:40 of the same hour
    - minute in [0, 10)   → slot is (HH-1):40 of the same day (or previous
                            day 23:40 when HH == 0)
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("aware_now_required")
    kst = now.astimezone(ZoneInfo("Asia/Seoul"))
    minute = kst.minute
    if minute >= 40:
        slot = kst.replace(minute=40, second=0, microsecond=0)
    elif minute >= 10:
        slot = kst.replace(minute=10, second=0, microsecond=0)
    else:
        slot = (kst - timedelta(hours=1)).replace(minute=40, second=0, microsecond=0)
    return slot.strftime("%Y%m%dT%H%M")


# ---------------------------------------------------------------------------
# G1A: Fundamental material composition wiring seam
#
# SDS §12 G1A item 8 authorizes minimal wiring in composition.py.  The
# actual implementation lives in fundamental_materialization.py (the leaf
# builder) so that behavioral effect-zero/store-factory tests can exercise
# it independently of this composition-root import chain.  This wrapper is
# a pure delegator with a function-local import: no import or construction
# side effects arise from importing composition.py itself.
# ---------------------------------------------------------------------------


def build_fundamental_materialization_deps(
    *,
    home: Path,
    clock: Clock,
):
    """Thin delegator to the leaf builder in fundamental_materialization.

    B3 fix: the composition wrapper function-locally imports the live
    transports and passes their factories to the leaf builder. The leaf
    builder imports NO live modules — only composition does.
    """
    from tokmo.facts.opendart_fundamentals_live import (
        OpenDartFundamentalsLiveTransport,
    )
    from tokmo.facts.sec_fundamentals_live import SECCompanyFactsLiveTransport
    from tokmo.programs.fundamental_materialization import (
        build_fundamental_materialization_deps as _build,
    )

    return _build(
        home=home,
        clock=clock,
        sec_transport_factory=SECCompanyFactsLiveTransport,
        opendart_transport_factory=OpenDartFundamentalsLiveTransport,
    )


def run_sec_targeted_operation(
    home: Path,
    clock: Clock = utc_now,
    sleeper: Callable[[float], None] = sleep_seconds,
    transport=None,
) -> SecTargetedRunResult:
    """One SEC targeted operation tick: wire ambient deps, derive run_id,
    validate credentials, and delegate to ``run_sec_targeted``.

    Parameters
    ----------
    home:
        Private home directory that owns the configured Tokmo state roots.
    clock:
        Aware-UTC wall clock callable.  Defaults to the ambient ``utc_now``
        because this is the composition root — the single place where the
        ambient clock leaf is wired.
    sleeper:
        Real elapsed-time pacing callable passed straight to the core.
    transport:
        SEC transport callable.  ``None`` (default) constructs a
        ``SecLiveTransport`` for production cron use.

    Returns a ``SecTargetedRunResult`` with an additional ``failed_config``
    code when ``sec.env`` is missing, a symlink, or mode-unsafe.
    """
    config_dir, data_dir = _live_dirs(home)
    _require_private_owned_dir(config_dir)
    _require_private_owned_dir(data_dir)

    env_path = config_dir / SEC_ENV_NAME
    try:
        if (
            env_path.is_symlink()
            or not env_path.is_file()
            or (env_path.stat().st_mode & 0o777) != 0o600
        ):
            return SecTargetedRunResult("failed_config", 0, 0, 0, 0, 0, ())
    except OSError:
        return SecTargetedRunResult("failed_config", 0, 0, 0, 0, 0, ())

    try:
        config = load_sec_config(env_path)
    except Exception:
        return SecTargetedRunResult("failed_config", 0, 0, 0, 0, 0, ())

    run_id = _sec_scheduled_slot_kst(clock())
    snapshot_store_path = _holdings_snapshot_store_path(home)

    # Load canonical project watchlists.  Production operation must load
    # canonical project watchlists with no arbitrary CLI path.
    # SEC only targets US market; KR is excluded by construction.
    us_watchlist_symbols: frozenset[str] = frozenset()
    try:
        all_watchlists = load_watchlists(PROJECT_ROOT / "watchlists")
        us_watchlist_symbols = all_watchlists.get(Market.US, frozenset())
    except Exception:
        return SecTargetedRunResult("failed_watchlist", 0, 0, 0, 0, 0, ())

    if transport is None:
        transport = SecLiveTransport()

    try:
        return run_sec_targeted(
            home=home,
            snapshot_store_path=snapshot_store_path,
            transport=transport,
            clock=clock,
            run_id=run_id,
            user_agent=config.user_agent,
            sleeper=sleeper,
            us_watchlist_symbols=us_watchlist_symbols,
        )
    except LiveRunError as exc:
        if exc.code == "overlap":
            return SecTargetedRunResult("overlap", 0, 0, 0, 0, 0, ())
        if exc.code == "invalid_watchlist":
            return SecTargetedRunResult("failed_watchlist", 0, 0, 0, 0, 0, ())
        raise


def build_macro_runtime_deps(config):
    from tokmo.facts.toss import (
        TossAccessToken,
        build_auth_request,
        parse_token_response,
    )
    from tokmo.facts.toss_auth_live import TossAuthLiveTransport
    from tokmo.facts.toss_reads import (
        TossReadOperation,
        build_read_request,
        query_read,
    )
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport
    from tokmo.records.macro_runtime_receipts import MacroRuntimeReceiptStore

    closed = dict(config)
    store = MacroRuntimeReceiptStore(Path(closed["root"]))
    store.reconcile_unknown()
    if closed.get("evaluate") is None and closed.get("credentials") is not None:
        auth_transport = closed.get("auth_transport") or TossAuthLiveTransport()
        read_transport = closed.get("read_transport") or TossReadsLiveTransport()
        clock = closed["clock"]
        credentials = closed["credentials"]

        def evaluate():
            starts = 1
            reply = auth_transport(
                build_auth_request(
                    client_id=credentials[0],
                    client_secret=credentials[1],
                )
            )
            token = (
                parse_token_response(
                    reply.body,
                    observed_at=clock(),
                )
                if hasattr(reply, "body")
                else None
            )
            if not isinstance(token, TossAccessToken):
                return {"starts": starts, "status": "FAILED"}
            operations = (
                (
                    TossReadOperation.INDICATOR_CANDLES,
                    {"symbol": "KOSPI", "interval": "1d", "count": 200},
                ),
                (
                    TossReadOperation.INDICATOR_INVESTOR_TRADING,
                    {"symbol": "KOSPI", "interval": "1d", "count": 100},
                ),
                (
                    TossReadOperation.STOCK_CANDLES,
                    {"symbol": "261240", "interval": "1d", "count": 200},
                ),
            )
            typed_pages = []
            for operation, parameters in operations:
                starts += 1
                outcome = query_read(
                    build_read_request(
                        operation,
                        token=token.value,
                        **parameters,
                    ),
                    transport=read_transport,
                    clock=clock,
                )
                if outcome.code != "success":
                    return {"starts": starts, "status": "FAILED"}
                typed_pages.append(outcome.value)
            authorities = closed.get("session_authorities")
            completed = closed.get("completed_session")
            if authorities is None or completed is None:
                return {"starts": starts, "status": "FAILED"}
            from zoneinfo import ZoneInfo

            from tokmo.programs.macro_canary_adapter import (
                _G1B_LIFECYCLE_SPEC,
                build_macro_bundle,
                compute_firing_rate_status,
            )
            from tokmo.programs.macro_divergence_analyzer import (
                evaluate_macro_divergence,
            )
            from tokmo.values.session_gate import Market
            from tokmo.values.signal_program_lifecycle import (
                ProgramLifecycleStateV1,
                ProgramLifecycleStatusV1,
            )

            series_dates = []
            for page in typed_pages:
                dates = set()
                values = getattr(page, "records", getattr(page, "candles", ()))
                for value in values:
                    dates.add(
                        value.date
                        if hasattr(value, "date")
                        else value.timestamp.astimezone(ZoneInfo("Asia/Seoul")).date()
                    )
                series_dates.append(dates)
            common = set.intersection(*series_dates)
            official = frozenset(
                day
                for day in common
                if (
                    lambda row: (
                        row.get("session") == "regular"
                        and row.get("local_date") in (day, day.isoformat())
                    )
                )(authorities[0].read(Market.KR, day) or {})
            )
            continuity_proof = (bool(common) and official == common, official)
            as_of = clock()
            bundle, continuity = build_macro_bundle(
                (typed_pages[0],),
                (typed_pages[1],),
                (typed_pages[2],),
                fetched_at=as_of,
                completed_session_date=completed,
                as_of=as_of,
                calendar_dates=continuity_proof,
            )
            lifecycle = ProgramLifecycleStateV1(
                _G1B_LIFECYCLE_SPEC, ProgramLifecycleStatusV1.ACTIVE, None, 0, 0, as_of
            )
            pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
            firing_status, _rate = compute_firing_rate_status(0, 0, normative=True)
            series = {ref.operation: ref.series_digest for ref in bundle.source_refs}
            source_digest = hashlib.sha256(
                "|".join(series[key] for key in sorted(series)).encode()
            ).hexdigest()
            lifecycle_digest = hashlib.sha256(
                (_G1B_LIFECYCLE_SPEC.spec_digest + "|ACTIVE").encode()
            ).hexdigest()
            request_manifest_digest = hashlib.sha256(
                b"TOKEN:1|INDICATOR_CANDLES:1:200|INDICATOR_INVESTOR_TRADING:1:100|STOCK_CANDLES:1:200|CAP:4"
            ).hexdigest()
            common_dates = sorted(
                {x.session_date for x in bundle.kospi_candles}
                & {x.session_date for x in bundle.investor_records}
                & {x.session_date for x in bundle.proxy_candles}
            )
            return {
                "starts": starts,
                "status": firing_status,
                "evidence_digest": pack.pack_digest,
                "pack_digest": pack.pack_digest,
                "source_digest": source_digest,
                "spec_digest": _G1B_LIFECYCLE_SPEC.spec_digest,
                "lifecycle_digest": lifecycle_digest,
                "request_manifest_digest": request_manifest_digest,
                "request_cap": 4,
                "continuity": continuity,
                "common_count": len(common_dates),
                "common_start": None
                if not common_dates
                else common_dates[0].isoformat(),
                "common_end": None
                if not common_dates
                else common_dates[-1].isoformat(),
                "fired": any(component.fired for component in pack.components),
            }

        closed["evaluate"] = evaluate
    fred_caller = closed.get("fred_caller")
    if fred_caller is not None and closed.get("evaluate") is not None:
        _original_evaluate = closed["evaluate"]

        def _evaluate_with_fred():
            fields = _original_evaluate()
            if fields.get("status") != "FAILED":
                fields.update(fred_caller())
            return fields

        closed["evaluate"] = _evaluate_with_fred
    return {"config": closed, "receipts": store}


def macro_runtime_main(deps):
    config = deps["config"]
    from tokmo.programs.macro_runtime import (
        MacroRuntimePolicyV1,
        MacroRuntimeTriggerV1,
        run_macro_runtime_once,
    )

    policy = MacroRuntimePolicyV1(
        bool(config.get("enabled")),
        str(config.get("policy", "PAUSED")),
    )
    trigger = MacroRuntimeTriggerV1(
        config.get("scheduled_at"),
        config.get("completed_session"),
        bool(config.get("replay", False)),
    )
    evaluate = config.get("evaluate")
    if evaluate is None:
        return 0
    run_macro_runtime_once(policy, trigger, deps["receipts"], evaluate)
    return 0


def build_pulse_production_live_deps(
    *,
    project_root: Path,
    clock: Callable[[], datetime],
    runtime_root: Path,
    dialogue_cutoff: datetime,
    https_handler_factory=None,
    dialogue_activation_enabled: bool = False,
    environ=None,
    quant_market_close_runner=None,
    quant_input_capture=None,
    capture_clock=None,
    capture_sleeper=sleep_seconds,
):
    """Compose the G3A Pulse from the canonical Toss read adapters.

    Construction performs no provider call.  The bearer cell is process
    memory only and is cleared by ``run_pulse_once`` at the tick boundary.

    G3B send activation (activation SDS v0.2 §4.1): the code default stays
    ``dialogue_activation_enabled=False`` — only the runtime gate file flips
    it, so deployment alone never activates sending. The Telegram sender is
    composed here and only here (live-import guard) from the injected
    ``environ`` mapping; the process environment itself is read at the
    ambient edge (``tools/run_tokmo_pulse.py``), never in this tree. Env is
    required only when the gate resolved enabled: gate true with env absent
    leaves the sender ``None``, which the dialogue stage surfaces as a
    send-step failure while register/enqueue and every collection lane
    proceed untouched.
    """
    import importlib.util

    from tokmo.programs.conditional_detection import (
        PulseConditionalPageOutcomeV1,
    )
    from tokmo.programs.execution_detection import PulseOrdersPageOutcomeV1
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
        read_news_gate,
    )
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1

    def handler():
        return None if https_handler_factory is None else https_handler_factory()

    auth_transport = TossAuthLiveTransport(https_handler=handler())
    prices_transport = TossPricesLiveTransport(https_handler=handler())
    reads_transport = TossReadsLiveTransport(https_handler=handler())
    config = load_toss_config(Path(project_root) / TOSS_ENV_NAME)
    watchlists = load_watchlists(Path(project_root) / "watchlists")
    bearer_cell = {"value": None}

    def now():
        value = clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("pulse clock must be aware")
        return value.astimezone(UTC)

    def token_fetch():
        if bearer_cell["value"] is None:
            outcome = auth_transport(
                build_auth_request(
                    client_id=config.client_id, client_secret=config.client_secret
                )
            )
            if not isinstance(outcome, _TossTransportSuccess):
                raise RuntimeError("token_transport_failed")
            parsed = parse_token_response(outcome.body, observed_at=now())
            if not isinstance(parsed, TossAccessToken):
                raise RuntimeError("token_parse_failed")
            bearer_cell["value"] = parsed.value
        return bearer_cell["value"]

    def quotes_fetch(market, symbols):
        del market
        outcome = query_current_prices(
            symbols=tuple(symbols),
            token=token_fetch(),
            transport=prices_transport,
            clock=now,
        )
        facts = tuple(outcome.facts)
        seen = {fact.symbol for fact in facts}
        failed = tuple(symbol for symbol in symbols if symbol not in seen)
        return PulseQuoteBatchOutcomeV1(outcome.code, facts, failed)

    def orders_page_fetch(
        *, account_scope, native_ref, status, from_date, to_date, cursor, limit
    ):
        del account_scope
        if native_ref is None:
            return PulseOrdersPageOutcomeV1("account_unmapped")
        request = build_read_request(
            TossReadOperation.ORDERS,
            token=token_fetch(),
            account_seq=native_ref,
            status=status,
            cursor=cursor,
            limit=limit,
            **{"from": from_date, "to": to_date},
        )
        outcome = query_read(request, transport=reads_transport, clock=now)
        if outcome.code != "success":
            return PulseOrdersPageOutcomeV1(outcome.code)
        value = outcome.value
        return PulseOrdersPageOutcomeV1(
            "success", tuple(value.orders), value.next_cursor, value.has_next
        )

    def order_detail_fetch(*, account_scope, native_ref, order_id):
        del account_scope
        if native_ref is None:
            return None
        request = build_read_request(
            TossReadOperation.ORDER_DETAIL,
            token=token_fetch(),
            account_seq=native_ref,
            order_id=order_id,
        )
        outcome = query_read(request, transport=reads_transport, clock=now)
        return outcome.value if outcome.code == "success" else None

    def conditional_orders_fetch(*, account_scope, native_ref, status, cursor, limit):
        del account_scope
        if native_ref is None:
            return PulseConditionalPageOutcomeV1("account_unmapped")
        request = build_read_request(
            TossReadOperation.CONDITIONAL_ORDERS,
            token=token_fetch(),
            account_seq=native_ref,
            status=status,
            cursor=cursor,
            limit=limit,
        )
        outcome = query_read(request, transport=reads_transport, clock=now)
        if outcome.code != "success":
            return PulseConditionalPageOutcomeV1(outcome.code)
        value = outcome.value
        next_cursor = value.next_cursor if isinstance(value.next_cursor, str) else None
        return PulseConditionalPageOutcomeV1(
            "success", tuple(value.conditional_orders), next_cursor, value.has_next
        )

    telegram_sender = None
    updates_provider = None
    callback_ack = None
    if environ is not None:
        bot_token = environ.get(ENV_TELEGRAM_TOKEN)
        chat_id = environ.get(ENV_TELEGRAM_CHAT_ID)
        if bot_token and chat_id:

            def telegram_sender(text, reply_markup=None):
                return send_dialogue_message(
                    body_text=text,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=reply_markup,
                )

            def updates_provider(offset):
                return fetch_dialogue_updates(
                    offset=offset, bot_token=bot_token, chat_id=chat_id
                )

            def callback_ack(callback_query_id):
                # Same stock-bot token; bounded, never raises (spinner ack).
                return answer_dialogue_callback(
                    callback_query_id=callback_query_id, bot_token=bot_token
                )

    validator_path = (
        Path(project_root) / "scripts" / "playbooks" / "validate_playbooks.py"
    )
    validator_spec = importlib.util.spec_from_file_location(
        "tokmo_canonical_playbook_validator", validator_path
    )
    if validator_spec is None or validator_spec.loader is None:
        raise RuntimeError("playbook_validator_unavailable")
    validator_module = importlib.util.module_from_spec(validator_spec)
    validator_spec.loader.exec_module(validator_module)
    playbook_validator = validator_module.validate_tree

    # NEWS lane gate (m8): the canonical ops kernel is the NEWS lane's own
    # dependency, so it is constructed only when the gate resolved enabled.
    # Constructing it unconditionally let an unhealthy ops DB fail every
    # live dep — collection lanes included — while NEWS was switched off.
    # Gate on: this stays the single canonical store, injected below.
    ops_kernel = None

    # Tier-2 official-evidence seam (SDS A2 §Z7/A3 §V5, decision 18),
    # composed inside this already-admitted seam function (live-import
    # guard): the OpenDART list transport plus a LAZY credential loader
    # over the existing ``opendart.env`` contract. Nothing is validated or
    # opened at composition time — every refusal surfaces at lookup time
    # as the reader's typed AUTH_UNAVAILABLE fail-closed outcome, so pulse
    # construction can never break on credential state.
    def build_news_official_live_seam(home=None):
        from tokmo.programs.news_official_evidence import OfficialLiveSeamV1

        base = Path.home() if home is None else Path(home)

        def credential_loader():
            # SOL M2: the EXISTING OpenDART composition credential
            # contract is reused verbatim — private directory guard +
            # private credential guard + config parse — and runs only at
            # lookup time inside the reader's guarded try.
            from tokmo.opendart_bulk_composition import (
                _require_private_credential,
                _require_private_directory,
            )

            config_dir = base / ".config" / "tokmo-v3"
            _require_private_directory(config_dir)
            config_path = config_dir / DART_ENV_NAME
            _require_private_credential(config_path)
            return load_opendart_config(config_path).api_key

        # SOL M2: the transport is a lazy FACTORY — OpenDartLiveTransport
        # is constructed inside the guarded lookup, never at assembly.
        return OfficialLiveSeamV1(
            credential_loader=credential_loader, transport_factory=OpenDartLiveTransport
        )

    # The snapshot object is passed through unmodified: it carries the G3
    # delivery judgment derived from the same single gate read, so producer
    # and delivery can never split-brain across two file reads.
    from tokmo.programs.news_delivery_route import build_news_sender

    news_telegram_sender = build_news_sender(
        runtime_root, telegram_sender, send_dialogue_message
    )
    news_snapshot = read_news_gate(Path(runtime_root))
    if news_snapshot[0].enabled:
        ops_kernel = OpsKernelStore(
            Path(runtime_root) / "ops" / "ops_kernel.sqlite3",
            stable_phase_ids=frozenset(),
        )
    if (
        quant_input_capture is None
        and (environ or {}).get("TOKMO_QUANT_INPUT_CAPTURE") == "1"
    ):
        from tokmo.programs.quant_input_capture import requested_capture_consumer

        quant_input_capture = requested_capture_consumer(
            project_root=project_root,
            data_root=runtime_root,
            clock=capture_clock or now,
        )
    opportunity_capture = None
    if (environ or {}).get("TOKMO_OPPORTUNITY_CAPTURE") == "1":
        from tokmo.programs.opportunity_rotation_capture import capture_consumer

        opportunity_capture = capture_consumer(
            data_root=runtime_root,
            transport=reads_transport,
            price_transport=prices_transport,
            clock=capture_clock or now,
            sleeper=capture_sleeper,
        )
    deps = build_pulse_production_deps(
        news_gate=news_snapshot,
        root=Path(runtime_root),
        clock=now,
        watchlist_symbols=watchlists,
        dialogue_cutoff=dialogue_cutoff,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        orders_page_fetch=orders_page_fetch,
        order_detail_fetch=order_detail_fetch,
        conditional_orders_fetch=conditional_orders_fetch,
        quotes_provider_enabled=True,
        execution_provider_enabled=True,
        conditional_provider_enabled=True,
        dialogue_activation_enabled=dialogue_activation_enabled,
        telegram_sender=telegram_sender,
        news_telegram_sender=news_telegram_sender,
        updates_provider=updates_provider,
        callback_ack=callback_ack,
        news_ops_kernel=ops_kernel,
        playbook_root=Path(project_root) / "playbooks",
        watchlist_root=Path(project_root) / "watchlists",
        playbook_validator=playbook_validator,
        official_live_seam=build_news_official_live_seam(),
        quant_market_close_runner=quant_market_close_runner,
        quant_input_capture=quant_input_capture,
        opportunity_capture=opportunity_capture,
        tick_cleanup=lambda: bearer_cell.update(value=None),
    )
    return deps


def build_order_execution_client(
    *,
    account_scope,
    journal_path,
    token_provider,
    account_ref_provider,
    clock,
    live_enabled=False,
    https_handler=None,
):
    """Explicit order assembly; never called by existing scheduled collectors.

    The owner supplies the already-coordinated bearer/account providers.
    This factory does not obtain credentials, refresh tokens, or send orders.
    """
    from tokmo.facts.toss_order_commands_live import TossOrderCommandsLiveTransport
    from tokmo.programs.order_execution import OrderExecutionClient
    from tokmo.records.order_command_journal import OrderCommandJournal

    return OrderExecutionClient(
        account_scope=account_scope,
        mode="live",
        journal=OrderCommandJournal(journal_path),
        token_provider=token_provider,
        account_ref_provider=account_ref_provider,
        clock=clock,
        transport=TossOrderCommandsLiveTransport(
            enabled=live_enabled, https_handler=https_handler
        ),
    )


def build_rotation_daily_wiring(
    *,
    project_root,
    root,
    account_scope,
    journal_path,
    clock,
    live_enabled=False,
    https_handler=None,
):
    """Unattended rotation cycle: one bearer per process, fresh reads, one order path.

    The broker invalidates the previous bearer when a new one is issued, so the
    caller must run between pulse ticks; the bearer lives only in this closure.
    Construction performs no network I/O. ``live_enabled`` gates the socket of
    the order transport only — authorization and journaling are unchanged.
    """
    from decimal import Decimal

    from tokmo.facts.toss_order_commands_live import TossOrderCommandsLiveTransport
    from tokmo.programs.order_execution import OrderExecutionClient
    from tokmo.programs.rotation_daily import RotationReads, RotationWiring
    from tokmo.records.account_native_refs import AccountNativeRefStoreV1
    from tokmo.records.calendar_lookahead import (
        CalendarLookaheadCorruptError,
        CalendarLookaheadStoreV1,
        judge_lookahead_session,
    )
    from tokmo.records.order_command_journal import OrderCommandJournal
    from tokmo.values.holdings import TossAccountHoldingsFact

    env_path = Path(project_root) / TOSS_ENV_NAME
    _require_private_credential_file(env_path)
    config = load_toss_config(env_path)
    auth_transport = TossAuthLiveTransport(https_handler=https_handler)
    data_transport = TossDataLiveTransport(https_handler=https_handler)
    reads_transport = TossReadsLiveTransport(https_handler=https_handler)
    prices_transport = TossPricesLiveTransport(https_handler=https_handler)
    bearer = {"value": None}

    def now():
        value = clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("rotation clock must be aware")
        return value.astimezone(UTC)

    def token_fetch():
        if bearer["value"] is None:
            outcome = auth_transport(
                build_auth_request(
                    client_id=config.client_id, client_secret=config.client_secret
                )
            )
            if not isinstance(outcome, _TossTransportSuccess):
                raise RuntimeError("token_transport_failed")
            parsed = parse_token_response(outcome.body, observed_at=now())
            if not isinstance(parsed, TossAccessToken):
                raise RuntimeError("token_parse_failed")
            bearer["value"] = parsed.value
        return bearer["value"]

    refs = AccountNativeRefStoreV1(
        Path(root) / "ops" / "account_native_refs.sqlite3", clock=clock
    ).read_refs()
    account_ref = refs.reveal(account_scope)
    if account_ref is None:
        raise ValueError("account_ref_unavailable")

    def read(operation, *, account, **kwargs):
        try:
            request = build_read_request(
                operation,
                token=token_fetch(),
                account_seq=account_ref if account else None,
                **kwargs,
            )
            outcome = query_read(request, transport=reads_transport, clock=now)
        except Exception:
            return None
        return outcome.value if outcome.code == "success" else None

    def order_detail(order_id):
        return read(TossReadOperation.ORDER_DETAIL, account=True, order_id=order_id)

    def conditional_detail(conditional_order_id):
        return read(
            TossReadOperation.CONDITIONAL_ORDER_DETAIL,
            account=True,
            conditional_order_id=conditional_order_id,
        )

    def holdings_quantity(symbol):
        try:
            held = data_transport(
                build_holdings_request(token=token_fetch(), account_seq=account_ref)
            )
            if not isinstance(held, _TossTransportSuccess):
                return None
            parsed = parse_holdings_response(
                held.body, account_ordinal=1, account_count=1, captured_at=now()
            )
        except Exception:
            return None
        if not isinstance(parsed, TossAccountHoldingsFact):
            return None
        return sum(
            (
                Decimal(item.quantity)
                for item in parsed.holdings
                if item.symbol == symbol
            ),
            Decimal(0),
        )

    lookahead = CalendarLookaheadStoreV1(
        Path(root) / "ops" / "calendar_lookahead.sqlite3", clock=clock
    )

    def session_verdict(market, now_utc):
        """Cadence-collected calendar only — consulted before any bearer exists."""
        try:
            days = lookahead.read_days(Market(market))
            return judge_lookahead_session(
                Market(market), tuple(days), now_utc.astimezone(UTC)
            )
        except (ValueError, CalendarLookaheadCorruptError):
            return None

    def evidence(policy):
        items = {}
        try:
            quote = query_current_prices(
                symbols=(policy.symbol,),
                token=token_fetch(),
                transport=prices_transport,
                clock=now,
            )
        except Exception:
            quote = None
        if quote is not None and quote.code == "success" and quote.facts:
            fact = quote.facts[0]
            items["quote"] = {
                "as_of": now().isoformat(),
                "last_price": fact.last_price,
                "currency": fact.currency_raw,
            }
        candles = read(
            TossReadOperation.STOCK_CANDLES,
            account=False,
            symbol=policy.symbol,
            interval="1d",
            count=30,
        )
        if candles is not None:
            items["daily_candles"] = {
                "as_of": now().isoformat(),
                "interval": "1d",
                "bars": [
                    {
                        "time": bar.timestamp.isoformat(),
                        "open": bar.open_price,
                        "high": bar.high_price,
                        "low": bar.low_price,
                        "close": bar.close_price,
                        "volume": bar.volume,
                    }
                    for bar in candles.candles
                ],
            }
        return items

    def conditional_lookup(symbol, order_id):
        """Find a group in the OPEN/CLOSED lists; "GONE" when in neither; None if unreadable."""
        for status in ("OPEN", "CLOSED"):
            page = read(
                TossReadOperation.CONDITIONAL_ORDERS,
                account=True,
                status=status,
                symbol=symbol,
                limit=100,
            )
            if page is None:
                return None
            for group in page.conditional_orders:
                if group.conditional_order_id == order_id:
                    return group
        return "GONE"

    def regular_session_state(market, now_utc):
        try:
            days = lookahead.read_days(Market(market))
        except (ValueError, CalendarLookaheadCorruptError):
            return (False, None)
        now_utc = now_utc.astimezone(UTC)
        in_regular, last_end = False, None
        for day in days:
            for session in day.sessions:
                if session.kind != "regularMarket":
                    continue
                if session.start_utc <= now_utc < session.end_utc:
                    in_regular = True
                if session.end_utc <= now_utc and (
                    last_end is None or session.end_utc > last_end
                ):
                    last_end = session.end_utc
        return (in_regular, last_end)

    capture_path = Path(root) / "ops" / "quant-inputs" / "latest.json"

    def cash_available(currency):
        """KRW/USD buying power as captured by the pulse's funds job; None when absent or stale."""
        try:
            captured = json.loads(capture_path.read_text(encoding="utf-8"))
            for item in captured.get("cash", ()):
                if (
                    item.get("account") != account_scope
                    or item.get("currency") != currency
                ):
                    continue
                observed = datetime.fromisoformat(item["observed_at"])
                if observed.tzinfo is None or now() - observed > timedelta(hours=2):
                    return None
                amount = Decimal(item["available"])
                return amount if amount.is_finite() and amount >= 0 else None
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
            return None
        return None

    def external_fills(symbol):
        """Confirmed fills the one-minute pulse already ingested for this account.

        Read-only and best-effort: the settlement decides what an unclaimed fill
        means, and a missing store must not stop a settlement that is correct.
        """
        from tokmo.records.broker_fill_feed import read_broker_fills

        try:
            return read_broker_fills(
                root / "stores" / "execution_dialogue.sqlite3",
                account_scope=account_scope,
                symbol=symbol,
            )
        except (OSError, ValueError):
            return []

    journal = OrderCommandJournal(journal_path)
    client = OrderExecutionClient(
        account_scope=account_scope,
        mode="live",
        journal=journal,
        token_provider=token_fetch,
        account_ref_provider=lambda scope: account_ref,
        clock=clock,
        transport=TossOrderCommandsLiveTransport(
            enabled=live_enabled, https_handler=https_handler
        ),
    )
    reads = RotationReads(
        order_detail=order_detail,
        conditional_detail=conditional_detail,
        holdings_quantity=holdings_quantity,
        session_verdict=session_verdict,
        evidence=evidence,
        cash_available=cash_available,
        conditional_lookup=conditional_lookup,
        regular_session_state=regular_session_state,
        external_fills=external_fills,
    )
    return RotationWiring(journal=journal, client=client, reads=reads)


def build_kis_etf_client(*, project_root, state_root, max_requests):
    """Construct the user-authorized ETF reader; construction sends no request."""
    from tokmo.facts.kis_etf_live import (
        KisLiveTransport,
        build_kis_opener,
        load_kis_credentials,
    )
    from tokmo.programs.kis_etf import KisEtfClient
    from tokmo.records.kis_state import KisState
    from tokmo.runtime.wallclock import sleep_seconds, utc_now

    return KisEtfClient(
        credentials=load_kis_credentials(Path(project_root) / ".env.kis"),
        state=KisState(state_root),
        transport=KisLiveTransport(opener=build_kis_opener()),
        clock=utc_now,
        pacer=sleep_seconds,
        max_requests=max_requests,
    )


if __name__ == "__main__":
    raise SystemExit(main())
