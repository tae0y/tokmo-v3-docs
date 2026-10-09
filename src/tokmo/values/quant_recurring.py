"""Pure values for the inactive Quant recurring cadence declaration."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from hashlib import sha256
from re import fullmatch

from tokmo.values.quant_multifactor_core import MarketV1

QUANT_RECURRING_PROGRAM_ID = "quant-recurring-shadow-v1"
QUANT_RECURRING_UNIVERSE_ID = "us-personal-holdings-v1"
QUANT_RECURRING_UNIVERSE_POLICY_VERSION = "quant-universe-us-personal-v1"
QUANT_RECURRING_ADJUSTMENT_VERSION = "raw-v1"
QUANT_ACQUISITION_NAMESPACE = "official-membership-recurring-v1"
QUANT_MARKET_CLOSE_IDENTITY_DOMAIN = "quant-market-close-schema-2"

MEMBERSHIP_FIXED_SOURCE_STARTS = 3
QUANT_CANDLE_MAX_PAGES = 2
QUANT_COHORT_STRUCTURAL_MAX = 8


def membership_plan_starts(cohort_size: int) -> int:
    if type(cohort_size) is not int or cohort_size < 0:
        raise ValueError("cohort_size")
    return MEMBERSHIP_FIXED_SOURCE_STARTS + cohort_size


def quant_due_max_increment(cohort_size: int) -> int:
    return membership_plan_starts(cohort_size) + QUANT_CANDLE_MAX_PAGES * cohort_size


class QuantDueDecisionV1(str, Enum):
    DUE = "DUE"
    NOT_POST_CLOSE = "NOT_POST_CLOSE"
    NO_COMPLETED_SESSION = "NO_COMPLETED_SESSION"
    NY_DATE_MISMATCH = "NY_DATE_MISMATCH"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NO_POST_CLOSE_OPEN_WINDOW = "NO_POST_CLOSE_OPEN_WINDOW"
    COHORT_UNPREDICTABLE = "COHORT_UNPREDICTABLE"
    MEMBERSHIP_BUDGET_UNSATISFIABLE = "MEMBERSHIP_BUDGET_UNSATISFIABLE"
    CALENDAR_BINDING_CONFLICT = "CALENDAR_BINDING_CONFLICT"
    INSUFFICIENT_DEADLINE = "INSUFFICIENT_DEADLINE"
    POLICY_ZERO = "POLICY_ZERO"


class QuantTerminalStateV1(str, Enum):
    PENDING_LEASE = "PENDING_LEASE"
    PENDING_CANDLES = "PENDING_CANDLES"
    RUNNING = "RUNNING"
    CLAIMED = "CLAIMED"  # legacy-v1 decode only
    SUCCEEDED = "SUCCEEDED"
    NO_PACK = "NO_PACK"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class QuantCloseMarketV1(str, Enum):
    KR = "KR"
    US = "US"

    @staticmethod
    def core_markets(market: "QuantCloseMarketV1") -> tuple[str, ...]:
        if market is QuantCloseMarketV1.KR:
            return (MarketV1.KR_KOSPI.value, MarketV1.KR_KOSDAQ.value)
        if market is QuantCloseMarketV1.US:
            return (MarketV1.US_NYSE_NASDAQ.value,)
        raise ValueError("market")


@dataclass(frozen=True, slots=True, init=False)
class QuantDueIdentityV1:
    market: QuantCloseMarketV1
    completed_session_date: date
    universe_revision: str
    quant_policy_version: str

    def __init__(
        self,
        *args,
        market=None,
        completed_session_date=None,
        universe_revision=None,
        quant_policy_version=None,
        **legacy,
    ):
        # Retain the inactive 300-second shadow caller while new product callers
        # use the reviewed four-field identity.
        if args:
            if len(args) != 5:
                raise TypeError("QuantDueIdentityV1")
            (
                program_id,
                quant_policy_version,
                market,
                completed_session_date,
                universe_revision,
            ) = args
            if program_id != QUANT_RECURRING_PROGRAM_ID:
                raise ValueError("program_id")
        elif legacy:
            program_id = legacy.pop("program_id", QUANT_RECURRING_PROGRAM_ID)
            quant_policy_version = legacy.pop("policy_version", quant_policy_version)
            universe_revision = legacy.pop("universe_policy_version", universe_revision)
            if legacy or program_id != QUANT_RECURRING_PROGRAM_ID:
                raise ValueError("program_id")
        if market is MarketV1.US_NYSE_NASDAQ:
            market = QuantCloseMarketV1.US
        if not isinstance(market, QuantCloseMarketV1):
            raise ValueError("market")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "completed_session_date", completed_session_date)
        object.__setattr__(self, "universe_revision", universe_revision)
        object.__setattr__(self, "quant_policy_version", quant_policy_version)
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(self.completed_session_date) is not date:
            raise ValueError("completed_session_date")
        for value, name in (
            (self.quant_policy_version, "quant_policy_version"),
            (self.universe_revision, "universe_revision"),
        ):
            if fullmatch(r"[A-Za-z0-9._:-]{1,64}", value or "") is None:
                raise ValueError(name)

    @property
    def program_id(self) -> str:
        return QUANT_RECURRING_PROGRAM_ID

    @property
    def policy_version(self) -> str:
        return self.quant_policy_version

    @property
    def universe_policy_version(self) -> str:
        return self.universe_revision

    @property
    def product_fields(self) -> tuple[str, str, str, str]:
        return (
            self.market.value,
            self.completed_session_date.isoformat(),
            self.universe_revision,
            self.quant_policy_version,
        )

    @property
    def identity_digest(self) -> str:
        payload = "|".join((QUANT_MARKET_CLOSE_IDENTITY_DOMAIN, *self.product_fields))
        return sha256(payload.encode("ascii")).hexdigest()

    @property
    def acquisition_id(self) -> str:
        return (
            f"{QUANT_ACQUISITION_NAMESPACE}:"
            f"{self.completed_session_date.isoformat()}:"
            f"{self.identity_digest[:32]}"
        )


@dataclass(frozen=True, slots=True)
class QuantDueContextV1:
    decision: QuantDueDecisionV1
    identity: QuantDueIdentityV1 | None
    completed_session_date: date | None
    ny_date: date | None
    predicted_symbols: tuple[str, ...]
    deadline_margin_seconds: int
    membership_plan_starts: int
    membership_starts_ceiling: int

    def __post_init__(self) -> None:
        if self.membership_plan_starts != membership_plan_starts(
            len(self.predicted_symbols)
        ):
            raise ValueError("due_context_plan_starts_conflict")
        if (
            type(self.membership_starts_ceiling) is not int
            or self.membership_starts_ceiling < 0
        ):
            raise ValueError("due_context_ceiling")
        if self.decision is QuantDueDecisionV1.DUE:
            if (
                self.identity is None
                or self.completed_session_date is None
                or self.ny_date is None
            ):
                raise ValueError("due_context_incomplete")
            if self.identity.completed_session_date != self.completed_session_date:
                raise ValueError("due_context_session_date_conflict")
            if self.ny_date != self.completed_session_date:
                raise ValueError("due_context_ny_date_conflict")
            if not 1 <= len(self.predicted_symbols) <= QUANT_COHORT_STRUCTURAL_MAX:
                raise ValueError("due_context_predicted_symbols")
            if self.membership_plan_starts > self.membership_starts_ceiling:
                raise ValueError("due_context_membership_budget")
        elif self.identity is not None:
            raise ValueError("non_due_context_must_not_carry_identity")


@dataclass(frozen=True, slots=True)
class QuantTickCalendarInputV1:
    us_refresh_state: str
    us_refresh_succeeded: bool
    us_verdict_open: bool
    us_verdict_reason: str
    us_in_effective_mask: bool
    mask_bound: bool
    us_cache_trade_date: date | None

    def __post_init__(self) -> None:
        if self.us_refresh_succeeded != (self.us_refresh_state == "success"):
            raise ValueError("quant_calendar_refresh_flag_conflict")
        if not self.us_refresh_succeeded and (
            self.us_verdict_open or self.us_in_effective_mask
        ):
            raise ValueError("quant_calendar_failed_refresh_open_conflict")
        if self.us_verdict_open != (self.us_verdict_reason == "open"):
            raise ValueError("quant_calendar_verdict_flag_conflict")
        if self.mask_bound and self.us_in_effective_mask != self.us_verdict_open:
            raise ValueError("quant_calendar_mask_verdict_conflict")
        if not self.mask_bound and self.us_in_effective_mask:
            raise ValueError("quant_calendar_unbound_mask_conflict")


@dataclass(frozen=True, slots=True)
class QuantSessionBindingV1:
    identity: QuantDueIdentityV1

    @property
    def session_date(self) -> date:
        return self.identity.completed_session_date
