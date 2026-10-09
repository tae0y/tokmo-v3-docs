"""Complete holdings snapshot bundle: completeness invariants and canonical
serialization/hash.

One snapshot is the sole holdings write/read unit: one complete capture
across every discovered account at one shared instant. These tests pin the
completeness validation (SDS §3.1, plan §3.2) and the canonical bytes /
content-hash contract (SDS §5) including the pinned golden vectors. All
values are synthetic.
"""

import dataclasses
import hashlib
from datetime import UTC, datetime

import pytest

from tokmo.records.holdings_persistence import authorize_user_request
from tokmo.records.holdings_snapshots import (
    HoldingsSnapshotStore,
    canonical_snapshot_bytes,
    canonical_utc_isoformat,
    legacy_snapshot_id,
    snapshot_content_hash,
)
from tokmo.values import (
    TOSS_SOURCE,
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    HoldingsSnapshotHeader,
    TossAccountHoldingsFact,
    TossHoldingFact,
)

UUID4_HEX = "16fd27068baf433b82eb8c7fada847da"
PLACEHOLDER_HASH = "0" * 64

CAPTURED_AT = datetime(2026, 7, 28, 0, 0, 0, tzinfo=UTC)


def holding(symbol="AAPL", quantity="10", avg_price="150.5", name="Apple Inc."):
    return TossHoldingFact(
        symbol=symbol,
        quantity=quantity,
        avg_price=avg_price,
        avg_price_field="averagePurchasePrice",
        currency="USD",
        market_raw="US",
        market_field="market",
        name=name,
        asset_type="STOCK",
    )


def snapshot(
    accounts,
    *,
    source="toss-holdings-v1",
    captured_at=CAPTURED_AT,
    count=None,
    content_hash=PLACEHOLDER_HASH,
):
    return CompleteHoldingsSnapshot(
        snapshot_id=UUID4_HEX,
        source=source,
        captured_at=captured_at,
        discovered_account_count=len(accounts) if count is None else count,
        accounts=tuple(accounts),
        content_hash=content_hash,
    )


def test_contiguous_ordinals():
    valid = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=()),
        ]
    )
    assert valid.discovered_account_count == 2

    for ordinals in ((2, 1), (1, 3), (0, 1), (1, 1)):
        with pytest.raises(ValueError):
            snapshot(
                [
                    HoldingsSnapshotAccount(account_ordinal=ordinal, holdings=())
                    for ordinal in ordinals
                ]
            )


def test_count_mismatch_rejected():
    accounts = [HoldingsSnapshotAccount(account_ordinal=1, holdings=())]
    with pytest.raises(ValueError):
        snapshot(accounts, count=2)
    with pytest.raises(ValueError):
        snapshot(accounts, count=0)


def test_bool_ordinal_rejected():
    """bool is an int subclass, so True would otherwise slip through the
    contiguity check as ordinal 1; the integer contract rejects it
    explicitly."""
    with pytest.raises(ValueError, match="holdings_snapshot_account_ordinal_bool"):
        HoldingsSnapshotAccount(account_ordinal=True, holdings=())


def test_bool_account_count_rejected():
    """True == 1 would otherwise satisfy the count-match check for a
    single-account capture."""
    accounts = [HoldingsSnapshotAccount(account_ordinal=1, holdings=())]
    with pytest.raises(ValueError, match="holdings_snapshot_count_bool"):
        snapshot(accounts, count=True)


def test_list_holdings_rejected():
    """A frozen value must not hold a caller-mutable list: holdings is
    required to be an exact tuple, never silently coerced."""
    with pytest.raises(ValueError, match="holdings_snapshot_holdings_not_tuple"):
        HoldingsSnapshotAccount(account_ordinal=1, holdings=[holding()])


def test_list_accounts_rejected():
    """accounts is likewise required to be an exact tuple."""
    account = HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(),))
    with pytest.raises(ValueError, match="holdings_snapshot_accounts_not_tuple"):
        CompleteHoldingsSnapshot(
            snapshot_id=UUID4_HEX,
            source="toss-holdings-v1",
            captured_at=CAPTURED_AT,
            discovered_account_count=1,
            accounts=[account],
            content_hash=PLACEHOLDER_HASH,
        )


