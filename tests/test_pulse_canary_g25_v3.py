"""G2.5 campaign v3 — token-per-tick, family cap 22, OS write-deny proof.

Two v2 defects drive v3.

1. Token amortization. v2 minted one TOKEN at 22:12:00 UTC and reused it for
   all five ticks. The production cadence minted its own TOKEN at 22:15:19 and
   the provider rotated the canary's bearer out from under it, so tick4's
   PRICE_BATCH failed provider_auth_rejection. v3 mints a fresh TOKEN
   immediately before each tick's PRICE_BATCH: 5 TOKEN + 5 PRICE_BATCH = 10
   local starts, on top of v1 6 + v2 6 = 12 inherited, for a family cap of 22.

2. Actor-blind safety gate. The protected paths (latest_quotes, trade_ledger,
   order_scan_state) are legitimately written by the concurrent cadence, and a
   pre/post content hash cannot say who wrote them. v3's primary non-mutation
   proof is process-level: the canary runs under macOS sandbox-exec with
   file-write* denied for the canonical root, and proves that denial from
   inside the process with a bounded EPERM probe sequence. Protected changes
   are then excused only when a fresh, timestamped cadence receipt covers the
   mutation's own mtime. Unattributed changes still fail closed.

An environment variable is explicitly NOT proof, and these tests assert that:
setting one cannot make the CLI proceed, and the source reads no environment.
"""

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

T0 = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PROJECT_ROOT / "tools" / "run_tokmo_pulse_canary_sandboxed.sh"

_MACOS_SANDBOX = sys.platform == "darwin" and os.access(
    "/usr/bin/sandbox-exec", os.X_OK
)
requires_macos_sandbox = pytest.mark.skipif(
    not _MACOS_SANDBOX, reason="requires macOS /usr/bin/sandbox-exec"
)


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


def _config():
    from tokmo.programs.pulse_canary import CanaryConfigV1
    from tokmo.values.session_gate import Market

    return CanaryConfigV1(
        market=Market.KR, symbol="005930", max_ticks=5, max_provider_starts=22
    )


def _denied_attestation(root):
    """An attestation object standing in for a real in-sandbox proof.

    Only run_canary_session consumes this. The CLI never accepts one from a
    caller — it produces its own (see the CLI tests below).
    """
    from tokmo.programs.canary_sandbox_attestation import (
        SANDBOX_MECHANISM,
        CanonicalWriteDenyAttestation,
    )

    return CanonicalWriteDenyAttestation(
        denied=True,
        reason="all_write_probes_denied_by_os",
        mechanism=SANDBOX_MECHANISM,
        canonical_root=str(root),
        probes=(("chmod_root_same_mode", "eperm"),),
    )


def _git_project(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(a, cwd=str(root), capture_output=True, text=True)
    run("git", "init")
    run("git", "config", "user.email", "t@t.com")
    run("git", "config", "user.name", "T")
    (root / "f.txt").write_text("x")
    run("git", "add", ".")
    run("git", "commit", "-m", "init")
    return run("git", "rev-parse", "HEAD").stdout.strip()


# ══════════════════════════════════════════════════════════════════════
# Family accounting: inherited 12 + local 10 = 22
# ══════════════════════════════════════════════════════════════════════


def test_inherited_12_plus_local_10_reaches_exactly_22(tmp_path):
    from tokmo.programs.pulse_canary import CanaryDepsV1, run_canary_session

    token_calls = {"n": 0}

    def token_fetch():
        token_calls["n"] += 1
        return f"token-tick-{token_calls['n']}"

    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=token_fetch,
        quotes_fetch=_make_success_quotes_fetch(),
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=_config(),
        deps=deps,
        t0=T0,
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "all_ticks_complete"
    assert result.inherited_provider_starts == 12
    assert result.local_provider_starts == 10  # 5 TOKEN + 5 PRICE_BATCH
    assert result.total_provider_starts == 22
    assert token_calls["n"] == 5


def test_inherited_13_blocks_before_any_outbound_call(tmp_path):
    from tokmo.programs.pulse_canary import CanaryDepsV1, run_canary_session

    outbound = {"token": 0, "quotes": 0}

    def token_fetch():
        outbound["token"] += 1
        return "token"

    def quotes_fetch(market, symbols, *, bearer=None):
        outbound["quotes"] += 1
        return _make_success_quotes_fetch()(market, symbols, bearer=bearer)

    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=_config(),
        deps=deps,
        t0=T0,
        inherited_provider_starts=13,
    )
    # 13 + 10 needed = 23 > 22, so the session cannot complete: refuse first.
    assert result.stop_reason == "provider_start_cap_exceeded"
    assert outbound == {"token": 0, "quotes": 0}


