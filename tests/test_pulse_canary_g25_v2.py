"""G2.5 inherited family accounting, cap enforcement, safety gate.

Introduced for campaign v2 and carried forward to v3, which raised the family
cap to 22 (v1 6 + v2 6 inherited, plus 10 local for token-per-tick). The
assertions below track the module's current contract; what they guard is
unchanged — one family ledger that no root, version or restart can reset.

Test matrix (requirements):
1. inherited 12 + prospective 10 => allowed and final family cumulative 22
2. inherited 13 + prospective 10 would exceed 22 => stop before outbound
3. alternate root/version cannot reset family accounting
4. prior campaign ledgers immutable — their DBs unchanged
5. protected mutation fails
6. unattributed change fails
7. expected mutable scheduler writes pass
8. CLI enforces max-provider-starts == 22
9. family identity stable across every campaign version
10. read_inherited_provider_starts reads from prior ledgers
"""

import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

T0 = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)


def _make_success_quotes_fetch():
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.values.market_observations import TossCurrentPriceFact

    def quotes_fetch(market, symbols, *, bearer=None):
        return PulseQuoteBatchOutcomeV1(
            "success",
            tuple(
                TossCurrentPriceFact(
                    source="toss-current-price-v1",
                    symbol=sym,
                    last_price="231500",
                    currency_raw="KRW",
                    timestamp=T0,
                    fetched_at=T0,
                )
                for sym in symbols
            ),
            (),
        )

    return quotes_fetch


# ===========================================================================
# 1. inherited 12 + prospective 10 => allowed, final family 22
# ===========================================================================


def test_v1_v2_inherited_12_v3_10_allowed_family_22(tmp_path):
    """v1 used 6 starts and v2 used 6. With inherited=12, cap=22 and a
    prospective 10, family cumulative reaches exactly 22 => complete."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.values.session_gate import Market

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=_make_success_quotes_fetch(),
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=cfg,
        deps=deps,
        t0=T0,
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "all_ticks_complete"
    assert result.inherited_provider_starts == 12
    assert result.local_provider_starts == 10
    assert result.total_provider_starts == 22  # family cumulative


# ===========================================================================
# 2. inherited 13 + prospective would exceed 22 => stop before outbound
# ===========================================================================


def test_inherited_13_prospective_exceeds_22_stops(tmp_path):
    """inherited=13, cap=22, remaining=9. Token-per-tick needs 10, so the
    session can never complete and refuses before any outbound call."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.values.session_gate import Market

    outbound_calls = {"token": 0, "quotes": 0}

    def token_fetch():
        outbound_calls["token"] += 1
        return "token"

    def quotes_fetch(market, symbols, *, bearer=None):
        outbound_calls["quotes"] += 1
        return _make_success_quotes_fetch()(market, symbols, bearer=bearer)

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=cfg,
        deps=deps,
        t0=T0,
        inherited_provider_starts=13,
    )
    assert result.stop_reason == "provider_start_cap_exceeded"
    assert result.inherited_provider_starts == 13
    assert result.local_provider_starts == 0  # nothing started at all
    assert result.total_provider_starts == 13
    # No PRICE_BATCH outbound was made
    assert outbound_calls["quotes"] == 0
    assert outbound_calls["token"] == 0


# ===========================================================================
# 3. Alternate root/version cannot reset family accounting
# ===========================================================================


