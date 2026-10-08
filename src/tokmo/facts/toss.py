"""Toss current-holdings parsers and request identities.

Three authorized operations exist for this source and no others: the token
request, the accounts read, and the per-account holdings read. Request
identity is expressed as a closed ``endpoint_id``; there is no method, path,
query, or body parameter anywhere in this module, so no order-capable
operation can be constructed.

No provider field name is invented. Only the names carried by the official
guide copy and by production-observed evidence are ever read, and a name
outside that set is never silently accepted as quantity or average price.

Credentials, the access token, and account sequences are carried with
``repr=False`` and live only as locals; they are never attributes of a fact.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from types import MappingProxyType

from tokmo.facts.eia import LIVE_TRANSPORT_FAILURE_CODES
from tokmo.values import (
    TOSS_SOURCE, TossAccountHoldingsFact, TossHoldingFact, UnavailableFact,
    canonical_decimal,
)
from tokmo.values.current_holdings import (
    AccountScopeCollisionError,
    AccountScopeDuplicateError,
    AccountScopeIdentityV1,
)

SOURCE = TOSS_SOURCE

# Closed operation identities. Paths live only in the transport adapters.
TOKEN_ENDPOINT_ID = "toss_oauth_token"
ACCOUNTS_ENDPOINT_ID = "toss_accounts"
HOLDINGS_ENDPOINT_ID = "toss_holdings"
DATA_ENDPOINT_IDS = frozenset({ACCOUNTS_ENDPOINT_ID, HOLDINGS_ENDPOINT_ID})

REQUEST_TIMEOUT_SECONDS = 10.0

# Official guide §5.3: the client-credentials grant, hard-coded, plus the only
# two other form keys the token request may ever carry.
GRANT_TYPE = "client_credentials"
TOKEN_FORM_KEYS = frozenset({"grant_type", "client_id", "client_secret"})
TOKEN_TYPE = "Bearer"
MAX_EXPIRES_IN_SECONDS = 30 * 24 * 3600

# Evidence-tier candidate names, most-authoritative first. Nothing else is
# ever read as an average price, a market, or a display name.
AVG_PRICE_FIELDS = ("averagePurchasePrice", "averagePrice", "avgPrice")
MARKET_FIELDS = ("market", "marketCountry")
NAME_FIELDS = ("name", "productName")
QUANTITY_FIELD = "quantity"
SYMBOL_FIELD = "symbol"
CURRENCY_FIELD = "currency"
ASSET_TYPE_FIELD = "assetType"

# Header values must stay single-line, printable ASCII and bounded.
MAX_ACCOUNT_SEQ_LENGTH = 64
MAX_TOKEN_LENGTH = 4096
# Form values are percent-encoded, so a space is harmless there; a control
# character never is, under any encoding.
MAX_CREDENTIAL_LENGTH = 512

# 403 is separated from 401 because the campaign treats them as distinct STOP
# outcomes; the rest of the closed set is the shared live-transport contract.
TOSS_TRANSPORT_FAILURE_CODES = LIVE_TRANSPORT_FAILURE_CODES | {"forbidden"}
TOSS_CAMPAIGN_CODES = TOSS_TRANSPORT_FAILURE_CODES | {
    "success", "contract_mismatch", "account_count_unexpected",
}

_SCOPE_DOMAIN = b"tokmo-v3:toss-account-scope:v1\x00"
_IDENTITY_DOMAIN = b"tokmo-v3:toss-account-identity:v1\x00"


def derive_account_scope(account_seq_decimal: str) -> str:
    digest = hashlib.sha256(_SCOPE_DOMAIN + account_seq_decimal.encode()).hexdigest()
    return "toss-acct-" + digest[:24]


def derive_raw_identity_fingerprint(account_seq_decimal: str) -> str:
    return hashlib.sha256(
        _IDENTITY_DOMAIN + account_seq_decimal.encode()
    ).hexdigest()


def derive_account_identities(account_list):
    return _derive_account_identities_from_seqs(account_list.seqs)


def _derive_account_identities_from_seqs(
        seqs, *, scope_deriver=derive_account_scope,
        fingerprint_deriver=derive_raw_identity_fingerprint):
    identities = []
    fingerprints = {}
    for ordinal, account_seq in enumerate(seqs, 1):
        scope = scope_deriver(account_seq)
        fingerprint = fingerprint_deriver(account_seq)
        if scope in fingerprints:
            if fingerprints[scope] != fingerprint:
                raise AccountScopeCollisionError("account scope collision")
            raise AccountScopeDuplicateError("duplicate account scope")
        identities.append(AccountScopeIdentityV1(scope, None, ordinal, None))
        fingerprints[scope] = fingerprint
    return tuple(identities), MappingProxyType(fingerprints)


@dataclass(frozen=True)
class TossAuthRequest:
    """Token request identity; both credential halves stay out of ``repr``."""

    endpoint_id: str
    timeout_seconds: float
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class TossDataRequest:
    """One of exactly two read operations.

    There is no method, path, query, or body parameter: ``endpoint_id`` picks
    between the accounts read and the holdings read, and the adapter refuses
    anything else before any I/O.
    """

    endpoint_id: str
    timeout_seconds: float
    token: str = field(repr=False)
    account_seq: str | None = field(repr=False, default=None)


@dataclass(frozen=True)
class TossAccessToken:
    """Memory-only bearer token; the value never reaches a repr or a record."""

    expires_in: int
    token_type: str
    value: str = field(repr=False)


class TossAccountList:
    """Provider-ordered account sequences, held as locals only.

    A plain class rather than a dataclass: no generated ``repr`` can ever be
    taught to render the sequences.
    """

    __slots__ = ("_seqs",)

    def __init__(self, seqs: tuple[str, ...]) -> None:
        self._seqs = seqs

    @property
    def seqs(self) -> tuple[str, ...]:
        return self._seqs

    def __len__(self) -> int:
        return len(self._seqs)

    def __repr__(self) -> str:
        return f"TossAccountList(count={len(self._seqs)})"


@dataclass(frozen=True)
class _TossTransportSuccess:
    """Source-private union member; the body never reaches a public repr."""

    body: bytes = field(repr=False)
    status: int = 200
    rate_limit_remaining: int | None = None


@dataclass(frozen=True)
class _TossTransportFailure:
    """Source-private union member: closed code only, nothing else."""

    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in TOSS_TRANSPORT_FAILURE_CODES:
            raise ValueError("live_transport_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


@dataclass(frozen=True)
class TossAttemptOutcome:
    """Public closed outcome of one request-start."""

    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in TOSS_CAMPAIGN_CODES:
            raise ValueError("toss_attempt_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


@dataclass(frozen=True)
class TossCampaignOutcome:
    """One capture attempt. ``facts`` is populated only on full success, so a
    stopped campaign can never hand a partial capture to a caller."""

    code: str
    stage: str
    account_count: int
    facts: tuple[TossAccountHoldingsFact, ...]
    attempts: tuple[TossAttemptOutcome, ...]
    rate_limit_remaining: int | None = None
    account_identities: tuple[AccountScopeIdentityV1, ...] = ()
    _identity_fingerprints: object = field(
        default_factory=lambda: MappingProxyType({}), repr=False,
    )


TossAuthTransport = Callable[
    [TossAuthRequest], "_TossTransportSuccess | _TossTransportFailure"]
TossDataTransport = Callable[
    [TossDataRequest], "_TossTransportSuccess | _TossTransportFailure"]
Clock = Callable[[], datetime]
BeforeStart = Callable[[str, "int | None"], None]
Checkpoint = Callable[[int], None]


def build_auth_request(*, client_id: str, client_secret: str) -> TossAuthRequest:
    return TossAuthRequest(
        endpoint_id=TOKEN_ENDPOINT_ID, timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        client_id=client_id, client_secret=client_secret,
    )


def build_accounts_request(*, token: str) -> TossDataRequest:
    """The accounts read carries no account header, hence no account sequence."""
    return TossDataRequest(
        endpoint_id=ACCOUNTS_ENDPOINT_ID, timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=token, account_seq=None,
    )


def build_holdings_request(*, token: str, account_seq: str) -> TossDataRequest:
    return TossDataRequest(
        endpoint_id=HOLDINGS_ENDPOINT_ID, timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=token, account_seq=account_seq,
    )


def valid_account_seq(value) -> bool:
    """Bounded single-line printable ASCII, safe as a header value."""
    return (isinstance(value, str) and value.isascii() and value.isprintable()
            and 0 < len(value) <= MAX_ACCOUNT_SEQ_LENGTH and " " not in value)


def valid_token(value) -> bool:
    return (isinstance(value, str) and value.isascii() and value.isprintable()
            and 0 < len(value) <= MAX_TOKEN_LENGTH and " " not in value)


def valid_credential(value) -> bool:
    return (isinstance(value, str) and value.isascii() and value.isprintable()
            and 0 < len(value) <= MAX_CREDENTIAL_LENGTH)


def parse_token_response(body: bytes, *,
                         observed_at: datetime) -> TossAccessToken | UnavailableFact:
    """``access_token``/``expires_in`` are required; ``token_type`` is optional
    but must be Bearer when present (production-observed default)."""
    payload = _load(body)
    if payload is None:
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if not isinstance(payload, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    value = payload.get("access_token")
    if not valid_token(value):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    expires_in = _bounded_seconds(payload.get("expires_in"))
    if expires_in is None:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    token_type = payload.get("token_type", TOKEN_TYPE)
    if not isinstance(token_type, str) or token_type.lower() != TOKEN_TYPE.lower():
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    return TossAccessToken(expires_in=expires_in, token_type=token_type, value=value)


def parse_accounts_response(body: bytes, *,
                            observed_at: datetime) -> TossAccountList | UnavailableFact:
    """``result`` is the account list; ``accountSeq`` is the only field read.

    The display account number is never consumed: it is not an account
    sequence and this tracer has no use for it.
    """
    payload = _load(body)
    if payload is None:
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if not isinstance(payload, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    rows = payload.get("result")
    if not isinstance(rows, list):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    seqs = []
    for row in rows:
        if not isinstance(row, dict):
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        raw = row.get("accountSeq")
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        seq = str(raw).strip()
        if not valid_account_seq(seq):
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        seqs.append(seq)
    return TossAccountList(tuple(seqs))


def parse_holdings_response(
        body: bytes, *, account_ordinal: int, account_count: int,
        captured_at: datetime) -> TossAccountHoldingsFact | UnavailableFact:
    """``result.items`` is the holdings list.

    Every item must supply a symbol, a quantity, and an average price under
    one of the evidenced names; a single unusable item invalidates the whole
    account response, because a partly-parsed account is not a snapshot.
    """
    payload = _load(body)
    if payload is None:
        return UnavailableFact(SOURCE, "parse_error", captured_at)
    if not isinstance(payload, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", captured_at)
    result = payload.get("result")
    if not isinstance(result, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", captured_at)
    items = result.get("items")
    if not isinstance(items, list):
        return UnavailableFact(SOURCE, "contract_mismatch", captured_at)

    holdings = []
    for item in items:
        holding = _parse_holding(item)
        if holding is None:
            return UnavailableFact(SOURCE, "contract_mismatch", captured_at)
        holdings.append(holding)
    return TossAccountHoldingsFact(
        source=SOURCE, captured_at=captured_at, account_ordinal=account_ordinal,
        account_count=account_count, holdings=tuple(holdings),
    )


def _parse_holding(item) -> TossHoldingFact | None:
    if not isinstance(item, dict):
        return None
    symbol = item.get(SYMBOL_FIELD)
    if not isinstance(symbol, str) or not symbol:
        return None
    quantity = _canonical_amount(item.get(QUANTITY_FIELD))
    if quantity is None:
        return None
    avg_price = _first_amount(item, AVG_PRICE_FIELDS)
    if avg_price is None:
        return None

    currency = _optional_text(item, (CURRENCY_FIELD,))
    market = _optional_text(item, MARKET_FIELDS)
    name = _optional_text(item, NAME_FIELDS)
    asset_type = _optional_text(item, (ASSET_TYPE_FIELD,))
    if currency is False or market is False or name is False or asset_type is False:
        return None

    return TossHoldingFact(
        symbol=symbol, quantity=quantity,
        avg_price=avg_price[0], avg_price_field=avg_price[1],
        currency=None if currency is None else currency[0],
        market_raw=None if market is None else market[0],
        market_field=None if market is None else market[1],
        name=None if name is None else name[0],
        asset_type=None if asset_type is None else asset_type[0],
    )


def _first_amount(item: dict, names: tuple[str, ...]) -> tuple[str, str] | None:
    """The first evidenced name actually present supplies the value; a name
    outside ``names`` is never consulted."""
    for name in names:
        if item.get(name) is None:
            continue
        amount = _canonical_amount(item[name])
        return None if amount is None else (amount, name)
    return None


def _optional_text(item: dict, names: tuple[str, ...]):
    """``None`` when absent, ``False`` when present but not usable text."""
    for name in names:
        if item.get(name) is None:
            continue
        value = item[name]
        if not isinstance(value, str) or not value:
            return False
        return (value, name)
    return None


def _canonical_amount(raw) -> str | None:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return canonical_decimal(value)


def _bounded_seconds(raw) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    return seconds if 0 < seconds <= MAX_EXPIRES_IN_SECONDS else None


def query_toss_holdings(*, client_id: str, client_secret: str, max_accounts: int,
                        auth_transport: TossAuthTransport,
                        data_transport: TossDataTransport, clock: Clock,
                        before_start: BeforeStart,
                        checkpoint: Checkpoint) -> TossCampaignOutcome:
    """Run one bounded capture: token, accounts, then one read per account.

    The credentials, the token, and the account sequences exist only as locals
    of this frame. ``before_start`` is called immediately before every
    request-start so the caller can durably record it and pace it, and it may
    refuse by raising; ``checkpoint`` gates the per-account tranche once the
    account count is known. There is no retry anywhere: one attempt per
    planned start, and the first failure ends the capture with nothing parsed.
    Identity derivation may raise ``AccountScopeDuplicateError`` or
    ``AccountScopeCollisionError`` for an invalid in-frame account identity set.
    """
    captured_at = clock()
    attempts: list[TossAttemptOutcome] = []
    observed_remaining: list[int] = []

    before_start("auth", None)
    reply = _send(auth_transport,
                  build_auth_request(client_id=client_id, client_secret=client_secret),
                  attempts, observed_remaining)
    if reply is None:
        return _stopped("auth", attempts, 0, observed_remaining)
    token = parse_token_response(reply.body, observed_at=captured_at)
    del reply
    if isinstance(token, UnavailableFact):
        return _mismatched("auth", attempts, 0, observed_remaining)

    before_start("accounts", None)
    reply = _send(data_transport, build_accounts_request(token=token.value),
                  attempts, observed_remaining)
    if reply is None:
        return _stopped("accounts", attempts, 0, observed_remaining)
    accounts = parse_accounts_response(reply.body, observed_at=captured_at)
    del reply
    if isinstance(accounts, UnavailableFact):
        return _mismatched("accounts", attempts, 0, observed_remaining)

    count = len(accounts)
    if not 1 <= count <= max_accounts:
        return TossCampaignOutcome(
            code="account_count_unexpected", stage="accounts", account_count=count,
            facts=(), attempts=tuple(attempts),
            rate_limit_remaining=_last(observed_remaining))
    checkpoint(count)
    account_identities, identity_fingerprints = derive_account_identities(accounts)

    facts = []
    for ordinal, account_seq in enumerate(accounts.seqs, 1):
        before_start("holdings", ordinal)
        reply = _send(data_transport,
                      build_holdings_request(token=token.value, account_seq=account_seq),
                      attempts, observed_remaining)
        if reply is None:
            return _stopped("holdings", attempts, count, observed_remaining)
        fact = parse_holdings_response(
            reply.body, account_ordinal=ordinal, account_count=count,
            captured_at=captured_at)
        del reply
        if isinstance(fact, UnavailableFact):
            return _mismatched("holdings", attempts, count, observed_remaining)
        facts.append(fact)

    del token, accounts
    return TossCampaignOutcome(
        code="success", stage="holdings", account_count=count, facts=tuple(facts),
        attempts=tuple(attempts), rate_limit_remaining=_last(observed_remaining),
        account_identities=account_identities,
        _identity_fingerprints=identity_fingerprints)


def _send(transport, request, attempts: list, observed_remaining: list):
    """One attempt, never more; an escaping exception is sanitized to a code."""
    try:
        outcome = transport(request)
    except Exception:
        outcome = None
    if isinstance(outcome, _TossTransportSuccess):
        attempts.append(TossAttemptOutcome(code="success"))
        if outcome.rate_limit_remaining is not None:
            observed_remaining.append(outcome.rate_limit_remaining)
        return outcome
    if isinstance(outcome, _TossTransportFailure):
        attempts.append(TossAttemptOutcome(code=outcome.code,
                                           retry_after=outcome.retry_after))
        return None
    attempts.append(TossAttemptOutcome(code="protocol_failure"))
    return None


def _stopped(stage: str, attempts: list, count: int,
             observed_remaining: list) -> TossCampaignOutcome:
    return TossCampaignOutcome(
        code=attempts[-1].code, stage=stage, account_count=count, facts=(),
        attempts=tuple(attempts), rate_limit_remaining=_last(observed_remaining))


def _mismatched(stage: str, attempts: list, count: int,
                observed_remaining: list) -> TossCampaignOutcome:
    """The wire call succeeded but its body is not the contracted shape."""
    attempts[-1] = TossAttemptOutcome(code="contract_mismatch")
    return _stopped(stage, attempts, count, observed_remaining)


def _last(values: list):
    return values[-1] if values else None


def _load(body: bytes):
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


__all__ = [
    "ACCOUNTS_ENDPOINT_ID", "AVG_PRICE_FIELDS", "GRANT_TYPE",
    "HOLDINGS_ENDPOINT_ID", "MARKET_FIELDS", "NAME_FIELDS",
    "REQUEST_TIMEOUT_SECONDS", "SOURCE", "TOKEN_ENDPOINT_ID",
    "TOKEN_FORM_KEYS", "TOSS_CAMPAIGN_CODES", "TOSS_TRANSPORT_FAILURE_CODES",
    "TossAccessToken", "TossAccountList", "TossAttemptOutcome",
    "TossAuthRequest", "TossCampaignOutcome", "TossDataRequest",
    "build_accounts_request", "build_auth_request", "build_holdings_request",
    "parse_accounts_response", "parse_holdings_response", "parse_token_response",
    "query_toss_holdings", "valid_account_seq", "valid_credential", "valid_token",
]