def test_local_starts_are_five_tokens_and_five_price_batches(tmp_path):
    from tokmo.programs.pulse_canary import CanaryDepsV1, run_canary_session

    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=_make_success_quotes_fetch(),
        sleep_until=lambda t: None,
    )
    evidence_root = tmp_path / "evidence"
    result = run_canary_session(
        root=evidence_root,
        config=_config(),
        deps=deps,
        t0=T0,
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "all_ticks_complete"

    connection = sqlite3.connect(str(evidence_root / "ops" / "canary_evidence.sqlite3"))
    try:
        counts = dict(
            connection.execute(
                "SELECT operation, COUNT(*) FROM provider_start_intent "
                "GROUP BY operation"
            ).fetchall()
        )
    finally:
        connection.close()
    assert counts == {"TOKEN": 5, "PRICE_BATCH": 5}


def test_token_rotation_by_cadence_no_longer_breaks_a_later_tick(tmp_path):
    """The v2 failure mode, replayed against v3.

    The provider stub accepts only the most recently issued token — exactly
    what killed v2's tick4 after the cadence rotated the bearer. Under
    token-per-tick every PRICE_BATCH carries the token minted moments before
    it, so all five ticks succeed.
    """
    from tokmo.programs.pulse_canary import CanaryDepsV1, run_canary_session

    issued = []
    bearers = []

    def token_fetch():
        issued.append(f"tok-{len(issued) + 1}")
        return issued[-1]

    def quotes_fetch(market, symbols, *, bearer=None):
        bearers.append(bearer)
        if bearer != issued[-1]:
            raise RuntimeError("provider_auth_rejection")
        return _make_success_quotes_fetch()(market, symbols, bearer=bearer)

    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=_config(),
        deps=deps,
        t0=T0,
        inherited_provider_starts=12,
    )
    assert result.stop_reason == "all_ticks_complete"
    assert result.completed_ticks == 5
    assert bearers == issued == [f"tok-{n}" for n in range(1, 6)]
    # And the same stub does reject a stale bearer — the v2 shape.
    with pytest.raises(RuntimeError):
        quotes_fetch(None, ("005930",), bearer="tok-1")


# ══════════════════════════════════════════════════════════════════════
# Campaign identity
# ══════════════════════════════════════════════════════════════════════


def test_v3_campaign_constants():
    from tokmo.programs.pulse_canary import (
        CANARY_CAMPAIGN_ID,
        CANARY_CAMPAIGN_VERSION,
        CANARY_FAMILY_CUMULATIVE_CAP,
        CANARY_FAMILY_ID,
        CANARY_MAX_PROVIDER_STARTS_LIMIT,
    )

    assert CANARY_CAMPAIGN_ID == "g25-quote-kr-005930-v4"
    assert CANARY_CAMPAIGN_VERSION == 4
    assert CANARY_FAMILY_ID == "g25-quote-kr-005930"
    assert CANARY_FAMILY_CUMULATIVE_CAP == 32
    assert CANARY_MAX_PROVIDER_STARTS_LIMIT == 32


def test_current_prior_allowlist_is_exactly_v1_v2_v3():
    from tokmo.programs.pulse_canary import _PRIOR_CAMPAIGN_SPECS

    assert [(spec.campaign_id, spec.version) for spec in _PRIOR_CAMPAIGN_SPECS] == [
        ("g25-quote-kr-005930-v1", 1),
        ("g25-quote-kr-005930-v2", 2),
        ("g25-quote-kr-005930-v3", 3),
    ]