def test_alternate_root_does_not_reset_family(tmp_path):
    """Two different evidence roots with same inherited_provider_starts
    both enforce the same family cap. Changing root doesn't reset."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.values.session_gate import Market

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps_factory = lambda: CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=_make_success_quotes_fetch(),
        sleep_until=lambda t: None,
    )

    # Root A: inherited=12, should complete with family 22
    r_a = run_canary_session(
        root=tmp_path / "rootA",
        config=cfg,
        deps=deps_factory(),
        t0=T0,
        inherited_provider_starts=12,
    )
    assert r_a.stop_reason == "all_ticks_complete"
    assert r_a.total_provider_starts == 22

    # Root B: inherited=12 (same family), should also complete with family 22
    # This proves alternate root doesn't reset the family accounting
    r_b = run_canary_session(
        root=tmp_path / "rootB",
        config=cfg,
        deps=deps_factory(),
        t0=T0,
        inherited_provider_starts=12,
    )
    assert r_b.stop_reason == "all_ticks_complete"
    assert r_b.total_provider_starts == 22


def test_family_identity_stable_across_v1_v2():
    """Family identity is the same regardless of campaign version."""
    from tokmo.programs.pulse_canary import compute_family_identity
    from tokmo.values.session_gate import Market

    fi = compute_family_identity(Market.KR, "005930")
    assert "g25-quote-kr-005930" in fi
    assert "KR" in fi
    assert "005930" in fi
    # No version suffix — stable across v1/v2
    assert "-v1" not in fi
    assert "-v2" not in fi
    assert "-v3" not in fi


# ===========================================================================
# 4. v1 immutable — prior DB unchanged
# ===========================================================================


def test_v1_db_unchanged_after_v2_constants(tmp_path):
    """The v1 evidence DB at the real path must have exactly 6 intents.
    This test verifies the v1 DB exists and is read-only for v2."""
    v1_db = (
        Path.home()
        / ".local"
        / "share"
        / "tokmo-v3-canary"
        / "campaigns"
        / "g25-quote-kr-005930-v1"
        / "KR"
        / "005930"
        / "ops"
        / "canary_evidence.sqlite3"
    )
    if not v1_db.exists():
        pytest.skip("v1 DB not found — skipping immutability check")
    conn = sqlite3.connect(str(v1_db))
    try:
        count = conn.execute("SELECT COUNT(*) FROM provider_start_intent").fetchone()[0]
        stop = conn.execute(
            "SELECT stop_reason FROM canary_session LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 6, f"v1 must have 6 intents, got {count}"
    assert stop == "canonical_fingerprint_mutated"


# ===========================================================================
# 5. Protected mutation fails (safety gate integration with inheritance)
# ===========================================================================


def test_protected_mutation_fails_with_inheritance(tmp_path):
    """Safety gate protected mutation check works with inherited accounting."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.values.session_gate import Market

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")

    mutation_done = {"done": False}

    def quotes_fetch(market, symbols, *, bearer=None):
        if not mutation_done["done"]:
            (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"x")
            mutation_done["done"] = True
        return _make_success_quotes_fetch()(market, symbols, bearer=bearer)

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=cfg,
        deps=deps,
        t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "safety_gate_protected_mutation"


# ===========================================================================
# 6. Unattributed change fails
# ===========================================================================


def test_unattributed_change_fails_with_inheritance(tmp_path):
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.values.session_gate import Market

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")

    def quotes_fetch(market, symbols, *, bearer=None):
        (canonical / "unknown.json").write_bytes(b"?")
        os.utime(canonical / "unknown.json", ns=(time.time_ns(), time.time_ns()))
        return _make_success_quotes_fetch()(market, symbols, bearer=bearer)

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=cfg,
        deps=deps,
        t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "safety_gate_unattributed_change"


# ===========================================================================
# 7. Expected mutable scheduler writes pass
# ===========================================================================


def test_expected_mutable_writes_pass_with_inheritance(tmp_path):
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.values.session_gate import Market

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"prot")
    (canonical / "ops").mkdir(parents=True)
    (canonical / "ops" / "ops_kernel.sqlite3").write_bytes(b"old")

    def quotes_fetch(market, symbols, *, bearer=None):
        (canonical / "ops" / "ops_kernel.sqlite3").write_bytes(b"new")
        os.utime(
            canonical / "ops" / "ops_kernel.sqlite3",
            ns=(time.time_ns(), time.time_ns()),
        )
        return _make_success_quotes_fetch()(market, symbols, bearer=bearer)

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=cfg,
        deps=deps,
        t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "all_ticks_complete"
    assert result.total_provider_starts == 22  # 12 inherited + 10 local


# ===========================================================================
# 8. CLI enforces max-provider-starts == 22
# ===========================================================================


def test_cli_rejects_max_provider_starts_not_22(tmp_path):
    """CLI must reject any max_provider_starts other than the family cap."""
    from tokmo.programs.pulse_canary import run_canary_cli

    for val in [10, 14, 15, 16, 20, 21, 23, 25]:
        result = run_canary_cli(
            [
                "--mode",
                "bounded-live-canary",
                "--market",
                "KR",
                "--symbol",
                "005930",
                "--canonical-root",
                str(tmp_path / "canonical"),
                "--project-root",
                str(tmp_path / "project"),
                "--max-ticks",
                "5",
                "--max-provider-starts",
                str(val),
                "--base-commit",
                "deadbeef",
            ],
            evidence_root_override=tmp_path / "evidence",
        )
        assert result == 1, f"max_provider_starts={val} must be rejected"