def test_zero_accounts_rejected():
    """N == 0 is a source-contract stop, never an empty complete snapshot."""
    with pytest.raises(ValueError):
        snapshot([])


def test_duplicate_symbol_in_account_rejected():
    with pytest.raises(ValueError):
        HoldingsSnapshotAccount(
            account_ordinal=1,
            holdings=(holding(symbol="AAPL"), holding(symbol="AAPL")),
        )
    # The same symbol in two different accounts stays valid.
    snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=(holding(),)),
        ]
    )


def test_timestamp_source_mismatch_rejected():
    accounts = [HoldingsSnapshotAccount(account_ordinal=1, holdings=())]
    with pytest.raises(ValueError):
        snapshot(accounts, source="eia_wti_rwtc")
    with pytest.raises(ValueError):
        snapshot(accounts, captured_at=datetime(2026, 7, 28, 0, 0, 0))  # naive


def test_empty_account_allowed():
    """An account discovered with zero holdings is a valid, complete part of
    the capture."""
    value = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=()),
        ]
    )
    assert value.accounts[1].holdings == ()


def test_no_durable_account_fields():
    """No account sequence/number/alias/HMAC vocabulary exists in the value
    types: the only per-account field beyond holdings is the capture-local
    ordinal."""
    account_fields = {f.name for f in dataclasses.fields(HoldingsSnapshotAccount)}
    assert account_fields == {"account_ordinal", "holdings"}

    snapshot_fields = {f.name for f in dataclasses.fields(CompleteHoldingsSnapshot)}
    assert snapshot_fields == {
        "snapshot_id",
        "source",
        "captured_at",
        "discovered_account_count",
        "accounts",
        "content_hash",
    }

    header_fields = {f.name for f in dataclasses.fields(HoldingsSnapshotHeader)}
    assert header_fields == {
        "snapshot_id",
        "source",
        "captured_at",
        "discovered_account_count",
        "content_hash",
    }

    banned = ("number", "sequence", "seq", "alias", "hmac", "encrypted")
    for name in account_fields | snapshot_fields | header_fields:
        for fragment in banned:
            assert fragment not in name.lower(), name


PROHIBITED_REPR_TOKENS = frozenset(
    {
        "symbol=",
        "quantity=",
        "avg_price=",
        "name=",
        "account_ordinal=",
        "holdings=",
        "snapshot_id=",
        "content_hash=",
        "authorized_at=",
        "reason=",
        "stored_at=",
    }
)


def test_snapshot_repr_redaction():
    value = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=()),
        ]
    )
    header = HoldingsSnapshotHeader(
        snapshot_id=UUID4_HEX,
        source="toss-holdings-v1",
        captured_at=CAPTURED_AT,
        discovered_account_count=2,
        content_hash=PLACEHOLDER_HASH,
    )
    for rendered in (repr(value), repr(value.accounts[0]), repr(header)):
        for token in PROHIBITED_REPR_TOKENS:
            assert token not in rendered, (token, rendered)
        assert UUID4_HEX not in rendered
        assert "AAPL" not in rendered
        assert "150.5" not in rendered
        assert "Apple" not in rendered
    # The safe aggregate fields stay visible on the bundle and header.
    assert "discovered_account_count=2" in repr(value)
    assert "source='toss-holdings-v1'" in repr(header)


# --- Canonical serialization / hash (SDS §5, pinned golden vectors §5.8) ---

CANONICAL_BYTES_CAPTURE_1 = (
    b'{"source":"toss-holdings-v1",'
    b'"captured_at":"2026-07-28T00:00:00.000000+00:00",'
    b'"discovered_account_count":2,'
    b'"accounts":['
    b'{"account_ordinal":1,"holdings":['
    b'{"symbol":"AAPL","quantity":"10","avg_price":"150.5",'
    b'"avg_price_field":"averagePurchasePrice","currency":"USD",'
    b'"market_raw":"US","market_field":"market","name":"Apple Inc.",'
    b'"asset_type":"STOCK"}'
    b"]},"
    b'{"account_ordinal":2,"holdings":[]}'
    b"]}"
)

