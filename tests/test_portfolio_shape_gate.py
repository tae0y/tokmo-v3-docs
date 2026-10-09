"""Durable opt-in read-only portfolio shape gate."""

import ast
import hashlib
import json
import socket
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import tokmo.programs.portfolio_shape_gate as gate_module
from tokmo.programs.portfolio_shape_gate import (
    _load_latest_complete_tick_mask,
    _load_private_scope_resolver,
    main,
    run_portfolio_shape_gate,
)
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.cadence_policy import CADENCE_POLICY_V1, cadence_tick_v1
from tokmo.values.material_pack import JobKind, build_material_pack_v1, canonical_encode
from tokmo.values.open_market_mask import (
    MaskMarketEntryV1,
    OpenMarketMaskV1,
    mask_digest_v1,
)
from tokmo.values.portfolio_calculations import PortfolioScope, ScopeUnresolved
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    CompletenessV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)
from tokmo.values.session_gate import Market

NOW = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
TICK = cadence_tick_v1(CADENCE_POLICY_V1.policy_version, NOW)
PRIVATE_SCOPE = "toss-acct-0123456789abcdef01234567"
SECRET = "gate-secret-canary"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("network forbidden in portfolio shape gate")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _mask():
    entries = {
        Market.KR: MaskMarketEntryV1(
            Market.KR, date(2026, 8, 4), "after_close", None, None
        ),
        Market.US: MaskMarketEntryV1(
            Market.US,
            date(2026, 8, 4),
            "open",
            datetime(2026, 8, 4, 13, 0, tzinfo=UTC),
            datetime(2026, 8, 4, 20, 0, tzinfo=UTC),
        ),
    }
    digest = mask_digest_v1(TICK.policy_version, NOW, TICK.tick_id, entries)
    return OpenMarketMaskV1(
        TICK.policy_version, NOW, TICK.tick_id, frozenset({Market.US}), entries, digest
    )


def _fixture_root(tmp_path):
    root = tmp_path / "runtime"
    mask = _mask()
    payloads = {
        JobKind.HOLDINGS: {
            "accounts": [
                {
                    "account_scope": PRIVATE_SCOPE,
                    "account_type_label": SECRET,
                    "ordinal": 1,
                    "holdings": [
                        {
                            "symbol": "SYN",
                            "quantity": "2",
                            "avg_price": "80",
                            "currency": "USD",
                            "market_raw": "US",
                            "market_field": "US",
                        }
                    ],
                }
            ],
            "global_inventory": True,
        },
        JobKind.QUOTES: {
            "markets": {
                "KR": [],
                "US": [
                    {
                        "symbol": "SYN",
                        "last_price": "100",
                        "currency_raw": "USD",
                        "provider_timestamp": "2026-08-04T14:00:00Z",
                        "fetched_at": "2026-08-04T14:00:00Z",
                        "stored_at": "2026-08-04T14:00:00Z",
                    }
                ],
            }
        },
        JobKind.TRADES: {"accounts": [], "pagination_pending": False},
    }
    writer = MaterialPackWriter(root / "ops", clock=lambda: NOW)
    paths = {}
    for job in JobKind:
        pack = build_material_pack_v1(
            schema_version=1,
            job=job,
            tick_id=TICK.tick_id,
            market_partition=("US",),
            payload=payloads[job],
            captured_at=NOW,
            source_timestamps={"provider": None, "fetched": None, "stored": None},
            parser_version="portfolio-gate-test-v1",
            mask_digest=mask.digest,
        )
        path = writer.write(pack)
        paths[job] = path
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = ReceiptV1(
            1,
            job,
            AttemptV1(TICK.tick_id, AttemptStatus.SAVED, "saved", NOW),
            LastSuccessV1(TICK.tick_id, str(path), sha, NOW, NOW),
            {
                market.value: MarketFreshness(market.value, "fresh", TICK.tick_id, NOW)
                for market in Market
            },
            CompletenessV1(0, 0, False, 0, 0) if job is JobKind.TRADES else None,
            mask_digest=mask.digest,
        )
        ReceiptStore(
            root / "ops" / "receipts" / f"{job.value}.json",
            clock=lambda: NOW,
        ).replace(receipt)
    return root, mask, paths


