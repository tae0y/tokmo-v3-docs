"""Tests for private atomic fundamental material store.

Stores OfficialFundamentalMaterialV1 records in a private 0600 SQLite
database. Atomic inserts, no clobber, deterministic retrieval.

M9: tests verify dir 0700, db file 0600, no exposure window.
"""

import json
import os
import stat
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.facts.sec_fundamentals import parse_companyfacts_response
from tokmo.records.fundamental_material_store import (
    FundamentalMaterialStore,
    FundamentalMaterialStoreError,
    FundamentalStoreAbsentError,
    FundamentalStoreCorruptError,
    FundamentalStoreCandidateCapError,
    FundamentalStoreVocabularyError,
    read_admissible_materials_ro,
)
from tokmo.values.quant_multifactor_core import MarketV1
from tokmo.values.fundamental_materials import (
    AccountingRegime,
    DATE_ONLY_POLICY_VERSION,
    DATE_ONLY_SESSION_HISTORY_SOURCE,
    AvailableAtResolution,
    MetricEvidence,
    MetricSelection,
    OfficialFundamentalMaterialV1,
    SEC_FUNDAMENTAL_SOURCE,
    StatementScope,
)


_AS_OF = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

FIXTURES = Path(__file__).parent / "fixtures" / "sec_companyfacts"


def _typed_resolver(filed_raw: str) -> AvailableAtResolution:
    return AvailableAtResolution(
        available_at=datetime(2025, 11, 3, 21, 0, tzinfo=timezone.utc),
        policy_version=DATE_ONLY_POLICY_VERSION,
        session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
    )


def _parse_fixture_materials():
    """Materials exactly as the production parser emits them."""
    result = parse_companyfacts_response(
        (FIXTURES / "aapl_minimal.json").read_bytes(),
        cik="0000320193",
        canonical_symbol="AAPL",
        observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
        available_at_resolver=_typed_resolver,
        market="US_NASDAQ",
        statement_scope=StatementScope.CONSOLIDATED,
        as_of=_AS_OF,
    )
    assert result.materials is not None
    return list(result.materials)


