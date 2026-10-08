"""Pure authority, execution and rendering seams for Quant market close."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from typing import Callable, Mapping

from tokmo.values.ops_kernel import AlertOutboxV1, ProducerRole
from tokmo.values.quant_recurring import QuantCloseMarketV1, QuantDueIdentityV1


@dataclass(frozen=True, slots=True)
class OfficialCloseEvidenceV1:
    market: str
    session_date: date
    official_close_utc: datetime | None
    verdict: str
    source_identity: str

    def __post_init__(self):
        if self.market not in {"KR", "US"} or self.verdict not in {"regular", "no_regular"}:
            raise ValueError("authority")
        if self.verdict == "regular":
            if (not isinstance(self.official_close_utc, datetime)
                    or self.official_close_utc.tzinfo is None
                    or self.official_close_utc.utcoffset() is None):
                raise ValueError("official_close")
        elif self.official_close_utc is not None:
            raise ValueError("holiday_close")
        expected = f"calendar:{self.market}:{self.session_date.isoformat()}:"
        if not self.source_identity.startswith(expected):
            raise ValueError("provenance")


@dataclass(frozen=True, slots=True)
class DueResolutionV1:
    decision: str
    evidence: OfficialCloseEvidenceV1 | None


def resolve_due(observed_now_utc: datetime,
                evidence: OfficialCloseEvidenceV1 | None) -> DueResolutionV1:
    if observed_now_utc.tzinfo is None or observed_now_utc.utcoffset() is None:
        raise ValueError("observed_now_utc")
    if evidence is None or evidence.verdict != "regular":
        return DueResolutionV1("CALENDAR_AUTHORITY_UNAVAILABLE", None)
    if observed_now_utc.astimezone(timezone.utc) < evidence.official_close_utc.astimezone(timezone.utc):
        return DueResolutionV1("BEFORE_CLOSE", evidence)
    return DueResolutionV1("DUE", evidence)


def completed_candles_ready(session_date: date, candle_dates) -> bool:
    """A valid response is ready only when every member has the completed bar."""
    values = tuple(candle_dates)
    return bool(values) and all(value == session_date for value in values)


def render_quant_market_close_alert(result: dict) -> tuple[str, ...]:
    exact = {"market", "completed_session_date", "universe_revision",
             "quant_policy_version", "ranked", "limitations", "official_close_source"}
    if type(result) is not dict or set(result) != exact:
        raise ValueError("result_shape")
    ranked = tuple(sorted(str(value) for value in result["ranked"]))
    limitations = tuple(sorted(str(value) for value in result["limitations"]))
    lines = (
        f"Quant market close — {result['market']} {result['completed_session_date']}",
        f"universe={result['universe_revision']} policy={result['quant_policy_version']}",
        "ranked=" + (", ".join(ranked) if ranked else "none"),
        "limitations=" + (", ".join(limitations) if limitations else "none"),
        f"official_close_source={result['official_close_source']}",
    )
    encoded = "\n".join(lines).encode("utf-8")
    lowered = encoded.lower()
    if len(encoded) > 4096 or any(marker in lowered for marker in
            (b"credential", b"/users/", b"account", b"private_holding")):
        raise ValueError("render_privacy")
    return lines


def canonical_result_json(result: dict) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass
class QuantMarketCloseRunnerDepsV1:
    """Narrow fixture-injectable composition; construction has no effects."""
    activation_reader: Callable
    authority_reader: Callable
    receipt_store: object
    operation_lease: Callable
    candle_reader: Callable
    core_callable: Callable
    kernel_store: object
    universe_revisions: Mapping[str, str]
    quant_policy_version: str
    tick_id: int
    invocation_id: str
    crash_injector: Callable | None = None


def _crash(deps, point):
    if deps.crash_injector is not None:
        deps.crash_injector(point)


def _result(identity, evidence, core_result):
    return {
        "market": identity.market.value,
        "completed_session_date": identity.completed_session_date.isoformat(),
        "universe_revision": identity.universe_revision,
        "quant_policy_version": identity.quant_policy_version,
        "ranked": core_result["ranked"],
        "limitations": core_result["limitations"],
        "official_close_source": evidence.source_identity,
    }


def _bind_terminal(identity, row, deps, observed_now_utc):
    """Complete an interrupted terminal->kernel->binding sequence, wire-zero."""
    if row.state not in {"SUCCEEDED", "NO_PACK"} or row.outbox_state == "OUTBOX_BOUND":
        return
    result = json.loads(row.result_json)
    lines = render_quant_market_close_alert(result)
    outbox = AlertOutboxV1.for_quant_market_close(
        alert_id=row.outbox_id, tick_id=deps.tick_id,
        subject_id=identity.identity_digest, body_evidence=lines,
        created_at=observed_now_utc,
    )
    deps.kernel_store.record_quant_market_close_outbox(ProducerRole.ANALYSIS, outbox)
    _crash(deps, "KERNEL_INSERT")
    deps.receipt_store.bind_outbox(identity, row.outbox_id)
    _crash(deps, "BINDING")


def run_quant_market_close(observed_now_utc: datetime,
                           deps: QuantMarketCloseRunnerDepsV1):
    """Run independent KR/US market-close lanes with no internal retry."""
    modes = deps.activation_reader(observed_now_utc)
    outcomes = []
    for market in ("KR", "US"):
        if modes.get(market, "OFF") == "OFF":
            outcomes.append((market, "OFF")); continue
        evidence = deps.authority_reader(market, observed_now_utc)
        due = resolve_due(observed_now_utc, evidence)
        if due.decision != "DUE":
            outcomes.append((market, due.decision)); continue
        identity = QuantDueIdentityV1(
            market=QuantCloseMarketV1(market),
            completed_session_date=evidence.session_date,
            universe_revision=deps.universe_revisions[market],
            quant_policy_version=deps.quant_policy_version,
        )
        row = deps.receipt_store.read(identity)
        if row is not None and row.state in {"RUNNING", "SUCCEEDED", "NO_PACK", "FAILED", "UNKNOWN"}:
            if row.state != "RUNNING":
                _bind_terminal(identity, row, deps, observed_now_utc)
            outcomes.append((market, row.state)); continue
        deps.receipt_store.ensure_pending(identity, state="PENDING_LEASE")
        lease = deps.operation_lease(("HOLDINGS", "STOCK_CANDLES_DAILY"))
        if lease is None:
            outcomes.append((market, "PENDING_LEASE")); continue
        try:
            candle_dates = deps.candle_reader(identity)
            if not completed_candles_ready(evidence.session_date, candle_dates):
                deps.receipt_store.set_pending(identity, state="PENDING_CANDLES")
                outcomes.append((market, "PENDING_CANDLES")); continue
            if not deps.receipt_store.claim(identity, invocation_id=deps.invocation_id):
                row = deps.receipt_store.read(identity)
                outcomes.append((market, row.state)); continue
            _crash(deps, "RUNNING")
            try:
                core = deps.core_callable(identity, candle_dates)
                terminal = "NO_PACK" if core is None else "SUCCEEDED"
                core = core or {"ranked": [], "limitations": ["no_pack"]}
                result = _result(identity, evidence, core)
            except Exception as error:
                deps.receipt_store.finish(identity, state="FAILED",
                    result_json=canonical_result_json({"failure": type(error).__name__}))
                outcomes.append((market, "FAILED")); continue
            outbox_id = "qmc:" + identity.identity_digest
            deps.receipt_store.finish(identity, state=terminal,
                result_json=canonical_result_json(result), outbox_id=outbox_id)
            _crash(deps, "TERMINAL")
            _bind_terminal(identity, deps.receipt_store.read(identity), deps,
                           observed_now_utc)
            outcomes.append((market, terminal))
        finally:
            lease.close()
    return tuple(outcomes)