def _rewrite_last_success(root, job, **changes):
    store = ReceiptStore(
        root / "ops" / "receipts" / f"{job.value}.json",
        clock=lambda: NOW,
    )
    receipt = store.read()
    assert receipt is not None and receipt.last_success is not None
    store.replace(
        replace(receipt, last_success=replace(receipt.last_success, **changes))
    )


@pytest.mark.parametrize("failure", ["sha_mismatch", "deleted", "redirected"])
def test_holdings_verification_failure_never_passes(tmp_path, failure):
    root, mask, paths = _fixture_root(tmp_path)
    if failure == "sha_mismatch":
        _rewrite_last_success(root, JobKind.HOLDINGS, pack_sha256="0" * 64)
    elif failure == "deleted":
        paths[JobKind.HOLDINGS].unlink()
    else:
        redirected = paths[JobKind.QUOTES]
        _rewrite_last_success(
            root,
            JobKind.HOLDINGS,
            pack_path=str(redirected),
            pack_sha256=hashlib.sha256(redirected.read_bytes()).hexdigest(),
        )
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report["gate_status"] == "FAIL"
    assert report["failure_reason"] == "REQUIRED_MATERIAL_NOT_VERIFIED"
    assert report["pack_reads"][JobKind.HOLDINGS.value] != 1
    assert report["write_count"] == 0


@pytest.mark.parametrize("read_shape", ["omitted", "duplicated"])
@pytest.mark.parametrize("job", tuple(JobKind))
def test_no_pass_when_any_receipt_is_not_read_exactly_once(
    tmp_path, monkeypatch, job, read_shape
):
    root, mask, _paths = _fixture_root(tmp_path)
    original = gate_module.assemble_analysis_materials

    def alter_receipt_read(**kwargs):
        if read_shape == "omitted":
            kwargs["receipt_readers"][job] = lambda: None
            return original(**kwargs)
        barrier = original(**kwargs)
        kwargs["receipt_readers"][job]()
        return barrier

    monkeypatch.setattr(gate_module, "assemble_analysis_materials", alter_receipt_read)
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    expected_reads = 0 if read_shape == "omitted" else 2
    assert report["receipt_reads"][job.value] == expected_reads
    assert report["gate_status"] == "FAIL"
    assert report["failure_reason"] == "REQUIRED_MATERIAL_NOT_VERIFIED"


def test_holdings_verified_with_trades_absent_remains_pass(tmp_path):
    root, mask, paths = _fixture_root(tmp_path)
    paths[JobKind.TRADES].unlink()
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report["gate_status"] == "PASS"
    assert report["receipt_reads"] == {job.value: 1 for job in JobKind}
    assert report["pack_reads"] == {
        JobKind.HOLDINGS.value: 1,
        JobKind.QUOTES.value: 1,
        JobKind.TRADES.value: 0,
    }
    assert report["material_count"] == 2


def test_all_packs_deleted_remains_fail(tmp_path):
    root, mask, paths = _fixture_root(tmp_path)
    for path in paths.values():
        path.unlink()
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report["gate_status"] == "FAIL"
    assert report["failure_reason"] == "REQUIRED_MATERIAL_NOT_VERIFIED"
    assert report["write_count"] == 0


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_unsafe_protected_evidence_returns_sanitized_fail(tmp_path, link_kind):
    root, mask, paths = _fixture_root(tmp_path)
    planted = paths[JobKind.HOLDINGS].with_name(f"unsafe-{link_kind}.json")
    if link_kind == "symlink":
        planted.symlink_to(paths[JobKind.HOLDINGS])
    else:
        planted.hardlink_to(paths[JobKind.HOLDINGS])
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report["gate_status"] == "FAIL"
    assert report["failure_reason"] == "PROTECTED_EVIDENCE_INVALID"
    assert report["write_count"] == 0
    rendered = json.dumps(report, sort_keys=True)
    assert str(root) not in rendered
    assert PRIVATE_SCOPE not in rendered