CONTENT_HASH_CAPTURE_1 = (
    "6eb2d30a92f8d66cb9857f4b01adaa766562f9d543cd0b4cc78aa6ec2d3df6ec"
)

LEGACY_SNAPSHOT_ID_CAPTURE_1 = (
    "5cafa5c45986675183f3185088e415f6995b34ae68fbece140ed80ba9b351ccc"
)

CANONICAL_BYTES_CAPTURE_2 = (
    b'{"source":"toss-holdings-v1",'
    b'"captured_at":"2026-07-28T06:00:00.000000+00:00",'
    b'"discovered_account_count":2,'
    b'"accounts":['
    b'{"account_ordinal":1,"holdings":['
    b'{"symbol":"AAPL","quantity":"10","avg_price":"150.5",'
    b'"avg_price_field":"averagePurchasePrice","currency":"USD",'
    b'"market_raw":"US","market_field":"market","name":"Apple Inc.",'
    b'"asset_type":"STOCK"},'
    b'{"symbol":"GOOGL","quantity":"5","avg_price":"2800",'
    b'"avg_price_field":"averagePurchasePrice","currency":"USD",'
    b'"market_raw":"US","market_field":"market","name":"Alphabet Inc.",'
    b'"asset_type":"STOCK"}'
    b"]},"
    b'{"account_ordinal":2,"holdings":['
    b'{"symbol":"MSFT","quantity":"20","avg_price":"380.25",'
    b'"avg_price_field":"averagePurchasePrice","currency":"USD",'
    b'"market_raw":"US","market_field":"market","name":"Microsoft Corp.",'
    b'"asset_type":"STOCK"}'
    b"]}"
    b"]}"
)

CONTENT_HASH_CAPTURE_2 = (
    "8b0c5b7afe0479ebf9722c877b879e380d686a4ee483f92c6d402b22cdd03806"
)

LEGACY_SNAPSHOT_ID_CAPTURE_2 = (
    "cb4a61363326b55da3e593668b0dd4ab5468243123d87065a3bdc4b294493ba1"
)


def capture_1():
    return snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=()),
        ]
    )


def capture_2():
    return snapshot(
        [
            HoldingsSnapshotAccount(
                account_ordinal=1,
                holdings=(
                    holding(),
                    holding(
                        symbol="GOOGL",
                        quantity="5",
                        avg_price="2800",
                        name="Alphabet Inc.",
                    ),
                ),
            ),
            HoldingsSnapshotAccount(
                account_ordinal=2,
                holdings=(
                    holding(
                        symbol="MSFT",
                        quantity="20",
                        avg_price="380.25",
                        name="Microsoft Corp.",
                    ),
                ),
            ),
        ],
        captured_at=datetime(2026, 7, 28, 6, 0, 0, tzinfo=UTC),
    )


def test_migration_parity():
    """The serializer reproduces both pinned canonical byte literals, and the
    domain-separated digest of each equals the pinned legacy snapshot ID."""
    bytes_1 = canonical_snapshot_bytes(capture_1())
    assert bytes_1 == CANONICAL_BYTES_CAPTURE_1
    assert snapshot_content_hash(capture_1()) == CONTENT_HASH_CAPTURE_1
    assert legacy_snapshot_id(bytes_1) == LEGACY_SNAPSHOT_ID_CAPTURE_1

    bytes_2 = canonical_snapshot_bytes(capture_2())
    assert bytes_2 == CANONICAL_BYTES_CAPTURE_2
    assert snapshot_content_hash(capture_2()) == CONTENT_HASH_CAPTURE_2
    assert legacy_snapshot_id(bytes_2) == LEGACY_SNAPSHOT_ID_CAPTURE_2