def test_v3_purpose_identity_and_evidence_root_are_version_scoped():
    from tokmo.programs.pulse_canary import (
        campaign_evidence_anchor_root,
        compute_purpose_identity,
    )
    from tokmo.values.session_gate import Market

    assert compute_purpose_identity(Market.KR, "005930") == (
        "canary|g25-quote-kr-005930-v4|v4|KR|005930"
    )
    root = str(campaign_evidence_anchor_root())
    assert "g25-quote-kr-005930-v4" in root
    assert "g25-quote-kr-005930-v2" not in root


def test_read_inherited_reads_v1_and_v2_ledgers():
    from tokmo.programs.pulse_canary import read_inherited_provider_starts
    from tokmo.values.session_gate import Market

    base = Path.home() / ".local/share/tokmo-v3-canary/campaigns"
    ledgers = [
        base / f"g25-quote-kr-005930-{v}" / "KR/005930/ops" / "canary_evidence.sqlite3"
        for v in ("v1", "v2")
    ]
    if not all(path.exists() for path in ledgers):
        pytest.skip("v1/v2 runtime ledgers not present")
    assert read_inherited_provider_starts(Market.KR, "005930") == 22


# ══════════════════════════════════════════════════════════════════════
# OS write-deny attestation: the proof itself
# ══════════════════════════════════════════════════════════════════════


def test_attestation_refuses_on_writable_root_without_writing_anything(tmp_path):
    """The unsandboxed path must refuse, and must not have mutated anything.

    The first probe is an idempotent chmod, so a writable root is detected
    before the only probe that would create a file ever runs.
    """
    from tokmo.programs.canary_sandbox_attestation import (
        attest_canonical_write_denied,
    )

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"original")
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    mode_before = stat.S_IMODE(root.stat().st_mode)

    attestation = attest_canonical_write_denied(
        root, nested_relatives=("stores/latest_quotes.sqlite3",)
    )

    assert attestation.denied is False
    assert attestation.reason == "root_write_allowed"
    assert attestation.mechanism == ""
    # No artifact, no mode change: the probe sequence stopped before creating.
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before
    assert stat.S_IMODE(root.stat().st_mode) == mode_before
    assert not any(
        p.name.startswith(".canary_write_deny_probe_") for p in root.rglob("*")
    )


def test_attestation_refuses_when_root_is_absent(tmp_path):
    from tokmo.programs.canary_sandbox_attestation import (
        attest_canonical_write_denied,
    )

    attestation = attest_canonical_write_denied(tmp_path / "nope")
    assert attestation.denied is False
    assert attestation.reason == "canonical_root_absent"


def test_attestation_refuses_when_root_is_owned_by_another_user():
    """EPERM from foreign ownership is not sandbox evidence.

    ``/`` is owned by root and this process is not, so chmod would fail EPERM
    with no sandbox at all. The attestation must reject that path rather than
    read it as a passing proof.
    """
    from tokmo.programs.canary_sandbox_attestation import (
        attest_canonical_write_denied,
    )

    if os.stat("/").st_uid == os.geteuid():
        pytest.skip("running as the owner of /")
    attestation = attest_canonical_write_denied(Path("/"))
    assert attestation.denied is False
    assert attestation.reason == "canonical_root_not_owned_by_process_user"


