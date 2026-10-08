"""Slice 3 — Composite HOLDINGS v2 MaterialPack tests (B1, B3, M3, M4, m4)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from tokmo.values.material_pack import (
    JobKind,
    MaterialPackV1,
    build_material_pack_v1,
    canonical_pack_bytes,
    canonical_pack_file_bytes,
    COMPOSITE_HOLDINGS_PARSER_VERSION,
    HOLDINGS_PAYLOAD_VERSION,
    _validate_holdings_v1,
    _validate_holdings_generation_r2,
)
from tokmo.values.current_holdings import (
    CurrentHoldingsV1,
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    HoldingItemV1,
)
from tokmo.values.pension_holdings import pension_account_scope

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
_H64 = "a" * 64


def _make_v1_payload():
    return {
        "accounts": [
            {
                "account_scope": "toss-acct-" + "a" * 24,
                "account_type_label": "toss",
                "ordinal": 1,
                "holdings": [
                    {"symbol": "005930", "quantity": "100", "avg_price": "70000",
                     "currency": "KRW", "market_raw": "KR", "market_field": "market"},
                ],
            },
        ],
        "global_inventory": True,
    }


def _make_v2_payload():
    pension_scope = pension_account_scope()
    return {
        "accounts": [
            {
                "account_scope": "toss-acct-" + "a" * 24,
                "account_type_label": "toss",
                "ordinal": 1,
                "holdings": [
                    {"symbol": "005930", "quantity": "100", "avg_price": "70000",
                     "currency": "KRW", "market_raw": "KR", "market_field": "market"},
                ],
                "source_provenance": "TOSS_PERSONAL",
            },
            {
                "account_scope": pension_scope,
                "account_type_label": "pension-excel-v1",
                "ordinal": 2,
                "holdings": [
                    {"symbol": "0167A0", "quantity": "50", "avg_price": "10000",
                     "currency": "KRW", "market_raw": "KR", "market_field": "market"},
                ],
                "source_provenance": "PENSION_EXCEL",
                "pension_revision_id": "a" * 32,
                "pension_source_as_of": "2026-07-01T00:00:00+09:00",
                "pension_source_age_days": 35,
                "pension_freshness_class": "AGED_MANUAL_SOURCE",
                "pension_semantic_digest": _H64,
            },
        ],
        "global_inventory": True,
        "payload_version": 2,
        "pension_provenance": {
            "revision_id": "a" * 32,
            "source_content_sha256": _H64,
            "semantic_digest": _H64,
            "source_as_of": "2026-07-01T00:00:00+09:00",
            "source_age_days": 35,
            "freshness_class": "AGED_MANUAL_SOURCE",
            "parser_version": "pension-excel-v1",
        },
    }


class TestV1BackwardCompat:
    def test_v1_payload_still_validates(self):
        payload = _make_v1_payload()
        _validate_holdings_v1(payload)

    def test_v1_pack_decode_unchanged(self):
        payload = _make_v1_payload()
        pack = build_material_pack_v1(
            schema_version=1, job=JobKind.HOLDINGS,
            tick_id=100, market_partition=("KR",),
            payload=payload, captured_at=_NOW,
            source_timestamps={"provider": None, "fetched": None, "stored": None},
            parser_version="toss-holdings-v1",
        )
        assert pack.content_hash is not None
        assert len(pack.content_hash) == 64


class TestV2Dispatch:
    def test_v2_payload_validates(self):
        payload = _make_v2_payload()
        _validate_holdings_generation_r2(payload)

    def test_v2_pack_builds_with_composite_parser_version(self):
        payload = _make_v2_payload()
        pack = build_material_pack_v1(
            schema_version=1, job=JobKind.HOLDINGS,
            tick_id=200, market_partition=("KR",),
            payload=payload, captured_at=_NOW,
            source_timestamps={"provider": None, "fetched": None, "stored": None},
            parser_version=COMPOSITE_HOLDINGS_PARSER_VERSION,
        )
        assert pack.parser_version == COMPOSITE_HOLDINGS_PARSER_VERSION
        assert len(pack.content_hash) == 64

    def test_v2_payload_version_must_be_2(self):
        payload = _make_v2_payload()
        payload["payload_version"] = 3
        with pytest.raises(ValueError):
            _validate_holdings_generation_r2(payload)

    def test_v2_requires_exactly_one_pension_excel(self):
        payload = _make_v2_payload()
        # Remove pension account
        payload["accounts"] = [payload["accounts"][0]]
        with pytest.raises(ValueError, match="exactly one PENSION_EXCEL"):
            _validate_holdings_generation_r2(payload)

    def test_v2_rejects_scope_mismatch(self):
        payload = _make_v2_payload()
        # Swap scope patterns
        payload["accounts"][0]["account_scope"] = "pension-excel-" + "b" * 24
        payload["accounts"][0]["source_provenance"] = "TOSS_PERSONAL"
        with pytest.raises(ValueError, match="TOSS_PERSONAL scope mismatch"):
            _validate_holdings_generation_r2(payload)


class TestCompositeParserVersion:
    def test_composite_parser_version_value(self):
        assert COMPOSITE_HOLDINGS_PARSER_VERSION == "composite-holdings-v2"

    def test_holdings_payload_version_value(self):
        assert HOLDINGS_PAYLOAD_VERSION == 2


class TestCurrentHoldingsWithPensionScope:
    def test_pension_scope_accepted_in_current_holdings(self):
        pension_scope = pension_account_scope()
        account = AccountHoldingsV1(
            account_scope=pension_scope,
            ordinal=2,
            holdings=(
                HoldingItemV1(
                    symbol="0167A0", quantity="50", avg_price="10000",
                    currency="KRW", market_raw="KR", market_field="market",
                ),
            ),
        )
        identity = AccountScopeIdentityV1(
            account_scope=pension_scope,
            account_type_label="pension-excel-v1",
            ordinal=2,
            masked_name=None,
        )
        personal = AccountHoldingsV1(
            account_scope="toss-acct-" + "c" * 24,
            ordinal=1,
            holdings=(
                HoldingItemV1(
                    symbol="005930", quantity="100", avg_price="70000",
                    currency="KRW", market_raw="KR", market_field="market",
                ),
            ),
        )
        personal_identity = AccountScopeIdentityV1(
            account_scope="toss-acct-" + "c" * 24,
            account_type_label="toss",
            ordinal=1,
            masked_name=None,
        )
        state = CurrentHoldingsV1(
            schema_version=1,
            accounts=(personal, account),
            global_inventory=True,
            captured_at=_NOW,
            identity_map={
                personal.account_scope: personal_identity,
                pension_scope: identity,
            },
        )
        assert len(state.accounts) == 2
        assert state.accounts[1].account_scope == pension_scope