def test_stable_ordering():
    """The same capture always produces the same bytes and hash: holdings
    supplied in any provider order are canonicalized by symbol."""
    googl = holding(
        symbol="GOOGL", quantity="5", avg_price="2800", name="Alphabet Inc."
    )
    ordered = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(), googl)),
        ]
    )
    reversed_input = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(googl, holding())),
        ]
    )
    assert canonical_snapshot_bytes(ordered) == canonical_snapshot_bytes(reversed_input)
    assert snapshot_content_hash(ordered) == snapshot_content_hash(reversed_input)


def test_exact_decimal_serialization():
    """Numerically equal Decimal spellings serialize identically: trailing
    zeros drop and scientific notation expands via canonical_decimal."""
    plain = snapshot(
        [
            HoldingsSnapshotAccount(
                account_ordinal=1, holdings=(holding(quantity="10", avg_price="78.42"),)
            )
        ]
    )
    trailing_zero = snapshot(
        [
            HoldingsSnapshotAccount(
                account_ordinal=1,
                holdings=(holding(quantity="10.0", avg_price="78.420"),),
            )
        ]
    )
    scientific = snapshot(
        [
            HoldingsSnapshotAccount(
                account_ordinal=1,
                holdings=(holding(quantity="1E+1", avg_price="7.842E+1"),),
            )
        ]
    )
    assert (
        canonical_snapshot_bytes(plain)
        == canonical_snapshot_bytes(trailing_zero)
        == canonical_snapshot_bytes(scientific)
    )
    assert (
        snapshot_content_hash(plain)
        == snapshot_content_hash(trailing_zero)
        == snapshot_content_hash(scientific)
    )


def test_single_field_change_alters_hash():
    base = capture_1()
    changed_quantity = snapshot(
        [
            HoldingsSnapshotAccount(
                account_ordinal=1, holdings=(holding(quantity="11"),)
            ),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=()),
        ]
    )
    changed_time = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding(),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=()),
        ],
        captured_at=datetime(2026, 7, 28, 0, 0, 1, tzinfo=UTC),
    )
    hashes = {
        snapshot_content_hash(base),
        snapshot_content_hash(changed_quantity),
        snapshot_content_hash(changed_time),
    }
    assert len(hashes) == 3


def test_reconstruction_equality():
    """Freshly reconstructed value objects with the same captured semantics
    produce byte-identical canonical serialization and the same hash, so a
    read-back bundle re-hashes to its append hash."""
    original = capture_1()
    reconstructed = CompleteHoldingsSnapshot(
        snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4",  # ID is not hashed
        source=original.source,
        captured_at=original.captured_at,
        discovered_account_count=original.discovered_account_count,
        accounts=tuple(
            HoldingsSnapshotAccount(
                account_ordinal=account.account_ordinal,
                holdings=tuple(
                    TossHoldingFact(**dataclasses.asdict(item))
                    for item in account.holdings
                ),
            )
            for account in original.accounts
        ),
        content_hash=snapshot_content_hash(original),
    )
    assert canonical_snapshot_bytes(reconstructed) == canonical_snapshot_bytes(original)
    assert snapshot_content_hash(reconstructed) == reconstructed.content_hash


def test_canonical_utc_isoformat_fixed_form():
    """Any aware input tzinfo canonicalizes to the same exact UTC string
    with six-digit microseconds and the +00:00 suffix."""
    from datetime import timedelta
    from datetime import timezone as tz

    kst = tz(timedelta(hours=9))
    assert (
        canonical_utc_isoformat(datetime(2026, 7, 28, 9, 0, 0, tzinfo=kst))
        == "2026-07-28T00:00:00.000000+00:00"
    )
    assert canonical_utc_isoformat(CAPTURED_AT) == "2026-07-28T00:00:00.000000+00:00"