def _rewrite_payload(db_path: Path, source_ref: str, mutate) -> None:
    """Apply ``mutate`` to the stored payload JSON for ``source_ref``."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT payload_json FROM fundamental_materials WHERE source_ref = ?",
            (source_ref,),
        ).fetchone()
        payload = json.loads(row[0])
        mutate(payload)
        conn.execute(
            "UPDATE fundamental_materials SET payload_json = ? WHERE source_ref = ?",
            (json.dumps(payload), source_ref),
        )
        conn.commit()
    finally:
        conn.close()


def _make_material(fiscal_year=2025, entity_id="0000320193", symbol="AAPL"):
    return OfficialFundamentalMaterialV1(
        source=SEC_FUNDAMENTAL_SOURCE,
        entity_id=entity_id,
        canonical_symbol=symbol,
        market="US_NASDAQ",
        accounting_regime=AccountingRegime.US_GAAP,
        statement_scope=StatementScope.CONSOLIDATED,
        fiscal_year=fiscal_year,
        period_start=date(2024, 9, 29),
        period_end=date(2025, 9, 27),
        filing_id="0000320193-25-000079",
        filed_at_raw="2025-10-31",
        available_at=datetime(2025, 11, 3, 21, 0, tzinfo=timezone.utc),
        revision_of=None,
        taxonomy_namespace="us-gaap",
        taxonomy_version="2024",
        mapping_version="v1",
        revenue_ttm=MetricEvidence(
            value=Decimal("416161000000"),
            source_concept_id="RevenueFromContractWithCustomerExcludingAssessedTax",
            unit="USD",
            selection=MetricSelection.EXACT,
        ),
        net_income_ttm=None,
        gross_profit_ttm=None,
        total_equity=None,
        total_assets=None,
        diluted_shares_outstanding=None,
        source_request_identity_digest="abc123",
        raw_body_sha256="def456",
        observed_at=datetime(2025, 10, 31, 22, 0, tzinfo=timezone.utc),
        as_of=_AS_OF,
    )


class TestFundamentalMaterialStore:
    def test_store_creates_private_file(self, tmp_path):
        store_path = tmp_path / "fundamentals.db"
        store = FundamentalMaterialStore(store_path)
        material = _make_material()
        store.insert(material)
        assert store_path.exists()
        mode = stat.S_IMODE(os.stat(store_path).st_mode)
        # 0600 permissions for private store DB file
        assert mode == 0o600

    def test_read_only_absent_creates_nothing(self, tmp_path):
        path = tmp_path / "missing" / "fundamentals.db"
        with pytest.raises(FundamentalStoreAbsentError):
            read_admissible_materials_ro(path, canonical_symbol="AAPL", market=MarketV1.US_NYSE_NASDAQ, as_of=_AS_OF, limit=64)
        assert not path.parent.exists()

    def test_read_only_corrupt_and_candidate_cap_are_typed(self, tmp_path):
        corrupt = tmp_path / "corrupt.db"
        corrupt.write_bytes(b"not sqlite")
        with pytest.raises(FundamentalStoreCorruptError):
            read_admissible_materials_ro(corrupt, canonical_symbol="AAPL", market=MarketV1.US_NYSE_NASDAQ, as_of=_AS_OF, limit=64)
        path = tmp_path / "fundamentals.db"
        store = FundamentalMaterialStore(path)
        for year in range(2020, 2023):
            store.insert(_make_material(fiscal_year=year))
        with pytest.raises(FundamentalStoreCandidateCapError):
            read_admissible_materials_ro(path, canonical_symbol="AAPL", market=MarketV1.US_NYSE_NASDAQ, as_of=_AS_OF, limit=2)

    def test_read_only_exact_market_vocabulary_and_utc_sort(self, tmp_path):
        path = tmp_path / "fundamentals.db"
        store = FundamentalMaterialStore(path)
        instants = (
            ("US_NYSE", datetime.fromisoformat("2025-11-02T01:30:00-04:00"), 2022),
            ("US_NASDAQ", datetime.fromisoformat("2025-11-02T01:15:00-05:00"), 2023),
            ("US_NYSE_NASDAQ", datetime.fromisoformat("2025-11-02T01:00:00-05:00"), 2024),
        )
        for market, available, year in instants:
            material = _make_material(fiscal_year=year)
            object.__setattr__(material, "market", market)
            object.__setattr__(material, "available_at", available)
            object.__setattr__(material, "_source_ref", material._compute_source_ref())
            store.insert(material)
        rows = read_admissible_materials_ro(path, canonical_symbol="AAPL", market=MarketV1.US_NYSE_NASDAQ, as_of=datetime.fromisoformat("2025-11-02T06:30:00+00:00"), limit=64)
        assert tuple(row.market for row in rows) == ("US_NASDAQ", "US_NYSE_NASDAQ", "US_NYSE")

    def test_read_only_stops_on_vocabulary_drift_and_handles_uri_chars(self, tmp_path):
        path = tmp_path / "store?#%" / "fundamentals.db"
        store = FundamentalMaterialStore(path)
        material = _make_material()
        object.__setattr__(material, "market", "US_NEW_EXCHANGE")
        object.__setattr__(material, "_source_ref", material._compute_source_ref())
        store.insert(material)
        with pytest.raises(FundamentalStoreVocabularyError):
            read_admissible_materials_ro(path, canonical_symbol="AAPL", market=MarketV1.US_NYSE_NASDAQ, as_of=_AS_OF, limit=64)

    def test_candidate_cap_applies_after_utc_admissibility_filter(self, tmp_path):
        path = tmp_path / "fundamentals.db"
        store = FundamentalMaterialStore(path)
        for year in range(2100, 2165):
            future = _make_material(fiscal_year=year)
            object.__setattr__(future, "available_at", datetime(year, 1, 1, tzinfo=timezone.utc))
            object.__setattr__(future, "_source_ref", future._compute_source_ref())
            store.insert(future)
        current = _make_material(fiscal_year=2025)
        store.insert(current)
        assert read_admissible_materials_ro(path, canonical_symbol="AAPL", market=MarketV1.US_NYSE_NASDAQ, as_of=_AS_OF, limit=2) == (current,)

    def test_unaware_available_at_corruption_keeps_specific_typed_error(self, tmp_path):
        path = tmp_path / "fundamentals.db"
        store = FundamentalMaterialStore(path)
        material = _make_material()
        store.insert(material)
        _rewrite_payload(path, material.source_ref, lambda payload: payload.__setitem__("available_at", "2025-11-03T21:00:00"))
        with pytest.raises(FundamentalStoreCorruptError, match="available_at_not_aware"):
            read_admissible_materials_ro(path, canonical_symbol="AAPL", market=MarketV1.US_NYSE_NASDAQ, as_of=_AS_OF, limit=64)

    def test_store_directory_is_private(self, tmp_path):
        """M9: parent directory must be 0700."""
        store_path = tmp_path / "subdir" / "fundamentals.db"
        store = FundamentalMaterialStore(store_path)
        material = _make_material()
        store.insert(material)
        dir_mode = stat.S_IMODE(os.stat(store_path.parent).st_mode)
        assert dir_mode == 0o700

    def test_no_exposure_window_on_creation(self, tmp_path):
        """M9: DB file must be created with 0600 from the start, not
        created with umask defaults and then chmod'd."""
        store_path = tmp_path / "fundamentals.db"
        # Create the store — _init_db should create the file 0600 directly.
        store = FundamentalMaterialStore(store_path)
        # Verify the file is 0600 even before any insert.
        assert store_path.exists()
        mode = stat.S_IMODE(os.stat(store_path).st_mode)
        assert mode == 0o600

    def test_insert_and_retrieve(self, tmp_path):
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        material = _make_material()
        store.insert(material)
        retrieved = store.get_by_source_ref(material.source_ref)
        assert retrieved is not None
        assert retrieved.source_ref == material.source_ref
        assert retrieved.fiscal_year == material.fiscal_year

    def test_no_clobber_on_duplicate_source_ref(self, tmp_path):
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        material = _make_material()
        store.insert(material)
        with pytest.raises(FundamentalMaterialStoreError):
            store.insert(material)

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        assert store.get_by_source_ref("nonexistent") is None

    def test_get_by_entity_and_year(self, tmp_path):
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        m1 = _make_material(fiscal_year=2025)
        m2 = _make_material(fiscal_year=2024)
        store.insert(m1)
        store.insert(m2)
        results = store.get_by_entity("0000320193")
        assert len(results) == 2
        fys = sorted(r.fiscal_year for r in results)
        assert fys == [2024, 2025]

    def test_store_preserves_all_fields(self, tmp_path):
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        material = _make_material()
        store.insert(material)
        retrieved = store.get_by_source_ref(material.source_ref)
        assert retrieved is not None
        assert retrieved.canonical_symbol == material.canonical_symbol
        assert retrieved.revenue_ttm is not None
        assert retrieved.revenue_ttm.value == material.revenue_ttm.value
        assert retrieved.revision_of == material.revision_of
        assert retrieved.raw_body_sha256 == material.raw_body_sha256
        assert retrieved.as_of == material.as_of

    def test_decimal_zero_persists(self, tmp_path):
        """m5: Decimal(0) must not be treated as falsy and stored as NULL."""
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        material = _make_material()
        # Override revenue to Decimal(0)
        material = OfficialFundamentalMaterialV1(
            source=material.source,
            entity_id=material.entity_id,
            canonical_symbol=material.canonical_symbol,
            market=material.market,
            accounting_regime=material.accounting_regime,
            statement_scope=material.statement_scope,
            fiscal_year=material.fiscal_year,
            period_start=material.period_start,
            period_end=material.period_end,
            filing_id=material.filing_id,
            filed_at_raw=material.filed_at_raw,
            available_at=material.available_at,
            revision_of=material.revision_of,
            taxonomy_namespace=material.taxonomy_namespace,
            taxonomy_version=material.taxonomy_version,
            mapping_version=material.mapping_version,
            revenue_ttm=MetricEvidence(
                value=Decimal("0"),
                source_concept_id="Test",
                unit="USD",
                selection=MetricSelection.EXACT,
            ),
            net_income_ttm=None,
            gross_profit_ttm=None,
            total_equity=None,
            total_assets=None,
            diluted_shares_outstanding=None,
            source_request_identity_digest="abc123",
            raw_body_sha256="def456",
            observed_at=material.observed_at,
            as_of=_AS_OF,
        )
        store.insert(material)
        retrieved = store.get_by_source_ref(material.source_ref)
        assert retrieved is not None
        assert retrieved.revenue_ttm is not None
        assert retrieved.revenue_ttm.value == Decimal("0")

    def test_parser_created_materials_roundtrip_exactly(self, tmp_path):
        """B1: materials produced by the production parser — which always
        stamps a verified session-history source — must survive insert and
        readback with every provenance field intact.

        The pre-repair store dropped available_at_session_history_source from
        the serialized payload, so every parser-created material failed
        source_ref verification on readback.
        """
        materials = _parse_fixture_materials()
        assert materials, "fixture must produce materials"
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        for m in materials:
            store.insert(m)

        for m in materials:
            assert m.available_at_session_history_source == (
                DATE_ONLY_SESSION_HISTORY_SOURCE
            )
            back = store.get_by_source_ref(m.source_ref)
            assert back is not None
            assert back == m
            assert back.source_ref == m.source_ref
            assert back.available_at_session_history_source == (
                m.available_at_session_history_source
            )

        by_entity = store.get_by_entity("0000320193")
        assert len(by_entity) == len(materials)
        assert {b.source_ref for b in by_entity} == {m.source_ref for m in materials}

    def test_missing_session_history_source_in_payload_is_detected(self, tmp_path):
        """B1 guard: a payload without the session-source field (the exact
        pre-repair serialization) must fail readback verification rather than
        silently reconstructing a material with an empty source."""
        materials = _parse_fixture_materials()
        material = materials[0]
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        store.insert(material)

        _rewrite_payload(
            tmp_path / "fundamentals.db",
            material.source_ref,
            lambda p: p.pop("available_at_session_history_source"),
        )
        with pytest.raises(FundamentalMaterialStoreError, match="source_ref_mismatch"):
            store.get_by_source_ref(material.source_ref)

    def test_tampered_session_history_source_is_detected(self, tmp_path):
        """B1/C3: the session-history source is digest-bound, so swapping it
        for another value must be caught on readback."""
        materials = _parse_fixture_materials()
        material = materials[0]
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        store.insert(material)

        def tamper(payload):
            payload["available_at_session_history_source"] = "some-other-source"

        _rewrite_payload(tmp_path / "fundamentals.db", material.source_ref, tamper)
        with pytest.raises(FundamentalMaterialStoreError, match="source_ref_mismatch"):
            store.get_by_source_ref(material.source_ref)

    def test_tampered_filed_at_raw_is_detected(self, tmp_path):
        """m3: filed_at_raw is digest-bound; tampering must be detected."""
        materials = _parse_fixture_materials()
        material = materials[0]
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        store.insert(material)

        def tamper(payload):
            payload["filed_at_raw"] = "1999-01-01"

        _rewrite_payload(tmp_path / "fundamentals.db", material.source_ref, tamper)
        with pytest.raises(FundamentalMaterialStoreError, match="source_ref_mismatch"):
            store.get_by_source_ref(material.source_ref)

    def test_deserialize_verifies_source_ref(self, tmp_path):
        """M2: deserialize recomputes source_ref and detects tampering."""
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        material = _make_material()
        store.insert(material)
        # Tamper with the payload_json directly
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "fundamentals.db"))
        row = conn.execute(
            "SELECT payload_json FROM fundamental_materials WHERE source_ref = ?",
            (material.source_ref,),
        ).fetchone()
        payload = json.loads(row[0])
        # Tamper: change revenue value
        payload["revenue_ttm"]["value"] = "999999999999"
        conn.execute(
            "UPDATE fundamental_materials SET payload_json = ? WHERE source_ref = ?",
            (json.dumps(payload), material.source_ref),
        )
        conn.commit()
        conn.close()
        # Retrieval should detect the mismatch
        with pytest.raises(FundamentalMaterialStoreError):
            store.get_by_source_ref(material.source_ref)