@requires_macos_sandbox
def test_attestation_holds_under_real_sandbox_exec(tmp_path):
    """End-to-end: run the attestation inside a real sandbox-exec process."""

    # sandbox-exec matches the kernel-resolved path, and pytest's tmp_path is
    # under a symlinked /var on macOS.
    root = (tmp_path / "canonical").resolve()
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"original")

    profile = tmp_path / "profile.sb"
    profile.write_text(
        f'(version 1)\n(allow default)\n(deny file-write* (subpath "{root}"))\n'
    )

    script = tmp_path / "probe.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from tokmo.programs.canary_sandbox_attestation import "
        "attest_canonical_write_denied\n"
        "att = attest_canonical_write_denied(\n"
        "    Path(sys.argv[1]),\n"
        "    nested_relatives=('stores/latest_quotes.sqlite3',))\n"
        "print(json.dumps(att.to_dict()))\n"
    )

    completed = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-f",
            str(profile),
            sys.executable,
            str(script),
            str(root),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
    )
    assert completed.returncode == 0, completed.stderr

    import json

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["denied"] is True
    assert payload["mechanism"] == "macos_sandbox_exec_file_write_deny"
    assert [probe["probe"] for probe in payload["probes"]] == [
        "chmod_root_same_mode",
        "chmod_nested_same_mode",
        "create_unique_file",
    ]
    assert [probe["verdict"] for probe in payload["probes"]] == [
        "eperm",
        "eperm",
        "eperm_no_artifact",
    ]
    # The create probe left nothing behind.
    assert not any(
        p.name.startswith(".canary_write_deny_probe_") for p in root.rglob("*")
    )
    assert (root / "stores" / "latest_quotes.sqlite3").read_bytes() == b"original"


@requires_macos_sandbox
def test_sandbox_denies_a_real_canary_style_write(tmp_path):
    """The deny rule blocks an actual write attempt, not just the probes."""
    root = (tmp_path / "canonical").resolve()
    (root / "stores").mkdir(parents=True)
    target = root / "stores" / "latest_quotes.sqlite3"
    target.write_bytes(b"original")

    profile = tmp_path / "profile.sb"
    profile.write_text(
        f'(version 1)\n(allow default)\n(deny file-write* (subpath "{root}"))\n'
    )
    script = tmp_path / "write.py"
    script.write_text(
        "import sys, errno\n"
        "try:\n"
        "    open(sys.argv[1], 'wb').write(b'canary-was-here')\n"
        "    print('WROTE')\n"
        "except OSError as error:\n"
        "    print('DENIED', error.errno)\n"
    )
    completed = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-f",
            str(profile),
            sys.executable,
            str(script),
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "DENIED 1"  # EPERM
    assert target.read_bytes() == b"original"


def test_attestation_source_reads_no_environment():
    """An env var is a claim, not a proof — neither module may consult one."""
    for relative in (
        "programs/canary_sandbox_attestation.py",
        "programs/pulse_canary.py",
    ):
        text = (PROJECT_ROOT / "src" / "tokmo" / relative).read_text()
        assert "os.environ" not in text, relative
        assert "getenv(" not in text, relative


# ══════════════════════════════════════════════════════════════════════
# CLI: refuses before any credential is assembled
# ══════════════════════════════════════════════════════════════════════


def _cli_refusal(tmp_path, capsys, *, canonical, anchor=None, argv_extra=()):
    from tokmo.programs.pulse_canary import run_canary_cli

    project_root = tmp_path / "project"
    head = _git_project(project_root)
    factory_calls = {"n": 0}

    def factory(**kwargs):
        factory_calls["n"] += 1
        raise AssertionError("credential assembly must not be reached")

    code = run_canary_cli(
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
            *argv_extra,
        ],
        live_deps_factory=factory,
        evidence_root_override=tmp_path / "evidence",
        canonical_root_anchor_override=anchor if anchor is not None else canonical,
    )
    return code, capsys.readouterr().err, factory_calls["n"]


def test_cli_refuses_unsandboxed_before_credential(tmp_path, capsys):
    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"x")

    code, err, factory_calls = _cli_refusal(tmp_path, capsys, canonical=canonical)

    assert code == 1
    assert "sandbox_write_deny_not_proven:root_write_allowed" in err
    assert factory_calls == 0
    # Refusing must not have mutated the canonical root.
    assert not any(
        p.name.startswith(".canary_write_deny_probe_") for p in canonical.rglob("*")
    )


def test_env_var_cannot_forge_the_attestation(tmp_path, capsys, monkeypatch):
    """The v3 draft trusted CANARY_SANDBOX_WRITE_DENY=1. It must not work."""
    monkeypatch.setenv("CANARY_SANDBOX_WRITE_DENY", "1")
    monkeypatch.setenv("CANARY_CANONICAL_WRITE_DENIED_BY_OS", "true")

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"x")

    code, err, factory_calls = _cli_refusal(tmp_path, capsys, canonical=canonical)

    assert code == 1
    assert "sandbox_write_deny_not_proven" in err
    assert factory_calls == 0