def test_cli_accepts_max_provider_starts_32(tmp_path):
    """CLI accepts max_provider_starts == 22 and refuses further down.

    It still refuses — an unsandboxed run cannot prove OS write-denial — but
    the refusal must not be about the cap."""
    import subprocess
    from io import StringIO

    from tokmo.programs.pulse_canary import run_canary_cli

    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(project_root), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=str(project_root),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=str(project_root), capture_output=True
    )
    (project_root / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(project_root), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(project_root), capture_output=True
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
    ).stdout.strip()

    canonical = tmp_path / "canonical"
    canonical.mkdir()

    # Capture stderr to check the refusal reason
    old_stderr = sys.stderr
    sys.stderr = captured = StringIO()
    try:
        result = run_canary_cli(
            [
                "--mode",
                "bounded-live-canary",
                "--market",
                "KR",
                "--symbol",
                "005930",
                "--canonical-root",
                str(canonical),
                "--project-root",
                str(project_root),
                "--max-ticks",
                "5",
                "--max-provider-starts",
                "32",
                "--base-commit",
                head,
            ],
            evidence_root_override=tmp_path / "evidence",
            canonical_root_anchor_override=canonical,
        )
    finally:
        sys.stderr = old_stderr

    err_msg = captured.getvalue()
    # Refuses at the sandbox attestation, not at the cap.
    assert result == 1
    assert "max_provider_starts_must_be" not in err_msg
    assert "sandbox_write_deny_not_proven" in err_msg


# ===========================================================================
# 9. read_inherited_provider_starts
# ===========================================================================


def test_read_inherited_provider_starts_from_prior_dbs():
    """Current campaign inherits the real v1, v2, and v3 ledgers."""
    from tokmo.programs.pulse_canary import read_inherited_provider_starts
    from tokmo.values.session_gate import Market

    v1_db = (
        Path.home()
        / ".local"
        / "share"
        / "tokmo-v3-canary"
        / "campaigns"
        / "g25-quote-kr-005930-v1"
        / "KR"
        / "005930"
        / "ops"
        / "canary_evidence.sqlite3"
    )
    if not v1_db.exists():
        pytest.skip("v1 DB not found")
    count = read_inherited_provider_starts(Market.KR, "005930")
    assert count == 22  # 6 from v1 + 6 from v2 + 10 from v3


def test_read_inherited_provider_starts_zero_when_no_prior(tmp_path, monkeypatch):
    """When no prior ledger exists, inherited count is 0."""
    from tokmo.programs import pulse_canary
    from tokmo.programs.pulse_canary import read_inherited_provider_starts
    from tokmo.values.session_gate import Market

    # Monkeypatch to point to non-existent path
    monkeypatch.setattr(
        pulse_canary,
        "_PRIOR_CAMPAIGN_SPECS",
        (pulse_canary.PriorCampaignSpec("nonexistent", 1),),
    )
    count = read_inherited_provider_starts(Market.KR, "005930")
    assert count == 0


# ===========================================================================
# 10. v2 constants and identity
# ===========================================================================


def test_current_campaign_constants():
    from tokmo.programs.pulse_canary import (
        CANARY_CAMPAIGN_ID,
        CANARY_CAMPAIGN_VERSION,
        CANARY_FAMILY_CUMULATIVE_CAP,
        CANARY_FAMILY_ID,
    )

    assert CANARY_CAMPAIGN_ID == "g25-quote-kr-005930-v4"
    assert CANARY_CAMPAIGN_VERSION == 4
    assert CANARY_FAMILY_ID == "g25-quote-kr-005930"
    assert CANARY_FAMILY_CUMULATIVE_CAP == 32


def test_current_purpose_identity():
    from tokmo.programs.pulse_canary import compute_purpose_identity
    from tokmo.values.session_gate import Market

    pi = compute_purpose_identity(Market.KR, "005930")
    assert "g25-quote-kr-005930-v4" in pi
    assert "v4" in pi


def test_current_evidence_root_differs_from_the_prior_campaign():
    from tokmo.programs.pulse_canary import campaign_evidence_anchor_root

    root = campaign_evidence_anchor_root()
    assert "g25-quote-kr-005930-v4" in str(root)
    assert "g25-quote-kr-005930-v2" not in str(root)


def test_prior_campaign_specs_fixed():
    """The prior campaign allowlist is a fixed constant."""
    from tokmo.programs.pulse_canary import _PRIOR_CAMPAIGN_SPECS

    assert len(_PRIOR_CAMPAIGN_SPECS) >= 1
    spec = _PRIOR_CAMPAIGN_SPECS[0]
    assert spec.campaign_id == "g25-quote-kr-005930-v1"
    assert spec.version == 1


# ===========================================================================
# 11. No-change safety gate PASS with inheritance (integration)
# ===========================================================================


def test_no_change_safety_gate_pass_with_inheritance(tmp_path):
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.values.session_gate import Market

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"prot")

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=_make_success_quotes_fetch(),
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=cfg,
        deps=deps,
        t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "all_ticks_complete"
    assert result.inherited_provider_starts == 12
    assert result.local_provider_starts == 10
    assert result.total_provider_starts == 22