def _install_latest_complete_cadence(root, mask):
    database = root / "ops" / "cadence_ticks.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE cadence_tick_receipts(
              policy_version TEXT, scheduled_for_utc TEXT, tick_id INTEGER,
              status TEXT, mask_digest TEXT, open_markets_json TEXT)
        """)
        connection.execute(
            "INSERT INTO cadence_tick_receipts VALUES(?,?,?,?,?,?)",
            (
                TICK.policy_version,
                "2026-08-04T14:00:00Z",
                TICK.tick_id,
                "COMPLETE",
                mask.digest,
                '["US"]',
            ),
        )
    calendar = {
        "schema_version": 2,
        "kr": {
            "trade_date": "2026-08-04",
            "sessions": {
                "regularMarket": {
                    "start": "2026-08-04T00:00:00Z",
                    "end": "2026-08-04T06:00:00Z",
                }
            },
            "source_trade_date": "2026-08-04",
            "fetched_at": "2026-08-04T13:59:00Z",
            "schema_version": 2,
            "parser_version": "portfolio-gate-test-v1",
        },
        "us": {
            "trade_date": "2026-08-04",
            "sessions": {
                "regularMarket": {
                    "start": "2026-08-04T13:00:00Z",
                    "end": "2026-08-04T20:00:00Z",
                }
            },
            "source_trade_date": "2026-08-04",
            "fetched_at": "2026-08-04T13:59:00Z",
            "schema_version": 2,
            "parser_version": "portfolio-gate-test-v1",
        },
        "updated_at": "2026-08-04T13:59:00Z",
    }
    (root / "ops" / "calendar_cache.json").write_bytes(canonical_encode(calendar))


def _json_output(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _valid_scope_map(tmp_path):
    path = tmp_path / f"private-map-{SECRET}.json"
    path.write_bytes(canonical_encode({PRIVATE_SCOPE: "personal"}))
    path.chmod(0o600)
    return path


def test_cli_valid_scope_map_passes_with_symmetric_immutable_evidence(
    tmp_path, monkeypatch, capsys
):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    scope_map = _valid_scope_map(tmp_path)
    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    assert main(["--opt-in", "--scope-map-file", str(scope_map)]) == 0
    output = _json_output(capsys)
    assert output["gate_status"] == "PASS"
    assert output["before_after_equal"] is True
    assert output["result_status"] == "COMPLETE"
    assert output["digest_replay"] == "PASS"
    assert output["reconciliation"] == "PASS"
    rendered = json.dumps(output, sort_keys=True)
    assert str(scope_map) not in rendered
    assert PRIVATE_SCOPE not in rendered
    assert SECRET not in rendered


@pytest.mark.parametrize("mutation", ["existing", "added"])
def test_cli_valid_scope_map_detects_protected_evidence_mutation(
    tmp_path, monkeypatch, capsys, mutation
):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    scope_map = _valid_scope_map(tmp_path)
    original = gate_module.run_portfolio_shape_gate

    def mutate_after_gate(**kwargs):
        report = original(**kwargs)
        if mutation == "existing":
            target = root / "ops" / "receipts" / "holdings.json"
        else:
            target = root / "ops" / "receipts" / "new-evidence.json"
        target.write_bytes(b"changed-after-gate")
        return report

    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    monkeypatch.setattr(gate_module, "run_portfolio_shape_gate", mutate_after_gate)
    assert main(["--opt-in", "--scope-map-file", str(scope_map)]) == 1
    output = _json_output(capsys)
    assert output["gate_status"] == "FAIL"
    assert output["failure_reason"] == "IMMUTABLE_EVIDENCE_CHANGED"
    rendered = json.dumps(output)
    assert str(scope_map) not in rendered
    assert PRIVATE_SCOPE not in rendered
    assert SECRET not in rendered


def test_cli_valid_scope_map_detects_scope_map_mutation(tmp_path, monkeypatch, capsys):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    scope_map = _valid_scope_map(tmp_path)
    original = gate_module.run_portfolio_shape_gate

    def mutate_map_after_gate(**kwargs):
        report = original(**kwargs)
        scope_map.write_bytes(canonical_encode({PRIVATE_SCOPE: "pension"}))
        return report

    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    monkeypatch.setattr(gate_module, "run_portfolio_shape_gate", mutate_map_after_gate)
    assert main(["--opt-in", "--scope-map-file", str(scope_map)]) == 1
    output = _json_output(capsys)
    assert output["gate_status"] == "FAIL"
    assert output["failure_reason"] == "IMMUTABLE_EVIDENCE_CHANGED"
    rendered = json.dumps(output)
    assert str(scope_map) not in rendered
    assert PRIVATE_SCOPE not in rendered
    assert SECRET not in rendered


def test_cli_no_scope_map_pass_is_retained(tmp_path, monkeypatch, capsys):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    assert main(["--opt-in"]) == 0
    output = _json_output(capsys)
    assert output["gate_status"] == "PASS"
    assert output["before_after_equal"] is True
    assert output["result_status"] == "SCOPE_RESOLUTION_INCOMPLETE"


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode"])
def test_cli_unsafe_scope_maps_remain_typed_fail(tmp_path, monkeypatch, capsys, unsafe):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    valid = _valid_scope_map(tmp_path)
    supplied = tmp_path / f"supplied-{unsafe}.json"
    if unsafe == "symlink":
        supplied.symlink_to(valid)
    elif unsafe == "hardlink":
        supplied.hardlink_to(valid)
    else:
        supplied.write_bytes(valid.read_bytes())
        supplied.chmod(0o644)
    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    assert main(["--opt-in", "--scope-map-file", str(supplied)]) == 1
    output = _json_output(capsys)
    assert output == {"gate_status": "FAIL", "failure_reason": "SCOPE_MAP_INVALID"}
    rendered = json.dumps(output)
    assert str(supplied) not in rendered
    assert PRIVATE_SCOPE not in rendered
    assert SECRET not in rendered


def test_cli_refuses_noncanonical_root(tmp_path, monkeypatch, capsys):
    canonical = tmp_path / "canonical"
    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: canonical)
    assert main(["--opt-in", "--root", str(tmp_path / "other")]) == 1
    assert _json_output(capsys) == {
        "gate_status": "REFUSED",
        "reason": "NONCANONICAL_ROOT",
    }


@pytest.mark.parametrize("create_root", [False, True])
def test_cli_skips_only_genuinely_unavailable_canonical_runtime(
    tmp_path, monkeypatch, capsys, create_root
):
    canonical = tmp_path / "canonical"
    if create_root:
        canonical.mkdir()
    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: canonical)
    assert main(["--opt-in"]) == 0
    assert _json_output(capsys) == {
        "gate_status": "SKIPPED",
        "reason": "CANONICAL_RUNTIME_UNAVAILABLE",
    }


def test_cli_missing_requested_scope_map_is_fail_not_skip(
    tmp_path, monkeypatch, capsys
):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    missing = tmp_path / f"{PRIVATE_SCOPE}-missing-map.json"
    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    assert main(["--opt-in", "--scope-map-file", str(missing)]) == 1
    output = _json_output(capsys)
    assert output == {"gate_status": "FAIL", "failure_reason": "SCOPE_MAP_INVALID"}
    rendered = json.dumps(output)
    assert str(root) not in rendered and PRIVATE_SCOPE not in rendered


def test_cli_protected_evidence_vanished_mid_run_is_fail(tmp_path, monkeypatch, capsys):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    original = gate_module._load_latest_complete_tick_mask

    def vanish_after_load(value):
        loaded = original(value)
        (root / "ops" / "receipts" / "holdings.json").unlink()
        return loaded

    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    monkeypatch.setattr(
        gate_module, "_load_latest_complete_tick_mask", vanish_after_load
    )
    assert main(["--opt-in"]) == 1
    output = _json_output(capsys)
    assert output["gate_status"] == "FAIL"
    assert output["failure_reason"] == "IMMUTABLE_EVIDENCE_CHANGED"
    assert str(root) not in json.dumps(output)


def test_cli_runtime_unavailable_does_not_mask_vanished_evidence(
    tmp_path, monkeypatch, capsys
):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)

    def vanish_then_unavailable(_value):
        (root / "ops" / "receipts" / "holdings.json").unlink()
        raise gate_module.CanonicalRuntimeUnavailable

    monkeypatch.setattr(gate_module, "_production_canonical_root", lambda: root)
    monkeypatch.setattr(
        gate_module, "_load_latest_complete_tick_mask", vanish_then_unavailable
    )
    assert main(["--opt-in"]) == 1
    output = _json_output(capsys)
    assert output == {
        "gate_status": "FAIL",
        "failure_reason": "IMMUTABLE_EVIDENCE_CHANGED",
    }


def test_receipt_pack_path_traversal_fails_required_material_gate(tmp_path):
    root, mask, _paths = _fixture_root(tmp_path)
    traversal = root / "ops" / "packs" / ".." / ".." / ".." / "outside.json"
    _rewrite_last_success(
        root, JobKind.HOLDINGS, pack_path=str(traversal), pack_sha256="0" * 64
    )
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report["gate_status"] == "FAIL"
    assert report["failure_reason"] == "REQUIRED_MATERIAL_NOT_VERIFIED"
    assert str(root) not in json.dumps(report)


def test_latest_complete_loader_explicitly_closes_sqlite_connection(
    tmp_path, monkeypatch
):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    original = sqlite3.connect
    closed = []

    class TrackingConnection:
        def __init__(self, connection):
            object.__setattr__(self, "connection", connection)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __setattr__(self, name, value):
            setattr(self.connection, name, value)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def close(self):
            closed.append(True)
            self.connection.close()

    monkeypatch.setattr(
        gate_module.sqlite3,
        "connect",
        lambda *args, **kwargs: TrackingConnection(original(*args, **kwargs)),
    )
    tick, loaded, status = _load_latest_complete_tick_mask(root)
    assert (tick, loaded, status) == (TICK, mask, "COMPLETE")
    assert closed == [True]


def test_account_scope_regex_detects_leak_without_private_marker_list(
    tmp_path, monkeypatch
):
    root, mask, _paths = _fixture_root(tmp_path)
    original = gate_module.compute_portfolio_snapshot

    class LeakyResult:
        def __init__(self, result):
            self.result = result

        def __getattr__(self, name):
            return getattr(self.result, name)

        def __repr__(self):
            return f"LeakyResult({PRIVATE_SCOPE})"

    monkeypatch.setattr(
        gate_module,
        "compute_portfolio_snapshot",
        lambda *args, **kwargs: LeakyResult(original(*args, **kwargs)),
    )
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda raw: (
            PortfolioScope.PERSONAL
            if raw == PRIVATE_SCOPE
            else ScopeUnresolved.UNRESOLVED
        ),
        private_markers=(),
    )
    assert report["privacy"] == "FAIL"
    assert report["gate_status"] == "FAIL"


def test_latest_complete_loader_reconstructs_exact_canonical_mask(tmp_path):
    root, mask, _paths = _fixture_root(tmp_path)
    _install_latest_complete_cadence(root, mask)
    tick, loaded, status = _load_latest_complete_tick_mask(root)
    assert tick == TICK
    assert loaded == mask
    assert status == "COMPLETE"


def test_private_scope_map_requires_mode_0600_and_never_enters_output(tmp_path):
    path = tmp_path / "scope-map.json"
    path.write_bytes(canonical_encode({PRIVATE_SCOPE: "personal"}))
    path.chmod(0o600)
    resolver, markers = _load_private_scope_resolver(path)
    assert resolver(PRIVATE_SCOPE) is PortfolioScope.PERSONAL
    assert resolver("toss-acct-ffffffffffffffffffffffff") is ScopeUnresolved.UNRESOLVED
    assert markers == (PRIVATE_SCOPE,)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe scope map"):
        _load_private_scope_resolver(path)


def test_durable_gate_success_reconciles_and_recomputes_digest(tmp_path):
    root, mask, _paths = _fixture_root(tmp_path)
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda raw: (
            PortfolioScope.PERSONAL
            if raw == PRIVATE_SCOPE
            else ScopeUnresolved.UNRESOLVED
        ),
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report == {
        "gate_status": "PASS",
        "tick_id": TICK.tick_id,
        "cadence_status": "INJECTED_FIXTURE",
        "open_markets": ["US"],
        "receipt_statuses": {job.value: "saved" for job in JobKind},
        "receipt_reads": {job.value: 1 for job in JobKind},
        "pack_reads": {job.value: 1 for job in JobKind},
        "verified_pack_count": 3,
        "material_count": 3,
        "result_status": "COMPLETE",
        "result_reason": None,
        "unresolved_account_count": 0,
        "privacy": "PASS",
        "digest_replay": "PASS",
        "reconciliation": "PASS",
        "before_after_equal": True,
        "provider_calls": 0,
        "network_calls": 0,
        "telegram_calls": 0,
        "order_calls": 0,
        "write_count": 0,
        "pension_live_subcase": "UNVERIFIED_UD-1",
    }


def test_durable_gate_without_mapping_is_typed_fail_closed(tmp_path):
    root, mask, _paths = _fixture_root(tmp_path)
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report["gate_status"] == "PASS"
    assert report["result_status"] == "SCOPE_RESOLUTION_INCOMPLETE"
    assert report["unresolved_account_count"] == 1
    assert report["privacy"] == "PASS"
    assert report["digest_replay"] == "NOT_REACHED_SCOPE_FAIL_CLOSED"
    assert report["reconciliation"] == "NOT_REACHED_SCOPE_FAIL_CLOSED"
    assert report["before_after_equal"] is True


def test_durable_gate_detects_mid_gate_pack_hash_mutation(tmp_path, monkeypatch):
    root, mask, paths = _fixture_root(tmp_path)
    original = MaterialPackWriter.read_and_verify
    mutated = False

    def mutate_then_read(self, path, expected_sha):
        nonlocal mutated
        if not mutated:
            paths[JobKind.HOLDINGS].write_bytes(b"tampered")
            mutated = True
        return original(self, path, expected_sha)

    monkeypatch.setattr(MaterialPackWriter, "read_and_verify", mutate_then_read)
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    assert report["gate_status"] == "FAIL"
    assert report["failure_reason"] == "IMMUTABLE_EVIDENCE_CHANGED"
    assert report["before_after_equal"] is False
    assert report["write_count"] == 0


def test_cli_default_is_clean_skip_without_touching_runtime(capsys):
    assert main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "gate_status": "SKIPPED",
        "reason": "EXPLICIT_OPT_IN_REQUIRED",
    }


def test_sanitized_output_contains_no_scope_secret_or_path(tmp_path):
    root, mask, _paths = _fixture_root(tmp_path)
    report = run_portfolio_shape_gate(
        root=root,
        tick=TICK,
        mask=mask,
        resolve_scope=lambda _raw: ScopeUnresolved.UNRESOLVED,
        private_markers=(PRIVATE_SCOPE, SECRET),
    )
    rendered = json.dumps(report, sort_keys=True)
    assert PRIVATE_SCOPE not in rendered
    assert SECRET not in rendered
    assert str(root) not in rendered
    assert "account_scope" not in rendered


def test_gate_module_has_no_write_network_messaging_or_order_surface():
    source = Path("src/tokmo/programs/portfolio_shape_gate.py").read_text()
    tree = ast.parse(source)
    forbidden_import_roots = {
        "requests",
        "httpx",
        "urllib.request",
        "socket",
        "subprocess",
    }
    imported = set()
    forbidden_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {
                "write",
                "write_bytes",
                "write_text",
                "replace",
                "unlink",
                "send",
                "post",
                "put",
                "patch",
                "delete",
                "place_order",
            }:
                forbidden_calls.append((node.func.attr, node.lineno))
    assert not any(
        name == root or name.startswith(root + ".")
        for name in imported
        for root in forbidden_import_roots
    )
    assert forbidden_calls == []
    assert not any(
        token in source.upper()
        for token in (
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "CREATE TABLE",
        )
    )
