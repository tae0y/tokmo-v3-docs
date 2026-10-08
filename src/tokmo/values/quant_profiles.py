"""Pure values for the KR/US quant market profiles (Plan/SDS 2026-08-22).

A profile is a frozen typed contract consumed by the shared orchestrator;
the registry is closed to exactly two reviewed entries. Budget formulas are
single-sourced here: US keeps the existing ``3 + N`` membership / ``3 + 3N``
increment semantics byte-for-byte, KR is exactly one STOCKS batch start
(``1`` / ``1 + 2N``). Cohort maxima are exact per-profile-version policy
(US ≤4, KR ≤5); an overflow is representable only as the typed
``PROFILE_COHORT_EXCEEDS_POLICY`` refusal with zero planned starts.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
import json
from re import fullmatch
from types import MappingProxyType
from typing import Mapping

from tokmo.values.material_pack import canonical_encode
from tokmo.values.quant_recurring import (
    MEMBERSHIP_FIXED_SOURCE_STARTS,
    QUANT_CANDLE_MAX_PAGES,
    QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    QuantTerminalStateV1,
)
from tokmo.values.session_gate import Market


QUANT_PROFILES_ACQUISITION_NAMESPACE = "quant-profiles-v1"

US_PROFILE_ID = "quant-us-nyse-nasdaq-v1"
KR_PROFILE_ID = "quant-kr-toss-membership-v1"
US_UNIVERSE_POLICY_VERSION = QUANT_RECURRING_UNIVERSE_POLICY_VERSION
KR_UNIVERSE_POLICY_VERSION = "quant-universe-kr-personal-v1"

US_MEMBERSHIP_SEMANTICS = "US_NYSE_NASDAQ_OFFICIAL_CURRENT_LISTING"
KR_MEMBERSHIP_SEMANTICS = "TOSS_OFFICIAL_CURRENT_SECURITY_MEMBERSHIP"


class QuantProfileFundamentalBindingV1(str, Enum):
    SEC_FUNDAMENTALS = "SEC_FUNDAMENTALS"
    PAUSED = "PAUSED"


class QuantProfileDueDecisionV1(str, Enum):
    DUE = "DUE"
    POLICY_ZERO = "POLICY_ZERO"
    NO_COMPLETED_SESSION = "NO_COMPLETED_SESSION"
    LOCAL_DATE_MISMATCH = "LOCAL_DATE_MISMATCH"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    CLAIM_IN_FLIGHT = "CLAIM_IN_FLIGHT"
    NO_POST_CLOSE_OPEN_WINDOW = "NO_POST_CLOSE_OPEN_WINDOW"
    COHORT_UNPREDICTABLE = "COHORT_UNPREDICTABLE"
    PROFILE_COHORT_EXCEEDS_POLICY = "PROFILE_COHORT_EXCEEDS_POLICY"
    MEMBERSHIP_BUDGET_UNSATISFIABLE = "MEMBERSHIP_BUDGET_UNSATISFIABLE"
    INSUFFICIENT_DEADLINE = "INSUFFICIENT_DEADLINE"
    CALENDAR_BINDING_CONFLICT = "CALENDAR_BINDING_CONFLICT"


@dataclass(frozen=True, slots=True)
class QuantMarketProfileV1:
    profile_id: str
    universe_policy_version: str
    market: Market
    membership_semantics: str
    cohort_max: int
    membership_fixed_source_starts: int
    membership_per_symbol_starts: int
    currency: str
    accounting_regime: str
    fundamental_binding: QuantProfileFundamentalBindingV1

    def __post_init__(self) -> None:
        if not isinstance(self.market, Market):
            raise ValueError("market")
        if type(self.cohort_max) is not int or self.cohort_max < 1:
            raise ValueError("cohort_max")
        for value in (
            self.membership_fixed_source_starts,
            self.membership_per_symbol_starts,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("membership starts formula")
        if not isinstance(
            self.fundamental_binding, QuantProfileFundamentalBindingV1
        ):
            raise ValueError("fundamental_binding")

    def membership_plan_starts(self, cohort_size: int) -> int:
        if type(cohort_size) is not int or cohort_size < 0:
            raise ValueError("cohort_size")
        return (
            self.membership_fixed_source_starts
            + self.membership_per_symbol_starts * cohort_size
        )

    def quant_due_max_increment(self, cohort_size: int) -> int:
        return (
            self.membership_plan_starts(cohort_size)
            + QUANT_CANDLE_MAX_PAGES * cohort_size
        )


US_QUANT_PROFILE_V1 = QuantMarketProfileV1(
    profile_id=US_PROFILE_ID,
    universe_policy_version=US_UNIVERSE_POLICY_VERSION,
    market=Market.US,
    membership_semantics=US_MEMBERSHIP_SEMANTICS,
    cohort_max=4,
    membership_fixed_source_starts=MEMBERSHIP_FIXED_SOURCE_STARTS,
    membership_per_symbol_starts=1,
    currency="USD",
    accounting_regime="US_GAAP",
    fundamental_binding=QuantProfileFundamentalBindingV1.SEC_FUNDAMENTALS,
)

# KR membership is exactly one STOCKS batch wire start regardless of cohort
# size; the 1..200 provider batch shape is transport capability only, never a
# product cohort allowance (§7).
KR_QUANT_PROFILE_V1 = QuantMarketProfileV1(
    profile_id=KR_PROFILE_ID,
    universe_policy_version=KR_UNIVERSE_POLICY_VERSION,
    market=Market.KR,
    membership_semantics=KR_MEMBERSHIP_SEMANTICS,
    cohort_max=5,
    membership_fixed_source_starts=1,
    membership_per_symbol_starts=0,
    currency="KRW",
    accounting_regime="K_IFRS",
    fundamental_binding=QuantProfileFundamentalBindingV1.PAUSED,
)

QUANT_PROFILE_REGISTRY = MappingProxyType({
    US_PROFILE_ID: US_QUANT_PROFILE_V1,
    KR_PROFILE_ID: KR_QUANT_PROFILE_V1,
})

# Fixed per-tick evaluation/execution order (§6.1): KR first, then US.
QUANT_PROFILE_EXECUTION_ORDER = (KR_PROFILE_ID, US_PROFILE_ID)


@dataclass(frozen=True, slots=True)
class QuantProfileDueIdentityV1:
    profile_id: str
    policy_version: str
    market: Market
    completed_session_date: date
    universe_policy_version: str

    def __post_init__(self) -> None:
        profile = QUANT_PROFILE_REGISTRY.get(self.profile_id)
        if profile is None:
            raise ValueError("profile_id")
        if self.market is not profile.market:
            raise ValueError("market")
        if self.universe_policy_version != profile.universe_policy_version:
            raise ValueError("universe_policy_version")
        if type(self.completed_session_date) is not date:
            raise ValueError("completed_session_date")
        if fullmatch(r"[A-Za-z0-9._:-]{1,64}", self.policy_version) is None:
            raise ValueError("policy_version")

    @property
    def identity_digest(self) -> str:
        payload = "|".join((
            self.profile_id,
            self.policy_version,
            self.market.value,
            self.completed_session_date.isoformat(),
            self.universe_policy_version,
        ))
        return sha256(payload.encode("ascii")).hexdigest()

    @property
    def acquisition_id(self) -> str:
        return (
            f"{QUANT_PROFILES_ACQUISITION_NAMESPACE}:"
            f"{self.profile_id}:"
            f"{self.completed_session_date.isoformat()}:"
            f"{self.identity_digest[:32]}"
        )


QUANT_PROFILE_MATERIAL_SCHEMA_VERSION = "quant-profile-material-v1"
QUANT_PROFILE_MATERIAL_MAX_BYTES = 262144

_SYMBOL_RE = r"[A-Z0-9._-]{1,32}"
_TOKEN_RE = r"[A-Z][A-Z0-9_]{0,31}"
_ISO_DATE_RE = r"\d{4}-\d{2}-\d{2}"

# Exactly the admission-relevant typed fields per profile (§9); the codec
# validates token shape only — the closed admission vocabulary is enforced by
# the profile admission gates, never here.
_MATERIAL_MEMBER_FIELDS = MappingProxyType({
    KR_PROFILE_ID: MappingProxyType({
        "symbol": _SYMBOL_RE,
        "isin_code": r"[A-Z0-9]{12}",
        "market": _TOKEN_RE,
        "security_type": _TOKEN_RE,
        "is_common_share": bool,
        "status": _TOKEN_RE,
        "currency": _TOKEN_RE,
        "list_date": _ISO_DATE_RE,
        "delist_date": _ISO_DATE_RE,
        "krx_trading_suspended": bool,
    }),
    US_PROFILE_ID: MappingProxyType({
        "symbol": _SYMBOL_RE,
        "venue": _TOKEN_RE,
        "security_type": _TOKEN_RE,
        "status": _TOKEN_RE,
        "sec_ticker_form": r"[A-Z0-9_-]{1,32}",
    }),
})

# Header fields whose values may be None (nullable member dates).
_NULLABLE_MEMBER_FIELDS = frozenset({"list_date", "delist_date"})

_MATERIAL_HEADER_KEYS = frozenset({
    "schema_version", "profile_id", "universe_policy_version",
    "policy_version", "completed_session_date", "request_identity",
    "observed_at", "members",
})
_REQUEST_IDENTITY_RE = r"[A-Za-z0-9:._|-]{1,128}"
_OBSERVED_AT_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"


def _validated_members(profile_id: str, members) -> tuple[dict, ...]:
    fields = _MATERIAL_MEMBER_FIELDS[profile_id]
    profile = QUANT_PROFILE_REGISTRY[profile_id]
    if type(members) is not tuple or not 1 <= len(members) <= profile.cohort_max:
        raise ValueError("material_members")
    rows = []
    for member in members:
        if not isinstance(member, Mapping) or set(member) != set(fields):
            raise ValueError("material_member_fields")
        row = {}
        for key, shape in fields.items():
            value = member[key]
            if shape is bool:
                if type(value) is not bool:
                    raise ValueError(f"material_member_value:{key}")
            elif value is None and key in _NULLABLE_MEMBER_FIELDS:
                pass
            elif type(value) is not str or fullmatch(shape, value) is None:
                raise ValueError(f"material_member_value:{key}")
            row[key] = value
        rows.append(row)
    symbols = [row["symbol"] for row in rows]
    if symbols != sorted(symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("material_member_order")
    return tuple(rows)


def encode_membership_material(
    *,
    profile_id: str,
    policy_version: str,
    completed_session_date: date,
    request_identity: str,
    observed_at: datetime,
    members,
) -> bytes:
    """Bounded canonical normalized membership material bytes (§9).

    Never the provider raw body: exactly the admission-relevant typed fields,
    members sorted by symbol, no prose. The sha256 over these bytes is the
    ``membership_material_digest`` bound in the profile result row.
    """
    profile = QUANT_PROFILE_REGISTRY.get(profile_id)
    if profile is None:
        raise ValueError("profile_id")
    if fullmatch(r"[A-Za-z0-9._:-]{1,64}", policy_version) is None:
        raise ValueError("policy_version")
    if type(completed_session_date) is not date:
        raise ValueError("completed_session_date")
    if (
        type(request_identity) is not str
        or fullmatch(_REQUEST_IDENTITY_RE, request_identity) is None
    ):
        raise ValueError("request_identity")
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("observed_at")
    raw = canonical_encode({
        "schema_version": QUANT_PROFILE_MATERIAL_SCHEMA_VERSION,
        "profile_id": profile_id,
        "universe_policy_version": profile.universe_policy_version,
        "policy_version": policy_version,
        "completed_session_date": completed_session_date.isoformat(),
        "request_identity": request_identity,
        "observed_at": observed_at,
        "members": _validated_members(profile_id, tuple(
            dict(member) for member in members
        )),
    })
    if len(raw) > QUANT_PROFILE_MATERIAL_MAX_BYTES:
        raise ValueError("material_oversize")
    return raw


def decode_membership_material(raw: bytes) -> dict:
    """Decode + fully re-validate material bytes; refuses non-canonical,
    oversize, or prose-bearing content fail-closed."""
    if type(raw) is not bytes or not raw:
        raise ValueError("material_bytes")
    if len(raw) > QUANT_PROFILE_MATERIAL_MAX_BYTES:
        raise ValueError("material_oversize")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("material_bytes") from None
    if type(parsed) is not dict or set(parsed) != _MATERIAL_HEADER_KEYS:
        raise ValueError("material_header")
    if parsed["schema_version"] != QUANT_PROFILE_MATERIAL_SCHEMA_VERSION:
        raise ValueError("material_schema_version")
    profile = QUANT_PROFILE_REGISTRY.get(parsed["profile_id"])
    if profile is None:
        raise ValueError("profile_id")
    if parsed["universe_policy_version"] != profile.universe_policy_version:
        raise ValueError("universe_policy_version")
    if (
        type(parsed["policy_version"]) is not str
        or fullmatch(r"[A-Za-z0-9._:-]{1,64}", parsed["policy_version"]) is None
    ):
        raise ValueError("policy_version")
    session_text = parsed["completed_session_date"]
    try:
        if (
            type(session_text) is not str
            or date.fromisoformat(session_text).isoformat() != session_text
        ):
            raise ValueError
    except ValueError:
        raise ValueError("completed_session_date") from None
    if (
        type(parsed["request_identity"]) is not str
        or fullmatch(_REQUEST_IDENTITY_RE, parsed["request_identity"]) is None
    ):
        raise ValueError("request_identity")
    if (
        type(parsed["observed_at"]) is not str
        or fullmatch(_OBSERVED_AT_RE, parsed["observed_at"]) is None
    ):
        raise ValueError("observed_at")
    members = parsed["members"]
    if type(members) is not list:
        raise ValueError("material_members")
    parsed["members"] = list(
        _validated_members(parsed["profile_id"], tuple(members))
    )
    if canonical_encode(parsed) != raw:
        raise ValueError("material_not_canonical")
    return parsed


@dataclass(frozen=True, slots=True)
class QuantProfileDueContextV1:
    profile_id: str
    decision: QuantProfileDueDecisionV1
    identity: QuantProfileDueIdentityV1 | None
    completed_session_date: date | None
    local_session_date: date | None
    predicted_symbols: tuple[str, ...]
    deadline_margin_seconds: int
    membership_plan_starts: int
    membership_starts_ceiling: int

    def __post_init__(self) -> None:
        profile = QUANT_PROFILE_REGISTRY.get(self.profile_id)
        if profile is None:
            raise ValueError("profile_id")
        if (
            type(self.membership_starts_ceiling) is not int
            or self.membership_starts_ceiling < 0
        ):
            raise ValueError("due_context_ceiling")
        if self.decision is QuantProfileDueDecisionV1.PROFILE_COHORT_EXCEEDS_POLICY:
            # Typed overflow refusal: zero planned starts, no cohort carried.
            if self.identity is not None:
                raise ValueError("non_due_context_must_not_carry_identity")
            if self.predicted_symbols != () or self.membership_plan_starts != 0:
                raise ValueError("cohort_overflow_must_plan_zero_starts")
            return
        if self.membership_plan_starts != profile.membership_plan_starts(
            len(self.predicted_symbols)
        ):
            raise ValueError("due_context_plan_starts_conflict")
        if self.decision is QuantProfileDueDecisionV1.DUE:
            if (
                self.identity is None
                or self.completed_session_date is None
                or self.local_session_date is None
            ):
                raise ValueError("due_context_incomplete")
            if self.identity.profile_id != self.profile_id:
                raise ValueError("due_context_profile_conflict")
            if self.identity.completed_session_date != self.completed_session_date:
                raise ValueError("due_context_session_date_conflict")
            if self.local_session_date != self.completed_session_date:
                raise ValueError("due_context_local_date_conflict")
            if not 1 <= len(self.predicted_symbols) <= profile.cohort_max:
                raise ValueError("due_context_predicted_symbols")
            if self.membership_plan_starts > self.membership_starts_ceiling:
                raise ValueError("due_context_membership_budget")
        elif self.identity is not None:
            raise ValueError("non_due_context_must_not_carry_identity")


def validate_material_binding(*, due, result) -> dict:
    """Exact semantic binding of the normalized material to its rows (B2).

    Decodes the persisted material and binds the typed projection — never
    the hash alone — to the due/result rows: profile, universe, policy,
    completed session date, the profile's allowed request identity, the
    exact acquisition-id namespace form, sorted symbols, and member counts.
    Member semantics per profile: KR members are the admitted members
    (count == ``admissible_count``; a truthful non-accepted terminal may
    carry exactly the admitted subset; equality with ``symbols`` whenever
    ``admissible_count == cohort_size``); US members are the observed cohort
    listings (always exactly ``symbols``). Raises ``ValueError`` with a
    typed token on any mismatch; returns the decoded projection.
    """
    decoded = decode_membership_material(result.membership_material)
    profile_id = due.profile_id
    if decoded["profile_id"] != profile_id:
        raise ValueError("material_profile_binding")
    if decoded["universe_policy_version"] != due.universe_policy_version:
        raise ValueError("material_universe_binding")
    if decoded["policy_version"] != due.policy_version:
        raise ValueError("material_policy_binding")
    if decoded["completed_session_date"] != due.completed_session_date:
        raise ValueError("material_session_binding")
    expected_acquisition = (
        f"{QUANT_PROFILES_ACQUISITION_NAMESPACE}:{profile_id}:"
        f"{due.completed_session_date}:{due.identity_digest[:32]}"
    )
    if result.acquisition_id != expected_acquisition:
        raise ValueError("material_acquisition_binding")
    expected_request = (
        f"membership:toss-stocks:{due.completed_session_date}"
        if profile_id == KR_PROFILE_ID else expected_acquisition
    )
    if decoded["request_identity"] != expected_request:
        raise ValueError("material_request_identity")
    symbols = tuple(result.symbols)
    if (
        symbols != tuple(sorted(set(symbols)))
        or len(symbols) != result.cohort_size
    ):
        raise ValueError("material_symbols_binding")
    members = tuple(row["symbol"] for row in decoded["members"])
    if profile_id == KR_PROFILE_ID:
        if len(members) != result.admissible_count:
            raise ValueError("material_admissible_binding")
        if not set(members) <= set(symbols):
            raise ValueError("material_symbols_binding")
        if result.admissible_count == result.cohort_size and members != symbols:
            raise ValueError("material_symbols_binding")
    elif members != symbols:
        raise ValueError("material_symbols_binding")
    return decoded


@dataclass(frozen=True, slots=True)
class QuantProfileAcceptanceV1:
    """Typed per-profile acceptance verdict (§5.3, §8). ``failed_clauses``
    names every violated clause; promotion requires ``passed`` exactly."""

    passed: bool
    failed_clauses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuantProfileEffectEvidenceV1:
    """Bounded zero-prohibited-effects evidence for KR acceptance (§5.3).

    The promotion-facing caller supplies it from the tick's actual
    kernel-backed wire settlement (the durable cadence wire report) plus its
    explicit effect attestation — the predicate never assumes zeros.
    ``membership_wires_settled`` / ``candle_wires_settled`` are the settled
    profile wire counts observed for the tick; the four effect counters are
    explicit alert/order/model-call/outside-profile-store-write counts.
    """

    membership_wires_settled: int
    candle_wires_settled: int
    alert_transports: int
    order_transports: int
    model_calls: int
    writes_outside_profile_store: int

    def __post_init__(self) -> None:
        for value in (
            self.membership_wires_settled, self.candle_wires_settled,
            self.alert_transports, self.order_transports, self.model_calls,
            self.writes_outside_profile_store,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("effect_evidence")


def _hex_digest(value) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _result_digests_bound(result) -> bool:
    material = getattr(result, "membership_material", None)
    return (
        _hex_digest(getattr(result, "membership_holdings_digest", None))
        and _hex_digest(getattr(result, "provenance_digest", None))
        and type(material) is bytes
        and bool(material)
        and sha256(material).hexdigest()
        == getattr(result, "membership_material_digest", None)
    )


def _material_binding_ok(due, result) -> bool:
    try:
        validate_material_binding(due=due, result=result)
    except (ValueError, TypeError, AttributeError, KeyError):
        return False
    return True


def kr_profile_acceptance_v1(
    *, due, result, evidence=None,
) -> QuantProfileAcceptanceV1:
    """PASS iff the KR evidence profile terminal is exactly compliant (§5.3).

    The terminal stays truthfully ``NO_PACK``; acceptance never claims a KR
    multifactor pack — it proves the membership/session/dedupe/candle
    evidence profile operated exactly as reviewed. The caller must supply
    :class:`QuantProfileEffectEvidenceV1`; missing or unbound evidence fails.
    """
    failed = []
    if due is None or due.state is not QuantTerminalStateV1.NO_PACK:
        failed.append("terminal_state")
    if due is None or due.reason_code != "kr_fundamentals_paused":
        failed.append("reason_code")
    if not isinstance(evidence, QuantProfileEffectEvidenceV1):
        failed.append("effect_evidence")
    elif (
        evidence.alert_transports != 0
        or evidence.order_transports != 0
        or evidence.model_calls != 0
        or evidence.writes_outside_profile_store != 0
    ):
        failed.append("prohibited_effects")
    if result is None:
        failed.extend((
            "degraded", "limitation", "admissible_count",
            "membership_starts", "candle_pages", "digests", "pack_digest",
        ))
        return QuantProfileAcceptanceV1(False, tuple(failed))
    if isinstance(evidence, QuantProfileEffectEvidenceV1) and (
        evidence.membership_wires_settled != result.membership_starts
        or evidence.candle_wires_settled
        != result.candle_page1_starts + result.candle_page2_starts
    ):
        failed.append("wire_settlement")
    if result.degraded is not True:
        failed.append("degraded")
    if result.limitations != ("KR_FUNDAMENTALS_PAUSED",):
        failed.append("limitation")
    if result.cohort_size < 1 or result.admissible_count != result.cohort_size:
        failed.append("admissible_count")
    if result.membership_starts != 1:
        failed.append("membership_starts")
    if not (
        result.candle_page1_starts == result.cohort_size
        and 0 <= result.candle_page2_starts <= result.cohort_size
        and result.candle_page1_starts + result.candle_page2_starts
        <= QUANT_CANDLE_MAX_PAGES * result.cohort_size
    ):
        failed.append("candle_pages")
    if not _result_digests_bound(result):
        failed.append("digests")
    if not _material_binding_ok(due, result):
        failed.append("material_binding")
    if result.pack_digest != "":
        failed.append("pack_digest")
    return QuantProfileAcceptanceV1(not failed, tuple(failed))


def us_profile_acceptance_v1(*, due, result) -> QuantProfileAcceptanceV1:
    """PASS iff the US profile terminal is ``SUCCEEDED`` plus its existing
    invariants (byte-equal ``3+N`` membership, ``≤2N`` candles, bound pack)."""
    failed = []
    if due is None or due.state is not QuantTerminalStateV1.SUCCEEDED:
        failed.append("terminal_state")
    if result is None:
        failed.extend((
            "membership_starts", "admissible_count", "candle_pages",
            "digests", "pack_digest",
        ))
        return QuantProfileAcceptanceV1(False, tuple(failed))
    us_profile = QUANT_PROFILE_REGISTRY[US_PROFILE_ID]
    if not (
        1 <= result.cohort_size <= us_profile.cohort_max
        and result.membership_starts
        == us_profile.membership_plan_starts(result.cohort_size)
    ):
        failed.append("membership_starts")
    if result.admissible_count != result.cohort_size:
        failed.append("admissible_count")
    if not (
        result.candle_page1_starts == result.cohort_size
        and 0 <= result.candle_page2_starts <= result.cohort_size
        and result.candle_page1_starts + result.candle_page2_starts
        <= QUANT_CANDLE_MAX_PAGES * result.cohort_size
    ):
        failed.append("candle_pages")
    if not _result_digests_bound(result):
        failed.append("digests")
    if not _material_binding_ok(due, result):
        failed.append("material_binding")
    if not _hex_digest(result.pack_digest):
        failed.append("pack_digest")
    return QuantProfileAcceptanceV1(not failed, tuple(failed))