def test_golden_vector_roundtrip():
    """Both pinned canonical bytes hash to the pinned content_hash, and
    prefix-hashed with the legacy domain separator produce the pinned
    legacy snapshot IDs (SDS §5.8, executable form)."""
    assert (
        hashlib.sha256(CANONICAL_BYTES_CAPTURE_1).hexdigest() == CONTENT_HASH_CAPTURE_1
    )
    assert (
        hashlib.sha256(
            b"tokmo-holdings-legacy-v1:" + CANONICAL_BYTES_CAPTURE_1
        ).hexdigest()
        == LEGACY_SNAPSHOT_ID_CAPTURE_1
    )
    assert (
        hashlib.sha256(CANONICAL_BYTES_CAPTURE_2).hexdigest() == CONTENT_HASH_CAPTURE_2
    )
    assert (
        hashlib.sha256(
            b"tokmo-holdings-legacy-v1:" + CANONICAL_BYTES_CAPTURE_2
        ).hexdigest()
        == LEGACY_SNAPSHOT_ID_CAPTURE_2
    )


def test_strict_schema_initialized(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    value = capture_1()
    value = dataclasses.replace(value, content_hash=snapshot_content_hash(value))
    permit = authorize_user_request(
        snapshot_id=value.snapshot_id, clock=lambda: CAPTURED_AT
    )

    receipt = HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT).append(
        value, permit
    )

    assert receipt.status == "SAVED"
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        assert objects == [
            ("index", "holdings_snapshots_capture_order"),
            ("table", "holdings_snapshot_accounts"),
            ("table", "holdings_snapshot_items"),
            ("table", "holdings_snapshots"),
            ("table", "storage_metadata"),
        ]
        strict = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_list")
            if row[1]
            in {
                "storage_metadata",
                "holdings_snapshots",
                "holdings_snapshot_accounts",
                "holdings_snapshot_items",
            }
        }
        assert set(strict.values()) == {1}
        assert set(connection.execute("SELECT key, value FROM storage_metadata")) == {
            ("holdings_snapshot_schema_version", "1"),  # B2: stays v1, no migration
            ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
        }
    finally:
        connection.close()


def test_exact_0700_0600_modes(tmp_path):
    import stat

    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    value = capture_1()
    value = dataclasses.replace(value, content_hash=snapshot_content_hash(value))
    permit = authorize_user_request(
        snapshot_id=value.snapshot_id, clock=lambda: CAPTURED_AT
    )

    assert (
        HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT)
        .append(value, permit)
        .status
        == "SAVED"
    )
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _stored(value):
    return dataclasses.replace(value, content_hash=snapshot_content_hash(value))


def _permit(value):
    return authorize_user_request(
        snapshot_id=value.snapshot_id, clock=lambda: CAPTURED_AT
    )


def test_append_new_snapshot_and_read_exact_typed_value(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    value = _stored(capture_1())
    store = HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT)

    receipt = store.append(value, _permit(value))

    assert dataclasses.asdict(receipt) == {
        "status": "SAVED",
        "snapshot_id": value.snapshot_id,
        "reason": "",
        "submitted_count": 1,
        "inserted_count": 1,
        "duplicate_count": 0,
        "conflict_count": 0,
        "stored_at": CAPTURED_AT,
    }
    assert store.read_snapshot(value.snapshot_id) == value


def test_missing_file_read_only(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT)
    from tokmo.records.holdings_snapshots import HoldingsSnapshotReadError

    with pytest.raises(HoldingsSnapshotReadError, match="not_found"):
        store.read_snapshot(UUID4_HEX)
    assert not path.exists()


def test_exact_same_snapshot_replay_is_duplicate(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    value = _stored(capture_1())
    store = HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT)
    assert store.append(value, _permit(value)).status == "SAVED"

    receipt = store.append(value, _permit(value))

    assert receipt.status == "DUPLICATE"
    assert receipt.reason == "duplicate"
    assert receipt.duplicate_count == 1


def test_same_id_different_content_is_conflict(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    value = _stored(capture_1())
    changed = _stored(
        snapshot(
            [
                HoldingsSnapshotAccount(
                    account_ordinal=1, holdings=(holding(quantity="11"),)
                ),
                HoldingsSnapshotAccount(account_ordinal=2, holdings=()),
            ]
        )
    )
    store = HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT)
    assert store.append(value, _permit(value)).status == "SAVED"

    receipt = store.append(changed, _permit(changed))

    assert receipt.status == "CONFLICT"
    assert receipt.reason == "conflict"
    assert receipt.conflict_count == 1
    assert store.read_snapshot(value.snapshot_id) == value


