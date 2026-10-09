"""Slice 3 — V2 profile receipt store + normalized membership material.

All symbols/ISINs here are synthetic RECONSTRUCTED_CONTRACT shapes only
(Plan/SDS §13); no live cohort data appears in this file.
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest

POLICY_VERSION = "cadence-300s-quant-profiles-shadow-v1"
SESSION = date(2026, 8, 21)
NOW = datetime(2026, 8, 21, 21, 10, tzinfo=UTC)


def _kr_members():
    # 2 synthetic stocks + 3 synthetic ETFs, RECONSTRUCTED_CONTRACT tokens.
    rows = []
    for index in range(1, 3):
        rows.append(
            {
                "symbol": f"RCS00{index}",
                "isin_code": f"KR7RCS00{index}000"[:12],
                "market": "KOSPI",
                "security_type": "STOCK",
                "is_common_share": True,
                "status": "ACTIVE",
                "currency": "KRW",
                "list_date": "2020-01-02",
                "delist_date": None,
                "krx_trading_suspended": False,
            }
        )
    for index in range(1, 4):
        rows.append(
            {
                "symbol": f"RCE00{index}",
                "isin_code": f"KR7RCE00{index}000"[:12],
                "market": "KOSPI",
                "security_type": "RECONSTRUCTED_ETF",
                "is_common_share": False,
                "status": "ACTIVE",
                "currency": "KRW",
                "list_date": "2021-03-04",
                "delist_date": None,
                "krx_trading_suspended": False,
            }
        )
    return tuple(sorted(rows, key=lambda row: row["symbol"]))


def _kr_material():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        encode_membership_material,
    )

    return encode_membership_material(
        profile_id=KR_PROFILE_ID,
        policy_version=POLICY_VERSION,
        completed_session_date=SESSION,
        request_identity="membership:toss-stocks:2026-08-21",
        observed_at=NOW,
        members=_kr_members(),
    )


def _kr_identity():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    return QuantProfileDueIdentityV1(
        KR_PROFILE_ID,
        POLICY_VERSION,
        Market.KR,
        SESSION,
        KR_UNIVERSE_POLICY_VERSION,
    )


def _us_identity():
    from tokmo.values.quant_profiles import (
        US_PROFILE_ID,
        US_UNIVERSE_POLICY_VERSION,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    return QuantProfileDueIdentityV1(
        US_PROFILE_ID,
        POLICY_VERSION,
        Market.US,
        SESSION,
        US_UNIVERSE_POLICY_VERSION,
    )


def _kr_result(
    identity,
    material,
    *,
    cohort=5,
    admissible=5,
    membership_starts=1,
    page1=5,
    page2=0,
    digest=None,
    reason="kr_fundamentals_paused",
):
    from tokmo.records.quant_profile_receipts import QuantProfileResultV1

    return QuantProfileResultV1(
        identity_digest=identity.identity_digest,
        acquisition_id=identity.acquisition_id,
        membership_holdings_digest=sha256(b"holdings").hexdigest(),
        holdings_captured_at=NOW,
        membership_starts=membership_starts,
        candle_page1_starts=page1,
        candle_page2_starts=page2,
        cohort_size=cohort,
        admissible_count=admissible,
        provenance_digest=sha256(b"provenance").hexdigest(),
        membership_material=material,
        membership_material_digest=(
            sha256(material).hexdigest() if digest is None else digest
        ),
        pack_digest="",
        degraded=True,
        limitations=(reason.upper(),),
        symbols=tuple(row["symbol"] for row in _kr_members())[:cohort],
    )


def _claim(store, identity, *, at=NOW):
    lease = store.acquire_identity_lease(identity.identity_digest)
    assert lease is not None
    with lease:
        return store.claim(
            identity,
            owner_invocation_id="test-invocation",
            owner_lease_key=lease.key,
            claimed_at=at,
            identity_lease=lease,
        )


def test_material_roundtrip_digest_and_prose_refusal():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        decode_membership_material,
        encode_membership_material,
    )

    raw = _kr_material()
    decoded = decode_membership_material(raw)
    assert decoded["schema_version"] == "quant-profile-material-v1"
    assert decoded["profile_id"] == "quant-kr-toss-membership-v1"
    assert decoded["universe_policy_version"] == "quant-universe-kr-personal-v1"
    assert decoded["completed_session_date"] == "2026-08-21"
    assert [row["symbol"] for row in decoded["members"]] == sorted(
        row["symbol"] for row in _kr_members()
    )
    # re-encoding the decoded fields reproduces the exact bytes
    re_encoded = encode_membership_material(
        profile_id=decoded["profile_id"],
        policy_version=decoded["policy_version"],
        completed_session_date=date.fromisoformat(decoded["completed_session_date"]),
        request_identity=decoded["request_identity"],
        observed_at=datetime.fromisoformat(
            decoded["observed_at"].replace("Z", "+00:00")
        ),
        members=tuple(decoded["members"]),
    )
    assert re_encoded == raw
    assert sha256(re_encoded).hexdigest() == sha256(raw).hexdigest()

    # prose-bearing member values are refused fail-closed
    prose = dict(_kr_members()[0])
    prose["status"] = "Active ordinary share listed"
    with pytest.raises(ValueError):
        encode_membership_material(
            profile_id=KR_PROFILE_ID,
            policy_version=POLICY_VERSION,
            completed_session_date=SESSION,
            request_identity="membership:toss-stocks:2026-08-21",
            observed_at=NOW,
            members=(prose,),
        )
    # oversize blobs are refused on decode
    with pytest.raises(ValueError):
        decode_membership_material(b"x" * (262144 + 1))
    # non-canonical bytes are refused on decode
    with pytest.raises(ValueError):
        decode_membership_material(b'{"schema_version": "quant-profile-material-v1"}')


def test_store_creates_private_strict_schema_and_rejects_tamper(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore

    path = tmp_path / "ops" / "quant_profile_receipts.sqlite3"
    path.parent.mkdir(mode=0o700)
    QuantProfileReceiptStore(path)
    assert (path.stat().st_mode & 0o777) == 0o600
    QuantProfileReceiptStore(path)  # clean reopen validates

    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE quant_profile_due ADD COLUMN extra TEXT")
    with pytest.raises(ValueError, match="schema_mismatch"):
        QuantProfileReceiptStore(path)


def test_claim_finish_roundtrip_binds_material_digest_and_refuses_rewrite(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    path = tmp_path / "quant_profile_receipts.sqlite3"
    store = QuantProfileReceiptStore(path)
    identity = _kr_identity()
    claimed = _claim(store, identity)
    assert claimed.state is QuantTerminalStateV1.CLAIMED
    assert claimed.profile_id == "quant-kr-toss-membership-v1"
    assert claimed.market == "KR"

    material = _kr_material()
    store.finish(
        identity.identity_digest,
        state=QuantTerminalStateV1.NO_PACK,
        reason_code="kr_fundamentals_paused",
        finished_at=NOW + timedelta(seconds=5),
        result=_kr_result(identity, material),
    )
    due = store.read_due(identity.identity_digest)
    assert due.state is QuantTerminalStateV1.NO_PACK
    assert due.reason_code == "kr_fundamentals_paused"

    # fresh reopen from a new store handle reproduces identical bytes+digest
    result = QuantProfileReceiptStore(path).read_result(identity.identity_digest)
    assert result.membership_material == material
    assert result.membership_material_digest == sha256(material).hexdigest()
    assert result.symbols == tuple(sorted(result.symbols))

    # a completed receipt is never rewritten
    with pytest.raises(ValueError, match="terminal"):
        store.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.FAILED,
            reason_code="late",
            finished_at=NOW + timedelta(seconds=9),
        )


def test_finish_refuses_unbound_material_digest_and_oversize(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store = QuantProfileReceiptStore(tmp_path / "receipts.sqlite3")
    identity = _kr_identity()
    _claim(store, identity)
    material = _kr_material()
    with pytest.raises(ValueError, match="membership_material_digest"):
        store.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.NO_PACK,
            reason_code="kr_fundamentals_paused",
            finished_at=NOW,
            result=_kr_result(identity, material, digest=sha256(b"x").hexdigest()),
        )
    with pytest.raises(ValueError, match="membership_material"):
        store.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.NO_PACK,
            reason_code="kr_fundamentals_paused",
            finished_at=NOW,
            result=_kr_result(identity, b"y" * (262144 + 1)),
        )
    # the identity is still CLAIMED after refused finishes
    from tokmo.values.quant_recurring import QuantTerminalStateV1 as _S

    assert store.read_due(identity.identity_digest).state is _S.CLAIMED


def test_read_result_revalidates_material_digest_binding(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    path = tmp_path / "receipts.sqlite3"
    store = QuantProfileReceiptStore(path)
    identity = _kr_identity()
    _claim(store, identity)
    store.finish(
        identity.identity_digest,
        state=QuantTerminalStateV1.NO_PACK,
        reason_code="kr_fundamentals_paused",
        finished_at=NOW,
        result=_kr_result(identity, _kr_material()),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE quant_profile_result SET membership_material=?",
            (b'{"tampered":true}',),
        )
    with pytest.raises(ValueError, match="membership_material_digest"):
        QuantProfileReceiptStore(path).read_result(identity.identity_digest)


def test_kr_and_us_rows_coexist_and_v1_store_is_never_touched(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    ops = tmp_path / "ops"
    ops.mkdir(mode=0o700)
    v1_path = ops / "quant_recurring.sqlite3"
    store = QuantProfileReceiptStore(ops / "quant_profile_receipts.sqlite3")
    kr = _kr_identity()
    us = _us_identity()
    _claim(store, kr)
    _claim(store, us)
    store.finish(
        kr.identity_digest,
        state=QuantTerminalStateV1.FAILED,
        reason_code="source_failure:cohort_echo",
        finished_at=NOW,
    )
    assert store.read_due(kr.identity_digest).market == "KR"
    assert store.read_due(us.identity_digest).market == "US"
    assert store.read_due(us.identity_digest).state is (QuantTerminalStateV1.CLAIMED)
    assert not v1_path.exists()


def test_identity_conflict_and_stale_claim_reconcile(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore
    from tokmo.values.cadence_policy import (
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
    )
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    path = tmp_path / "receipts.sqlite3"
    store = QuantProfileReceiptStore(path)
    identity = _kr_identity()
    _claim(store, identity)

    # same digest, conflicting stored bindings => typed identity_conflict
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE quant_profile_due SET policy_version='tampered-v9'",
        )
    with pytest.raises(ValueError, match="identity_conflict"):
        _claim(store, identity)

    # stale CLAIMED rows become terminal UNKNOWN/stale_claim
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE quant_profile_due SET policy_version=?",
            (POLICY_VERSION,),
        )
    late = NOW + timedelta(seconds=1000)
    changed = store.reconcile_stale(
        now=late,
        policy=CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
    )
    assert changed == 1
    due = store.read_due(identity.identity_digest)
    assert due.state is QuantTerminalStateV1.UNKNOWN
    assert due.reason_code == "stale_claim"


def test_result_rows_enforce_per_profile_cohort_maximum(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store = QuantProfileReceiptStore(tmp_path / "receipts.sqlite3")
    us = _us_identity()
    _claim(store, us)
    material = _kr_material()  # bytes content irrelevant to the bound check
    from tokmo.records.quant_profile_receipts import QuantProfileResultV1

    oversized_us = QuantProfileResultV1(
        identity_digest=us.identity_digest,
        acquisition_id=us.acquisition_id,
        membership_holdings_digest=sha256(b"holdings").hexdigest(),
        holdings_captured_at=NOW,
        membership_starts=7,
        candle_page1_starts=5,
        candle_page2_starts=0,
        cohort_size=5,  # exceeds the US profile maximum of 4
        admissible_count=5,
        provenance_digest=sha256(b"p").hexdigest(),
        membership_material=material,
        membership_material_digest=sha256(material).hexdigest(),
        pack_digest="",
        degraded=True,
        limitations=("X",),
        symbols=("RCU1", "RCU2", "RCU3", "RCU4", "RCU5"),
    )
    with pytest.raises(ValueError, match="cohort"):
        store.finish(
            us.identity_digest,
            state=QuantTerminalStateV1.NO_PACK,
            reason_code="cohort_mismatch",
            finished_at=NOW,
            result=oversized_us,
        )


# ---------------------------------------------------------------------------
# G2 targeted repair B2 — semantic material binding (write and read)
# ---------------------------------------------------------------------------


def _kr_material_variant(
    *, session=SESSION, policy=POLICY_VERSION, request_identity=None, members=None
):
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        encode_membership_material,
    )

    return encode_membership_material(
        profile_id=KR_PROFILE_ID,
        policy_version=policy,
        completed_session_date=session,
        request_identity=(
            f"membership:toss-stocks:{session.isoformat()}"
            if request_identity is None
            else request_identity
        ),
        observed_at=NOW,
        members=_kr_members() if members is None else members,
    )


def _us_shaped_material():
    from tokmo.values.quant_profiles import (
        US_PROFILE_ID,
        encode_membership_material,
    )

    return encode_membership_material(
        profile_id=US_PROFILE_ID,
        policy_version=POLICY_VERSION,
        completed_session_date=SESSION,
        request_identity="quant-profiles-v1:unbound",
        observed_at=NOW,
        members=(
            {
                "symbol": "RCE001",
                "venue": "NASDAQ",
                "security_type": "COMMON_EQUITY",
                "status": "N",
                "sec_ticker_form": "RCE001",
            },
            {
                "symbol": "RCE002",
                "venue": "NASDAQ",
                "security_type": "COMMON_EQUITY",
                "status": "N",
                "sec_ticker_form": "RCE002",
            },
            {
                "symbol": "RCS001",
                "venue": "NASDAQ",
                "security_type": "COMMON_EQUITY",
                "status": "N",
                "sec_ticker_form": "RCS001",
            },
            {
                "symbol": "RCS002",
                "venue": "NASDAQ",
                "security_type": "COMMON_EQUITY",
                "status": "N",
                "sec_ticker_form": "RCS002",
            },
        ),
    )


def _finish_no_pack(store, identity, result):
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store.finish(
        identity.identity_digest,
        state=QuantTerminalStateV1.NO_PACK,
        reason_code="kr_fundamentals_paused",
        finished_at=NOW,
        result=result,
    )


def test_b2_finish_refuses_cross_session_and_cross_policy_material(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore

    store = QuantProfileReceiptStore(tmp_path / "receipts.sqlite3")
    identity = _kr_identity()
    _claim(store, identity)
    with pytest.raises(ValueError, match="material_session_binding"):
        _finish_no_pack(
            store,
            identity,
            _kr_result(
                identity,
                _kr_material_variant(session=date(2026, 8, 20)),
            ),
        )
    with pytest.raises(ValueError, match="material_policy_binding"):
        _finish_no_pack(
            store,
            identity,
            _kr_result(
                identity,
                _kr_material_variant(policy="cadence-300s-market-quant4-shadow-v1"),
            ),
        )
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    assert store.read_due(identity.identity_digest).state is (
        QuantTerminalStateV1.CLAIMED
    )


def test_b2_finish_refuses_cross_profile_material(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore

    store = QuantProfileReceiptStore(tmp_path / "receipts.sqlite3")
    identity = _kr_identity()
    _claim(store, identity)
    material = _us_shaped_material()
    result = _kr_result(identity, material, cohort=4, admissible=4, page1=4)
    result = type(result)(
        **{
            **{name: getattr(result, name) for name in result.__dataclass_fields__},
            "symbols": ("RCE001", "RCE002", "RCS001", "RCS002"),
        }
    )
    with pytest.raises(ValueError, match="material_profile_binding"):
        _finish_no_pack(store, identity, result)


def test_b2_finish_refuses_wrong_request_identity_symbols_and_admissible(
    tmp_path,
):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore

    store = QuantProfileReceiptStore(tmp_path / "receipts.sqlite3")
    identity = _kr_identity()
    _claim(store, identity)

    # request identity for another session while the header session is right
    with pytest.raises(ValueError, match="material_request_identity"):
        _finish_no_pack(
            store,
            identity,
            _kr_result(
                identity,
                _kr_material_variant(
                    request_identity="membership:toss-stocks:2026-08-20",
                ),
            ),
        )
    # symbols disagree with the material members (count matches)
    good = _kr_material_variant()
    mismatched = _kr_result(identity, good)
    mismatched = type(mismatched)(
        **{
            **{
                name: getattr(mismatched, name)
                for name in mismatched.__dataclass_fields__
            },
            "symbols": ("RCX001", "RCX002", "RCX003", "RCX004", "RCX005"),
        }
    )
    with pytest.raises(ValueError, match="material_symbols_binding"):
        _finish_no_pack(store, identity, mismatched)
    # admitted-member count must equal admissible_count for KR
    with pytest.raises(ValueError, match="material_admissible_binding"):
        _finish_no_pack(
            store,
            identity,
            _kr_result(
                identity,
                good,
                admissible=4,
                page1=4,
            ),
        )
    # acquisition id must be the profile identity's exact namespace form
    unbound = _kr_result(identity, good)
    unbound = type(unbound)(
        **{
            **{name: getattr(unbound, name) for name in unbound.__dataclass_fields__},
            "acquisition_id": "quant-profiles-v1:quant-kr-toss-membership-v1:"
            "2026-08-21:" + "0" * 32,
        }
    )
    with pytest.raises(ValueError, match="material_acquisition_binding"):
        _finish_no_pack(store, identity, unbound)


def test_b2_read_result_refuses_semantically_tampered_persisted_row(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore

    path = tmp_path / "receipts.sqlite3"
    store = QuantProfileReceiptStore(path)
    identity = _kr_identity()
    _claim(store, identity)
    _finish_no_pack(store, identity, _kr_result(identity, _kr_material()))

    # consistent hash, wrong session: hash-only validation would accept this
    tampered = _kr_material_variant(session=date(2026, 8, 20))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE quant_profile_result SET membership_material=?,"
            "membership_material_digest=?",
            (tampered, sha256(tampered).hexdigest()),
        )
    with pytest.raises(ValueError, match="material_session_binding"):
        QuantProfileReceiptStore(path).read_result(identity.identity_digest)