def test_cli_refuses_a_decoy_canonical_root(tmp_path, capsys):
    """A caller-chosen unwritable path must not stand in for the real root.

    Without the anchor lock, pointing --canonical-root at any directory the
    process cannot write would produce a passing attestation about a
    directory nobody cares about.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    real_anchor = tmp_path / "canonical"
    real_anchor.mkdir()

    code, err, factory_calls = _cli_refusal(
        tmp_path, capsys, canonical=decoy, anchor=real_anchor
    )

    assert code == 1
    assert "canonical_root_must_be_the_derived_anchor" in err
    assert factory_calls == 0


def test_cli_rejects_any_cap_other_than_22(tmp_path):
    from tokmo.programs.pulse_canary import run_canary_cli

    for value in (6, 10, 15, 21, 23, 100):
        assert (
            run_canary_cli(
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
                    str(value),
                    "--base-commit",
                    "deadbeef",
                ],
                evidence_root_override=tmp_path / "evidence",
            )
            == 1
        ), value


def test_cli_anchor_defaults_to_the_derived_canonical_root():
    from tokmo.programs.pulse_canary import canonical_root_anchor

    assert canonical_root_anchor() == Path.home() / ".local/share/tokmo-v3"


# ══════════════════════════════════════════════════════════════════════
# Cadence receipts: freshness and coverage
# ══════════════════════════════════════════════════════════════════════


def _write_cadence_ledger(canonical: Path, rows) -> None:
    path = canonical / "ops" / "cadence_ticks.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE cadence_tick_receipts("
            "policy_version TEXT, scheduled_for_utc TEXT, status TEXT, "
            "started_at TEXT, finished_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO cadence_tick_receipts VALUES (?,?,?,?,?)", rows
        )
        connection.commit()
    finally:
        connection.close()


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_read_cadence_receipts_parses_the_durable_ledger(tmp_path):
    from tokmo.programs.canary_cadence_attribution import read_cadence_receipts

    canonical = tmp_path / "canonical"
    _write_cadence_ledger(
        canonical,
        [
            ("v7", _iso(T0), "COMPLETE", _iso(T0), _iso(T0 + timedelta(seconds=3))),
        ],
    )
    receipts = read_cadence_receipts(canonical)
    assert len(receipts) == 1
    assert receipts[0].policy_version == "v7"
    assert receipts[0].status == "COMPLETE"
    assert receipts[0].started_at == T0


def test_read_cadence_receipts_returns_empty_when_ledger_absent(tmp_path):
    from tokmo.programs.canary_cadence_attribution import read_cadence_receipts

    assert read_cadence_receipts(tmp_path / "canonical") == ()


def test_stale_receipts_are_not_fresh(tmp_path):
    from tokmo.programs.canary_cadence_attribution import (
        CadenceReceiptWindow,
        fresh_receipts,
    )

    stale = CadenceReceiptWindow(
        policy_version="v7",
        scheduled_for_utc=_iso(T0 - timedelta(hours=2)),
        status="COMPLETE",
        started_at=T0 - timedelta(hours=2),
        finished_at=T0 - timedelta(hours=2) + timedelta(seconds=3),
    )
    assert (
        fresh_receipts((stale,), window_start=T0, window_end=T0 + timedelta(minutes=5))
        == ()
    )


# ══════════════════════════════════════════════════════════════════════
# Safety adjudication under the OS proof
# ══════════════════════════════════════════════════════════════════════


def _cadence_mutating_session(
    tmp_path,
    *,
    receipts,
    attested=True,
    mutate_protected=True,
    mutate_unknown=False,
    protected_mtime=None,
):
    """Run a full session whose 'cadence' rewrites canonical paths."""
    from tokmo.programs.pulse_canary import CanaryDepsV1, run_canary_session

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"original")
    (canonical / "unknown").mkdir(parents=True)
    (canonical / "unknown" / "mystery.json").write_bytes(b"{}")

    clock = {"now": T0}

    def quotes_fetch(market, symbols, *, bearer=None):
        clock["now"] = clock["now"] + timedelta(seconds=1)
        if mutate_protected:
            target = canonical / "stores" / "latest_quotes.sqlite3"
            target.write_bytes(b"cadence-wrote-" + str(clock["now"]).encode())
            stamp = (protected_mtime or clock["now"]).timestamp()
            os.utime(target, (stamp, stamp))
        if mutate_unknown:
            target = canonical / "unknown" / "mystery.json"
            target.write_bytes(b'{"x":1}')
            stamp = clock["now"].timestamp()
            os.utime(target, (stamp, stamp))
        return _make_success_quotes_fetch()(market, symbols, bearer=bearer)

    deps = CanaryDepsV1(
        clock=lambda: clock["now"],
        token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    return run_canary_session(
        root=tmp_path / "evidence",
        config=_config(),
        deps=deps,
        t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
        inherited_provider_starts=12,
        write_deny_attestation=_denied_attestation(canonical) if attested else None,
        cadence_receipt_reader=(lambda: receipts) if receipts is not None else None,
    )


def _receipt(started, finished):
    from tokmo.programs.canary_cadence_attribution import CadenceReceiptWindow

    return CadenceReceiptWindow(
        policy_version="v7",
        scheduled_for_utc=_iso(started),
        status="COMPLETE",
        started_at=started,
        finished_at=finished,
    )


def test_protected_change_is_excused_by_os_proof_plus_fresh_receipt(tmp_path):
    covering = (_receipt(T0, T0 + timedelta(minutes=10)),)
    result = _cadence_mutating_session(tmp_path, receipts=covering)

    assert result.stop_reason == "all_ticks_complete"
    assert result.canary_canonical_write_denied_by_os is True
    assert result.write_deny_mechanism == "macos_sandbox_exec_file_write_deny"
    assert result.total_provider_starts == 22
    assert len(result.cadence_attribution) == 1


def test_protected_change_fails_closed_without_any_receipt(tmp_path):
    result = _cadence_mutating_session(tmp_path, receipts=())
    assert result.stop_reason == "safety_gate_protected_mutation_no_fresh_receipt"


def test_protected_change_fails_closed_with_a_missing_reader(tmp_path):
    result = _cadence_mutating_session(tmp_path, receipts=None)
    assert result.stop_reason == "safety_gate_protected_mutation_no_fresh_receipt"


def test_protected_change_fails_closed_with_a_stale_receipt(tmp_path):
    stale = (
        _receipt(
            T0 - timedelta(hours=2), T0 - timedelta(hours=2) + timedelta(seconds=3)
        ),
    )
    result = _cadence_mutating_session(tmp_path, receipts=stale)
    assert result.stop_reason == "safety_gate_protected_mutation_no_fresh_receipt"


def test_protected_change_outside_the_cadence_window_fails_closed(tmp_path):
    """A fresh receipt that does not cover the mutation's mtime proves nothing.

    The receipt overlaps the session window — so it is fresh — but the write
    is stamped half an hour after that cadence run finished, so the run
    cannot be the actor and the OS proof alone must not excuse it.
    """
    fresh_but_not_covering = (_receipt(T0, T0 + timedelta(seconds=3)),)
    result = _cadence_mutating_session(
        tmp_path,
        receipts=fresh_but_not_covering,
        protected_mtime=T0 + timedelta(minutes=30),
    )
    assert result.stop_reason == (
        "safety_gate_protected_mutation_outside_cadence_window"
    )


def test_protected_change_without_os_proof_fails_closed(tmp_path):
    covering = (_receipt(T0, T0 + timedelta(minutes=10)),)
    result = _cadence_mutating_session(tmp_path, receipts=covering, attested=False)
    assert result.stop_reason == "safety_gate_protected_mutation"
    assert result.canary_canonical_write_denied_by_os is False


def test_unattributed_change_passes_with_os_proof_and_receipt(tmp_path):
    """v3 RCA repair: with OS write-deny + fresh cadence receipt, ALL
    canonical changes are attributed — even files not in the protected
    or expected_mutable lists. The OS proved the canary can't write,
    and the receipt attributes concurrent cadence activity."""
    covering = (_receipt(T0, T0 + timedelta(minutes=10)),)
    result = _cadence_mutating_session(
        tmp_path, receipts=covering, mutate_protected=False, mutate_unknown=True
    )
    assert result.stop_reason == "all_ticks_complete"


def test_protected_and_unattributed_change_passes_with_os_proof_and_receipt(tmp_path):
    """Both protected and unattributed changes pass when OS proved the
    canary can't write and fresh receipts attribute the activity."""
    covering = (_receipt(T0, T0 + timedelta(minutes=10)),)
    result = _cadence_mutating_session(
        tmp_path, receipts=covering, mutate_protected=True, mutate_unknown=True
    )
    assert result.stop_reason == "all_ticks_complete"