def test_distinct_snapshot_same_content_is_saved(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    first = _stored(capture_1())
    second = dataclasses.replace(first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    store = HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT)

    assert store.append(first, _permit(first)).status == "SAVED"
    assert store.append(second, _permit(second)).status == "SAVED"
    assert store.read_snapshot(second.snapshot_id) == second


def test_latest_pair_headers_and_tied_timestamp_ordering(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(path, clock=lambda: CAPTURED_AT)
    lower = _stored(capture_1())
    higher = dataclasses.replace(lower, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    assert store.append(lower, _permit(lower)).status == "SAVED"
    assert store.append(higher, _permit(higher)).status == "SAVED"

    assert store.read_latest() == higher
    assert store.read_latest_pair() == (lower, higher)
    headers = store.list_capture_headers()
    assert tuple(header.snapshot_id for header in headers) == (
        higher.snapshot_id,
        lower.snapshot_id,
    )
    with pytest.raises(ValueError):
        store.list_capture_headers(True)
    with pytest.raises(ValueError):
        store.list_capture_headers(0)
    with pytest.raises(ValueError):
        store.list_capture_headers(1001)


# --- assemble_complete_snapshot (Slice 7: program/composition seam) ---


def test_assemble_complete_snapshot_from_account_facts():
    """assemble_complete_snapshot builds a CompleteHoldingsSnapshot from
    a list of TossAccountHoldingsFact and a snapshot_id, computing the
    content_hash via the canonical serializer (SDS §2.3, §5)."""
    from tokmo.programs.toss_holdings import assemble_complete_snapshot
    from tokmo.values import TOSS_SOURCE, TossAccountHoldingsFact

    facts = (
        TossAccountHoldingsFact(
            source=TOSS_SOURCE,
            captured_at=CAPTURED_AT,
            account_ordinal=1,
            account_count=2,
            holdings=(holding("AAPL"),),
        ),
        TossAccountHoldingsFact(
            source=TOSS_SOURCE,
            captured_at=CAPTURED_AT,
            account_ordinal=2,
            account_count=2,
            holdings=(),
        ),
    )
    result = assemble_complete_snapshot(
        facts, snapshot_id=UUID4_HEX, content_hash_fn=snapshot_content_hash
    )
    assert isinstance(result, CompleteHoldingsSnapshot)
    assert result.snapshot_id == UUID4_HEX
    assert result.source == TOSS_SOURCE
    assert result.captured_at == CAPTURED_AT
    assert result.discovered_account_count == 2
    assert len(result.accounts) == 2
    assert result.accounts[0].account_ordinal == 1
    assert result.accounts[1].account_ordinal == 2
    assert result.accounts[1].holdings == ()
    # content_hash is computed, not a placeholder.
    assert result.content_hash == snapshot_content_hash(result)
    assert result.content_hash != PLACEHOLDER_HASH


def test_diff_latest_typed_pair():
    """diff_latest_typed compares two CompleteHoldingsSnapshot values
    as capture-level multisets (no A→B linkage, no ordinal continuity).
    It discloses account_count_changed when account counts differ."""
    from tokmo.programs.toss_holdings import diff_latest_typed

    earlier = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=(holding("GOOGL"),)),
        ]
    )
    later = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )

    diff = diff_latest_typed(earlier, later)
    assert "account_count_changed" in diff
    assert diff["account_count_changed"] is True
    assert diff["counts"]["current_items"] == 1
    assert diff["counts"]["previous_items"] == 2


def test_diff_latest_typed_same_account_count():
    """When account counts are equal, account_count_changed is False."""
    from tokmo.programs.toss_holdings import diff_latest_typed

    earlier = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )
    later = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )

    diff = diff_latest_typed(earlier, later)
    assert diff["account_count_changed"] is False


