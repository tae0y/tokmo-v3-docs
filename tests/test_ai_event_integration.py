import sqlite3
from datetime import UTC, datetime, timedelta

from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.material_pack import JobKind
from tokmo.values.ops_kernel import ProducerRole, ReplayResult

START = datetime(2026, 8, 4, tzinfo=UTC)
BINDING = "a" * 64


def run_empty_day(path, ranges):
    provider_calls = telegram = order_write = source_mutations = 0
    for begin, end in ranges:
        store = OpsKernelStore(path)
        for tick_id in range(begin, end):
            at = START + timedelta(minutes=5 * tick_id)
            result = store.create_analysis_requests_with_manifest(
                ProducerRole.ANALYSIS,
                tick_id=tick_id,
                started_at=at,
                material_ticks={JobKind.HOLDINGS: tick_id},
                material_binding_digest=BINDING,
                candidates=(),
                requests=(),
            )
            assert result is ReplayResult.CREATED
            store.seal_manifest(ProducerRole.ANALYSIS, tick_id, at)
    with sqlite3.connect(path) as connection:
        return {
            "manifests": connection.execute(
                "SELECT count(*) FROM manifest_runs"
            ).fetchone()[0],
            "requests": connection.execute(
                "SELECT count(*) FROM analysis_requests"
            ).fetchone()[0],
            "coverage": connection.execute(
                "SELECT count(*) FROM analysis_coverage"
            ).fetchone()[0],
            "dispositions": connection.execute(
                "SELECT count(*) FROM dispositions"
            ).fetchone()[0],
            "outbox": connection.execute(
                "SELECT count(*) FROM alert_outbox"
            ).fetchone()[0],
            "provider_calls": provider_calls,
            "telegram": telegram,
            "order_write": order_write,
            "source_mutations": source_mutations,
        }


def test_288_tick_no_match_day_equals_144_restart_split_and_has_no_external_effect(
    tmp_path,
):
    uninterrupted = run_empty_day(tmp_path / "a.sqlite3", ((0, 288),))
    restarted = run_empty_day(tmp_path / "b.sqlite3", ((0, 144), (144, 288)))
    assert (
        uninterrupted
        == restarted
        == {
            "manifests": 288,
            "requests": 0,
            "coverage": 0,
            "dispositions": 0,
            "outbox": 0,
            "provider_calls": 0,
            "telegram": 0,
            "order_write": 0,
            "source_mutations": 0,
        }
    )


def test_analysis_tables_do_not_store_private_payload_columns(tmp_path):
    store = OpsKernelStore(tmp_path / "privacy.sqlite3")
    with sqlite3.connect(store.path) as connection:
        request_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(analysis_requests)")
        }
        coverage_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(analysis_coverage)")
        }
    forbidden = {
        "account_scope",
        "account_sequence",
        "authorization",
        "credential",
        "prompt",
        "raw_response",
        "raw_provider_body",
        "quantity",
        "price",
        "rationale",
        "filesystem_path",
    }
    assert not request_columns & forbidden
    assert not coverage_columns & forbidden