def test_clean_canonical_root_passes_with_os_proof(tmp_path):
    from tokmo.programs.pulse_canary import CanaryDepsV1, run_canary_session

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"prot")

    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=_make_success_quotes_fetch(),
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=_config(),
        deps=deps,
        t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
        inherited_provider_starts=12,
        write_deny_attestation=_denied_attestation(canonical),
    )
    assert result.stop_reason == "all_ticks_complete"
    assert result.total_provider_starts == 22
    assert result.canary_canonical_write_denied_by_os is True
    assert result.cadence_attribution == ()


# ══════════════════════════════════════════════════════════════════════
# The sandbox wrapper
# ══════════════════════════════════════════════════════════════════════


def test_wrapper_is_executable_and_syntactically_valid():
    assert WRAPPER.exists(), WRAPPER
    assert os.access(WRAPPER, os.X_OK), "wrapper must be executable"
    assert (
        subprocess.run(
            [shutil.which("bash") or "/bin/bash", "-n", str(WRAPPER)],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_wrapper_denies_writes_to_the_resolved_canonical_root():
    text = WRAPPER.read_text()
    assert "/usr/bin/sandbox-exec" in text
    assert '(deny file-write* (subpath "${CANONICAL_ROOT}"))' in text
    assert "(allow default)" in text
    # The deny rule is a silent no-op unless the path is kernel-resolved.
    assert "cd -P" in text and "pwd -P" in text
    assert 'CANONICAL_ROOT="$(resolve_dir "${CANONICAL_ROOT_RAW}")"' in text


def test_wrapper_fixes_every_parameter_and_exports_no_attestation():
    text = WRAPPER.read_text()
    assert "--max-provider-starts 32" in text
    assert "--max-ticks 5" in text
    assert "--market KR --symbol 005930" in text
    assert "run_tokmo_pulse_canary.py" in text
    # No caller-selectable anything: no argument forwarding.
    assert '"$@"' not in text
    # The wrapper creates the denial; it never asserts it.
    assert "CANARY_SANDBOX_WRITE_DENY" not in text


def test_wrapper_fails_closed_off_macos_and_cleans_up_its_profile():
    text = WRAPPER.read_text()
    assert 'if [[ "$(/usr/bin/uname)" != "Darwin" ]]; then' in text
    assert "if [[ ! -x /usr/bin/sandbox-exec ]]; then" in text
    assert "mktemp" in text
    assert "trap '/bin/rm -f \"${SANDBOX_PROFILE}\"' EXIT" in text


@requires_macos_sandbox
def test_wrapper_refuses_when_head_is_dirty_or_paths_missing(tmp_path):
    """Running the wrapper from a directory with no .venv fails closed.

    This exercises the wrapper's own guards without reaching the canary: no
    provider call, no credential, no network.
    """
    fake_project = tmp_path / "proj"
    (fake_project / "tools").mkdir(parents=True)
    shutil.copy(WRAPPER, fake_project / "tools" / WRAPPER.name)
    os.chmod(fake_project / "tools" / WRAPPER.name, 0o755)

    completed = subprocess.run(
        [str(fake_project / "tools" / WRAPPER.name)], capture_output=True, text=True
    )
    assert completed.returncode == 1
    assert "ERROR:" in completed.stderr