def test_diff_latest_typed_none_earlier():
    """When there is no earlier snapshot, account_count_changed is False
    and all counts reflect the current capture only."""
    from tokmo.programs.toss_holdings import diff_latest_typed

    later = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )
    diff = diff_latest_typed(None, later)
    assert diff["account_count_changed"] is False
    assert diff["previous_captured_at"] is None


# --- Slice 7 BLOCKER fix: ordinal-free multiset diff (no A→B linkage) ---


def test_ordinal_swap_produces_zero_diff():
    """Ordinal is capture-local: when the same holdings multiset appears
    under different ordinal positions across two captures, the diff must
    be zero — no added/removed/changed.  No account_ordinal may appear
    in diff entries."""
    from tokmo.programs.toss_holdings import diff_latest_typed

    earlier = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=(holding("GOOGL"),)),
        ]
    )
    # Same holdings multiset, swapped between ordinal positions.
    later = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("GOOGL"),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=(holding("AAPL"),)),
        ]
    )

    diff = diff_latest_typed(earlier, later)
    assert diff["counts"]["added"] == 0
    assert diff["counts"]["removed"] == 0
    assert diff["counts"]["changed"] == 0
    # No entry may contain account_ordinal.
    for bucket in ("added", "removed", "changed"):
        for entry in diff[bucket]:
            assert "account_ordinal" not in entry, (
                f"account_ordinal leaked into {bucket}: {entry}"
            )


def test_duplicate_symbol_preserves_multiplicity():
    """When the same symbol appears across multiple accounts, multiplicity
    must be preserved — a dict keyed on symbol alone would lose duplicates.
    The Counter/multiset approach must NOT overwrite."""
    from tokmo.programs.toss_holdings import diff_latest_typed

    earlier = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=(holding("AAPL"),)),
        ]
    )
    later = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )

    diff = diff_latest_typed(earlier, later)
    # One AAPL was removed (multiplicity 2 → 1).
    assert diff["counts"]["removed"] == 1
    assert diff["counts"]["added"] == 0
    assert diff["counts"]["changed"] == 0
    assert diff["counts"]["current_items"] == 1
    assert diff["counts"]["previous_items"] == 2


def test_unmatched_quantity_change_is_added_removed_not_changed():
    """Without durable item/account identity, a quantity/avg change must
    NOT be labelled 'changed'. The old signature is 'removed', the new
    signature is 'added', and changed must be empty."""
    from tokmo.programs.toss_holdings import diff_latest_typed

    earlier = snapshot(
        [
            HoldingsSnapshotAccount(
                account_ordinal=1,
                holdings=(holding("AAPL", quantity="10", avg_price="150.5"),),
            ),
        ]
    )
    later = snapshot(
        [
            HoldingsSnapshotAccount(
                account_ordinal=1,
                holdings=(holding("AAPL", quantity="20", avg_price="150.5"),),
            ),
        ]
    )

    diff = diff_latest_typed(earlier, later)
    assert diff["counts"]["changed"] == 0
    assert diff["counts"]["added"] == 1
    assert diff["counts"]["removed"] == 1
    # Added entry has the new values; removed has the old.
    assert len(diff["added"]) == 1
    assert len(diff["removed"]) == 1
    added = diff["added"][0]
    removed = diff["removed"][0]
    assert added["quantity"] == "20"
    assert removed["quantity"] == "10"
    # No account_ordinal in any entry.
    assert "account_ordinal" not in added
    assert "account_ordinal" not in removed


def test_account_count_changed_remains_aggregate_and_accurate():
    """account_count_changed is aggregate (discovered_account_count differs)
    and remains accurate regardless of ordinal values used per capture."""
    from tokmo.programs.toss_holdings import diff_latest_typed

    # 2 accounts in previous, 1 in current — different count.
    earlier = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
            HoldingsSnapshotAccount(account_ordinal=2, holdings=(holding("GOOGL"),)),
        ]
    )
    later = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )

    diff = diff_latest_typed(earlier, later)
    assert diff["account_count_changed"] is True

    # Same count (1→1), same holdings — account_count_changed is False.
    # Ordinal is always 1 for a single account (contiguous invariant);
    # the point is that account_count_changed is purely aggregate.
    earlier2 = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )
    later2 = snapshot(
        [
            HoldingsSnapshotAccount(account_ordinal=1, holdings=(holding("AAPL"),)),
        ]
    )
    diff2 = diff_latest_typed(earlier2, later2)
    assert diff2["account_count_changed"] is False


# --- Slice 7 BLOCKER 1 repair: assembly completeness invariants ---


def _account_fact(
    ordinal, count=2, captured_at=CAPTURED_AT, source=TOSS_SOURCE, holdings=None
):
    return TossAccountHoldingsFact(
        source=source,
        captured_at=captured_at,
        account_ordinal=ordinal,
        account_count=count,
        holdings=holdings if holdings is not None else (holding("AAPL"),),
    )


class _RecordingHasher:
    """Records every call; returns a fixed non-zero hash."""

    def __init__(self):
        self.calls = 0

    def __call__(self, snapshot):
        self.calls += 1
        return "a" * 64


@pytest.mark.parametrize(
    "label,bad_facts",
    [
        ("empty", ()),
        ("wrong_type_bool", (True, False)),
        ("wrong_type_dict", ({"source": TOSS_SOURCE},)),
        ("wrong_type_list", [_account_fact(1)]),
        (
            "mixed_source",
            (
                _account_fact(1, source="wrong-source"),
                _account_fact(2),
            ),
        ),
        (
            "mixed_timestamp",
            (
                _account_fact(1, captured_at=CAPTURED_AT),
                _account_fact(2, captured_at=CAPTURED_AT.replace(hour=1)),
            ),
        ),
        (
            "conflicting_count",
            (
                _account_fact(1, count=3),
                _account_fact(2, count=3),
            ),
        ),
        (
            "count_neq_len",
            (
                _account_fact(1, count=3),
                _account_fact(2, count=3),
            ),
        ),
        (
            "missing_ordinal",
            (
                _account_fact(1),
                _account_fact(3),
            ),
        ),
        (
            "duplicate_ordinal",
            (
                _account_fact(1),
                _account_fact(1),
            ),
        ),
        (
            "out_of_order_ordinal",
            (
                _account_fact(2),
                _account_fact(1),
            ),
        ),
    ],
)
def test_assemble_rejects_malformed_bundle_before_hasher(label, bad_facts):
    """Every malformed fact bundle must raise ValueError before the
    content_hash_fn is ever called (fail closed before hasher)."""
    from tokmo.programs.toss_holdings import assemble_complete_snapshot

    hasher = _RecordingHasher()
    with pytest.raises(ValueError):
        assemble_complete_snapshot(
            bad_facts,
            snapshot_id=UUID4_HEX,
            content_hash_fn=hasher,
        )
    assert hasher.calls == 0, "hasher must not be called on invalid input"


def test_assemble_accepts_valid_bundle_and_calls_hasher_once():
    """A well-formed bundle calls the hasher exactly once and produces
    the expected snapshot."""
    from tokmo.programs.toss_holdings import assemble_complete_snapshot

    hasher = _RecordingHasher()
    facts = (
        _account_fact(1, count=2),
        _account_fact(2, count=2),
    )
    result = assemble_complete_snapshot(
        facts,
        snapshot_id=UUID4_HEX,
        content_hash_fn=hasher,
    )
    assert hasher.calls == 1
    assert result.source == TOSS_SOURCE
    assert result.discovered_account_count == 2
    assert result.captured_at == CAPTURED_AT


def test_assemble_wrong_type_tuple_of_str_rejected():
    """Exact type: tuple of TossAccountHoldingsFact only; a tuple of
    strings must be rejected before the hasher."""
    from tokmo.programs.toss_holdings import assemble_complete_snapshot

    hasher = _RecordingHasher()
    with pytest.raises(ValueError):
        assemble_complete_snapshot(
            ("not", "facts"),
            snapshot_id=UUID4_HEX,
            content_hash_fn=hasher,
        )
    assert hasher.calls == 0
