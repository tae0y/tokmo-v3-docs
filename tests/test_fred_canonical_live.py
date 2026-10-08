from datetime import date, datetime, timedelta, timezone
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import importlib.util
from pathlib import Path
import fcntl
import pytest
import urllib.error

from tokmo.values import fred_macro
from tokmo.records.fred_material_store import FredMaterialStore
from tokmo.records.fred_material_store import DDL_V3, _schema_semantics
from tokmo.facts.fred import build_query_pairs
from tokmo.facts.fred_live import FredLiveTransport

OFFICIAL_AUDIT_PROVENANCE = {
    "series": ("https://fred.stlouisfed.org/docs/api/fred/series.html", "b293a17e3c76200c69b276e2f0e8edcec05064299ba01b56b4af2b25d5fdcc53"),
    "series_observations": ("https://fred.stlouisfed.org/docs/api/fred/series_observations.html", "2367ce88cb0f34f18e74181e1bcd78d884f3408e68d338ccc3f23af5b88833f6"),
    "series_release": ("https://fred.stlouisfed.org/docs/api/fred/series_release.html", "615616b36011d255588c78ebda0858734c19767ec94f584413d3a8420ecff24d"),
    "release_dates": ("https://fred.stlouisfed.org/docs/api/fred/release_dates.html", "17cfda100487e0dfb269af0cf75a63423fe6a612d6bce2e5b7f6e58fac0cd4ec"),
    "series_vintagedates": ("https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html", "8454837098b7774552a6ec0de02bedb8b2e552e0a6e296da11b9c00255b43c30"),
    "errors": ("https://fred.stlouisfed.org/docs/api/fred/errors.html", "cb5d9b8eef61ca6a27cc33b1a13ac9413a071db88373a771171cd727d90d0fd3"),
}


def _live_tool():
    path = Path(__file__).parents[1] / "tools" / "fred_canonical_live.py"
    spec = importlib.util.spec_from_file_location("fred_canonical_live_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_g_live_identity_and_closed_manifest_are_stable():
    assert fred_macro.CAMPAIGN_CODE == "FRED_G_LIVE_PAYEMS_R1"
    assert fred_macro.MANIFEST_CODE == "FRED_G_LIVE_PAYEMS_FIVE_R1"
    assert fred_macro.CAMPAIGN_CAP == 10
    assert fred_macro.PURPOSE_CUMULATIVE_CEILING == 20
    manifest = fred_macro.g_live_manifest(release_id=17)
    assert [request.operation.value for request in manifest] == [
        "SERIES", "SERIES_OBSERVATIONS", "SERIES_RELEASE",
        "RELEASE_DATES", "SERIES_VINTAGE_DATES",
    ]
    assert manifest[0].realtime_start == manifest[0].realtime_end == date(2026, 8, 12)
    assert manifest[1].output_type == 4
    assert (manifest[1].observation_start, manifest[1].observation_end) == (
        date(2026, 7, 1), date(2026, 7, 31))
    assert manifest[2].realtime_start == manifest[2].realtime_end == date(2026, 8, 12)
    assert manifest[3].release_id == 17 and manifest[3].realtime_start is None
    assert manifest[4].limit == 10 and manifest[4].realtime_start is None
    assert fred_macro.MANIFEST_DIGEST == "1ca30ee6c3ce6442e31f2c31a09461d6407bad8c94dce6be97807540056386a1"
    mutated = fred_macro.canonical_json({"digest": fred_macro.MANIFEST_DIGEST,
                                         "release_placeholder_ordinal": 4})
    assert __import__("hashlib").sha256(mutated).hexdigest() != fred_macro.MANIFEST_DIGEST


def test_g_live_manifest_rejects_invalid_release_placeholder():
    for value in (None, True, 0):
        try:
            fred_macro.g_live_manifest(release_id=value)
        except ValueError as error:
            assert str(error) == "invalid_release_id"
        else:
            raise AssertionError("invalid release identity accepted")


def test_store_v4_schema_and_immutable_inspection_are_noncreating(tmp_path):
    absent = tmp_path / "absent"
    assert FredMaterialStore.inspect_existing(absent).code == "ABSENT"
    assert not absent.exists()
    store = FredMaterialStore(tmp_path / "private")
    before = {path.name: (path.stat().st_mtime_ns, path.stat().st_size)
              for path in store.root.iterdir()}
    inspection = FredMaterialStore.inspect_existing(store.root)
    after = {path.name: (path.stat().st_mtime_ns, path.stat().st_size)
             for path in store.root.iterdir()}
    assert inspection.code == "PRESENT" and inspection.schema_fingerprint_ok
    assert before == after
    assert not any(os.path.exists(f"{store.path}{suffix}")
                   for suffix in ("-journal", "-wal", "-shm"))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute("SELECT count(*) FROM campaign").fetchone() == (0,)


def test_explicit_v3_to_v4_migration_backup_receipt_and_readback(tmp_path):
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    database = root / "fred.sqlite3"
    database.touch(mode=0o600)
    with sqlite3.connect(database) as connection:
        connection.executescript(DDL_V3)
        connection.execute("INSERT INTO campaign VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, fred_macro.SOURCE, fred_macro.PURPOSE, fred_macro.CAMPAIGN_CODE,
            fred_macro.CODE_REVISION, fred_macro.MANIFEST_CODE, fred_macro.MANIFEST_REVISION,
            fred_macro.MANIFEST_DIGEST, "LIVE", "PAYEMS", 10, "QUARANTINED", "bad_request"))
        request = fred_macro.g_live_manifest(release_id=1)[0]
        connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            fred_macro.SOURCE, fred_macro.PURPOSE, "a" * 64, 1, request.operation.value,
            request.request_identity, "LIVE", "2026-08-12T00:00:00.000000Z", "FAILED",
            fred_macro.CAMPAIGN_CODE, fred_macro.MANIFEST_REVISION, fred_macro.MANIFEST_DIGEST))
    lock = tmp_path / "ns" / "lock"
    receipt = module.migrate_r2_schema(root, lock_path=lock, migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    assert receipt["from_user_version"] == 3 and receipt["to_user_version"] == 4
    assert receipt["pre_database_sha256"] == receipt["backup_sha256"]
    assert receipt["pre_database_sha256"] != receipt["post_database_sha256"]
    assert module.readback_r2(root)["campaigns"] == 2
    assert stat.S_IMODE((root / receipt["backup_basename"]).stat().st_mode) == 0o600


def test_r2_migration_is_idempotent_and_rollback_is_prestart_only(tmp_path):
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    database = root / "fred.sqlite3"
    database.touch(mode=0o600)
    with sqlite3.connect(database) as connection:
        connection.executescript(DDL_V3)
        connection.execute("INSERT INTO campaign VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, fred_macro.SOURCE, fred_macro.PURPOSE, fred_macro.CAMPAIGN_CODE,
            fred_macro.CODE_REVISION, fred_macro.MANIFEST_CODE, fred_macro.MANIFEST_REVISION,
            fred_macro.MANIFEST_DIGEST, "LIVE", "PAYEMS", 10, "QUARANTINED", "bad_request"))
        request = fred_macro.g_live_manifest(release_id=1)[0]
        connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            fred_macro.SOURCE, fred_macro.PURPOSE, "b" * 64, 1, request.operation.value,
            request.request_identity, "LIVE", "2026-08-12T00:00:00.000000Z", "FAILED",
            fred_macro.CAMPAIGN_CODE, fred_macro.MANIFEST_REVISION, fred_macro.MANIFEST_DIGEST))
    lock = tmp_path / "ns" / "lock"
    receipt = module.migrate_r2_schema(root, lock_path=lock, migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    assert module.migrate_r2_schema(root, lock_path=lock, migrated_at=datetime(2026,8,13,tzinfo=timezone.utc)) == receipt
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert module.rollback_r2_schema(root, lock_path=lock)["code"] == "rollback_ok"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)


def test_live_campaign_pin_and_fixture_guard_precede_mutation(tmp_path):
    from datetime import datetime, timezone
    root = tmp_path / "private"
    store = FredMaterialStore(root)
    store.ensure_live_campaign()
    before = store.counts()
    request = fred_macro.g_live_manifest(release_id=17)[0]
    try:
        store.reserve(request, execution_mode=fred_macro.ExecutionMode.FIXTURE,
                      started_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    except ValueError as error:
        assert str(error) == "fixture_live_campaign_forbidden"
    else:
        raise AssertionError("fixture operation reached a LIVE campaign")
    assert store.counts() == before


def test_exact_manifest_query_fields_and_realtime_exception():
    manifest = fred_macro.g_live_manifest(release_id=50)
    pairs = [build_query_pairs(request) for request in manifest]
    assert [tuple(key for key, _ in item) for item in pairs] == [
        ("series_id","realtime_start","realtime_end","file_type"),
        ("series_id","realtime_start","realtime_end","observation_start","observation_end","units","output_type","sort_order","limit","offset","file_type"),
        ("series_id","realtime_start","realtime_end","file_type"),
        ("release_id","include_release_dates_with_no_data","sort_order","limit","offset","file_type"),
        ("series_id","sort_order","limit","offset","file_type"),
    ]


def test_r2_wire_manifest_is_exact_reviewed_builder_output():
    operations = []
    for index, request in enumerate(fred_macro.g_live_manifest(release_id=1), 1):
        operation: dict[str, object] = {"operation": request.operation.value}
        for key, value in build_query_pairs(request):
            if index == 4 and key == "release_id":
                operation[key] = {"manifest_ordinal": 3, "type": "operation_3_release_id"}
            elif key in request.__dataclass_fields__:
                declared = getattr(request, key)
                operation[key] = declared if type(declared) in (bool, int) else value
            else:
                operation[key] = value
        operations.append(operation)
    actual = fred_macro.canonical_json({
        "campaign_code": fred_macro.R2_CAMPAIGN_CODE,
        "manifest_code": fred_macro.R2_MANIFEST_CODE,
        "manifest_revision": fred_macro.R2_MANIFEST_REVISION,
        "operations": operations,
    })
    expected = b'{"campaign_code":"FRED_G_LIVE_PAYEMS_R2","manifest_code":"FRED_G_LIVE_PAYEMS_FIVE_R2","manifest_revision":"fred-canonical-stage3-g-live-r2-diagnostic-v1","operations":[{"file_type":"json","operation":"SERIES","realtime_end":"2026-08-12","realtime_start":"2026-08-12","series_id":"PAYEMS"},{"file_type":"json","limit":10,"observation_end":"2026-07-31","observation_start":"2026-07-01","offset":0,"operation":"SERIES_OBSERVATIONS","output_type":4,"realtime_end":"2026-08-12","realtime_start":"2026-08-12","series_id":"PAYEMS","sort_order":"asc","units":"lin"},{"file_type":"json","operation":"SERIES_RELEASE","realtime_end":"2026-08-12","realtime_start":"2026-08-12","series_id":"PAYEMS"},{"file_type":"json","include_release_dates_with_no_data":false,"limit":10,"offset":0,"operation":"RELEASE_DATES","release_id":{"manifest_ordinal":3,"type":"operation_3_release_id"},"sort_order":"desc"},{"file_type":"json","limit":10,"offset":0,"operation":"SERIES_VINTAGE_DATES","series_id":"PAYEMS","sort_order":"asc"}]}\n'
    assert actual == expected
    assert __import__("hashlib").sha256(expected).hexdigest() == fred_macro.R2_MANIFEST_DIGEST


class _Http400Opener:
    def __init__(self, body, *, encoding=None, read_error=False):
        self.body, self.encoding, self.read_error = body, encoding, read_error
        self.error = None
    def open(self, request, timeout):
        class Body:
            def __init__(inner): inner.reads = inner.closes = 0
            def read(inner, size):
                inner.reads += 1
                assert size == 4097
                if self.read_error: raise OSError("private secret")
                return self.body
            def close(inner): inner.closes += 1
        stream = Body()
        error = urllib.error.HTTPError(request.full_url, 400, "private secret", {}, stream)
        error.headers["Content-Encoding"] = self.encoding
        self.error = stream
        raise error


@pytest.mark.parametrize(("body", "expected"), [
    (b'{"error_code":400,"error_message":"Bad Request.  The value for variable api_key is not registered.  Read https://fred.stlouisfed.org/docs/api/api_key.html for more information."}', "api_key_not_registered"),
    (b'{"error_message":"Bad Request.  The value for variable api_key is not registered.  Read https://fred.stlouisfed.org/docs/api/api_key.html for more information.","error_code":400}', "api_key_not_registered"),
    (b'{"error_code":"400","error_message":"Bad Request"}', "provider_bad_request_unknown"),
    (b'{"error_code":400,"error_code":400,"error_message":"Bad Request"}', "provider_bad_request_unknown"),
    (b'not-json', "provider_bad_request_unknown"),
    (b'x' * 4097, "provider_bad_request_unknown"),
])
def test_http_400_classifier_is_closed_single_read_and_close(body, expected):
    opener = _Http400Opener(body)
    outcome = FredLiveTransport(opener=opener)(fred_macro.g_live_manifest(release_id=1)[0], "a" * 32)
    assert outcome.code == expected
    assert (opener.error.reads, opener.error.closes) == (1, 1)
    assert "secret" not in repr(outcome) and "https://" not in repr(outcome)


def test_release_handoff_rejects_bool_and_reads_exact_live_operation_three(tmp_path):
    from datetime import datetime, timezone
    store = FredMaterialStore(tmp_path / "live")
    store.ensure_live_campaign()
    manifest = fred_macro.g_live_manifest(release_id=50)
    reservations = []
    for index, request in enumerate(manifest[:3], 1):
        reservation = store.reserve(request, execution_mode=fred_macro.ExecutionMode.LIVE,
            started_at=datetime(2026,8,12,0,0,index,tzinfo=timezone.utc))
        reservations.append(reservation)
        if index < 3:
            store.settle(reservation, "SUCCEEDED")
    store.store_page(({"id":50,"series_id":"PAYEMS"},), reservation=reservations[2],
        operation="SERIES_RELEASE", fetched_at=datetime(2026,8,12,0,0,3,tzinfo=timezone.utc))
    kwargs = dict(mode="CURRENT_CONSTRUCTION", source=fred_macro.SOURCE,
        purpose=fred_macro.PURPOSE, campaign_code=fred_macro.CAMPAIGN_CODE,
        manifest_revision=fred_macro.MANIFEST_REVISION,
        manifest_digest=fred_macro.MANIFEST_DIGEST,
        invocation_identity=reservations[2].invocation_identity, ordinal=3,
        execution_mode=fred_macro.ExecutionMode.LIVE,
        current_run_identity=reservations[2].invocation_identity)
    assert store.read_release_id_for_campaign(**kwargs) == 50
    with pytest.raises(ValueError, match="release_handoff_invalid"):
        store.read_release_id_for_campaign(**{**kwargs, "current_run_identity": "f" * 64})
    reconstruction = {**kwargs, "mode": "RECONSTRUCTION", "current_run_identity": None}
    assert store.read_release_id_for_campaign(**reconstruction) == 50
    with sqlite3.connect(store.path) as connection:
        payload = connection.execute("SELECT source_canonical_bytes FROM materials").fetchone()[0]
        connection.execute("UPDATE materials SET source_canonical_bytes=?", (payload.replace(b"50", b"true"),))
    with pytest.raises(ValueError, match="release_handoff_invalid"):
        store.read_release_id_for_campaign(**kwargs)


def test_gate_and_lock_contention_are_zero_read_zero_start_and_noncreating(tmp_path):
    module = _live_tool()
    root, lock, credential = tmp_path / "root", tmp_path / "ns" / "lock", tmp_path / "key"
    forbidden = module.run_initial(root=root, credential_path=credential, lock_path=lock,
        execution_allowed=False, wall_clock=lambda: None, monotonic_clock=lambda: None,
        sleeper=lambda _: None, api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    assert forbidden["code"] == "g_live_forbidden" and not root.exists() and not lock.exists()
    lock.parent.mkdir(mode=0o700)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        blocked = module.run_initial(root=root, credential_path=credential, lock_path=lock,
            execution_allowed=True, wall_clock=lambda: None, monotonic_clock=lambda: None,
            sleeper=lambda _: None, api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
            opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    finally:
        os.close(descriptor)
    assert blocked == {"code":"lock_not_acquired","G_LIVE_ALLOWED":"NO",
                       "provider_starts":0,"credential_reads":0}
    assert not root.exists()


def test_home_mismatch_stops_before_derived_path_use(tmp_path, monkeypatch):
    module = _live_tool()
    account, alternate = tmp_path / "account", tmp_path / "alternate"
    account.mkdir(); alternate.mkdir()
    uid = os.getuid()
    monkeypatch.setattr(module.pwd, "getpwuid", lambda _: type("P", (), {"pw_dir":str(account)})())
    monkeypatch.setattr(module.os, "getuid", lambda: uid)
    monkeypatch.setenv("HOME", str(alternate))
    with pytest.raises(ValueError, match="home_mismatch"):
        module.canonical_paths()
    assert list(account.iterdir()) == [] and list(alternate.iterdir()) == []


def test_production_main_binds_execute_r2_to_exact_derived_dependencies(tmp_path, monkeypatch):
    module = _live_tool()
    root, credential, lock = tmp_path / "root", tmp_path / "credential", tmp_path / "lock"
    wall, mono, sleeper, opener, loader = object(), object(), object(), object(), object()
    observed = {}
    monkeypatch.setattr(module, "canonical_paths", lambda: (root, credential, lock))
    monkeypatch.setattr(module, "execute_r2", lambda **kwargs: observed.update(kwargs) or {
        "code":"already_complete","provider_starts":0,"credential_reads":0})
    monkeypatch.setattr(module.sys, "argv", ["fred_canonical_live.py", "execute-r2"])
    assert module.run_cli(["execute-r2"], execution_allowed=True, wall_clock=wall,
        monotonic_clock=mono, sleeper=sleeper, opener_builder=opener,
        api_key_loader=loader) == 0
    assert observed == {"root":root,"credential_path":credential,"lock_path":lock,
        "execution_allowed":True,"wall_clock":wall,"monotonic_clock":mono,
        "sleeper":sleeper,"opener_builder":opener,"api_key_loader":loader}


def test_evidence_schema_is_exact_static_and_path_secret_free(tmp_path):
    module = _live_tool()
    store = FredMaterialStore(tmp_path / "private")
    store.ensure_live_campaign()
    evidence = module._evidence(store, [], 0, "ACTIVE", None)
    assert set(evidence) == {"schema","campaign_code","code_revision","manifest_code",
        "manifest_revision","manifest_digest","terminal_state","terminal_code","operations",
        "local_opener_start_count","counts","integrity","store_evidence_sha256","database_sha256"}
    rendered = fred_macro.canonical_json(evidence).decode()
    assert str(tmp_path) not in rendered and "api_key" not in rendered and "https://" not in rendered


def test_exact_complete_campaign_revalidation_does_not_quarantine(tmp_path):
    store = FredMaterialStore(tmp_path / "private")
    store.ensure_live_campaign()
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE campaign SET terminal_state='COMPLETE',terminal_code='manifest_complete'")
    store.ensure_live_campaign()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT terminal_state,terminal_code FROM campaign").fetchone() == (
            "COMPLETE", "manifest_complete")


def _live_bodies():
    return {
        "/fred/series": {"realtime_start":"2026-08-12","realtime_end":"2026-08-12",
            "seriess":[{"id":"PAYEMS","realtime_start":"2026-08-12","realtime_end":"2026-08-12",
            "title":"Employment","observation_start":"1939-01-01","observation_end":"2026-07-01",
            "frequency":"Monthly","frequency_short":"M","units":"Thousands","units_short":"Thous.",
            "seasonal_adjustment":"Seasonally Adjusted","seasonal_adjustment_short":"SA",
            "last_updated":"2026-08-01 00:00:00-05","popularity":90}]},
        "/fred/series/observations": {"realtime_start":"2026-08-12","realtime_end":"2026-08-12",
            "observation_start":"2026-07-01","observation_end":"2026-07-31","units":"lin",
            "output_type":4,"file_type":"json","order_by":"observation_date","sort_order":"asc",
            "count":1,"offset":0,"limit":10,"observations":[{"realtime_start":"2026-08-12",
            "realtime_end":"2026-08-12","date":"2026-07-01","value":"1"}]},
        "/fred/series/release": {"realtime_start":"2026-08-12","realtime_end":"2026-08-12",
            "releases":[{"id":50,"realtime_start":"2026-08-12","realtime_end":"2026-08-12",
            "name":"Employment Situation","press_release":True}]},
        "/fred/release/dates": {"realtime_start":"2026-08-12","realtime_end":"2026-08-12",
            "order_by":"release_date","sort_order":"desc","count":1,"offset":0,"limit":10,
            "release_dates":[{"release_id":50,"date":"2026-08-01"}]},
        "/fred/series/vintagedates": {"realtime_start":"1776-07-04","realtime_end":"9999-12-31",
            "order_by":"vintage_date","sort_order":"asc","count":1,"offset":0,"limit":10,
            "vintage_dates":["2026-08-01"]},
    }


class _Response:
    status = 200
    headers = {}
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return self._body


class _LiveOpener:
    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at
    def open(self, request, *, timeout):
        from urllib.parse import urlsplit, parse_qsl
        parsed = urlsplit(request.full_url)
        self.calls.append((parsed.path, tuple(parse_qsl(parsed.query))))
        if len(self.calls) == self.fail_at:
            raise OSError("masked provider failure")
        return _Response(json.dumps(_live_bodies()[parsed.path]).encode())


def _r2_dependencies(tmp_path, fail_at=None):
    module = _live_tool()
    root = tmp_path / "r2"
    root.mkdir(mode=0o700)
    database = root / "fred.sqlite3"
    database.touch(mode=0o600)
    with sqlite3.connect(database) as connection:
        connection.executescript(DDL_V3)
        connection.execute("INSERT INTO campaign VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, fred_macro.SOURCE, fred_macro.PURPOSE, fred_macro.CAMPAIGN_CODE,
            fred_macro.CODE_REVISION, fred_macro.MANIFEST_CODE, fred_macro.MANIFEST_REVISION,
            fred_macro.MANIFEST_DIGEST, "LIVE", "PAYEMS", 10, "QUARANTINED", "bad_request"))
        request = fred_macro.g_live_manifest(release_id=1)[0]
        connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            fred_macro.SOURCE, fred_macro.PURPOSE, "c" * 64, 1, request.operation.value,
            request.request_identity, "LIVE", "2026-08-12T00:00:00.000000Z", "FAILED",
            fred_macro.CAMPAIGN_CODE, fred_macro.MANIFEST_REVISION, fred_macro.MANIFEST_DIGEST))
    module.migrate_r2_schema(root, lock_path=tmp_path / "namespace" / "lock",
                             migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    credential = credentials / "fred.env"
    credential.write_text("metadata only\n")
    credential.chmod(0o600)
    opener, keys = _LiveOpener(fail_at), []
    base, tick = datetime(2026,8,12,tzinfo=timezone.utc), [0]
    def wall():
        tick[0] += 1
        return base + timedelta(seconds=tick[0] * 2)
    result = module.execute_r2(root=root, credential_path=credential,
        lock_path=tmp_path / "namespace" / "lock", execution_allowed=True,
        wall_clock=wall, monotonic_clock=lambda: float(tick[0]), sleeper=lambda _: None,
        api_key_loader=lambda path: keys.append(path) or "a" * 32,
        opener_builder=lambda: opener)
    return module, result, root, credential, opener, keys


def test_r2_five_success_campaign_local_ordinals_high_water_and_zero_start_replay(tmp_path):
    module, result, root, credential, opener, keys = _r2_dependencies(tmp_path)
    assert result["code"] == "durable_success"
    assert result["provider_starts"] == result["credential_reads"] == 5
    with sqlite3.connect(root / "fred.sqlite3") as connection:
        assert connection.execute("SELECT manifest_ordinal,a.ordinal FROM campaign_attempts c JOIN attempts a ON a.invocation_identity=c.invocation_identity WHERE c.campaign_code=? ORDER BY manifest_ordinal", (fred_macro.R2_CAMPAIGN_CODE,)).fetchall() == list(zip(range(1,6), range(2,7)))
    evidence = json.loads((root / "g-live-r2-evidence.json").read_bytes())
    assert evidence["aggregate_live_high_water"] == 6
    replay = module.execute_r2(root=root, credential_path=credential,
        lock_path=tmp_path / "namespace" / "lock", execution_allowed=True,
        wall_clock=lambda: datetime(2026,8,13,tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError("key")),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError("provider")))
    assert replay["code"] == "already_complete"
    assert replay["provider_starts"] == replay["credential_reads"] == 0


@pytest.mark.parametrize("position", range(1, 6))
def test_r2_each_failure_position_stops_and_never_writes_r1_evidence(position, tmp_path):
    _, result, root, _, opener, keys = _r2_dependencies(tmp_path, fail_at=position)
    assert result["code"] == "connection_failure"
    assert result["provider_starts"] == result["credential_reads"] == position
    assert len(opener.calls) == len(keys) == position
    assert not (root / "g-live-evidence.json").exists()


def _run_dependencies(tmp_path, fail_at=None, module=None, wall_clock=None):
    root, lock = tmp_path / "root", tmp_path / "namespace" / "lock"
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener, keys, sleeps = _LiveOpener(fail_at), [], []
    base, wall_index, mono_index = datetime(2026,8,12,tzinfo=timezone.utc), [0], [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    module = module or _live_tool()
    result = module.run_initial(root=root, credential_path=credential,
        lock_path=lock, execution_allowed=True, wall_clock=wall_clock or wall,
        monotonic_clock=mono, sleeper=sleeps.append,
        api_key_loader=lambda path: keys.append(path) or "a" * 32,
        opener_builder=lambda: opener)
    return result, root, credential, opener, keys, sleeps


def test_production_initial_five_operation_success_and_exact_replay(tmp_path):
    result, root, credential, opener, keys, sleeps = _run_dependencies(tmp_path)
    assert result == {"code":"durable_success","G_LIVE_ALLOWED":"NO",
        "provider_starts":5,"credential_reads":5}
    assert [path for path, _ in opener.calls] == list(_live_bodies())
    assert all(pairs[-1] == ("api_key", "a" * 32) for _, pairs in opener.calls)
    assert keys == [credential] * 5
    evidence = json.loads((root / "g-live-evidence.json").read_bytes())
    assert evidence["local_opener_start_count"] == evidence["counts"]["attempts"] == 5
    assert [item["attempt_state"] for item in evidence["operations"]] == ["SUCCEEDED"] * 5
    assert evidence["integrity"] == {"schema_fingerprint_ok":True,"campaign_match":True,
        "store_evidence_ok":True,"controller_evidence_ok":True}
    before = FredMaterialStore(root).counts()
    replay_opener, replay_keys, replay_sleeps = _LiveOpener(), [], []
    module = _live_tool()
    replay = module.run_initial(root=root, credential_path=credential,
        lock_path=root.parent / "namespace" / "lock", execution_allowed=True,
        wall_clock=lambda: datetime(2026,8,13,tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=replay_sleeps.append,
        api_key_loader=lambda path: replay_keys.append(path), opener_builder=lambda: replay_opener)
    assert replay["code"] == "already_complete"
    assert replay["provider_starts"] == replay["credential_reads"] == 0
    assert replay_opener.calls == replay_keys == replay_sleeps == []
    assert FredMaterialStore(root).counts() == before


def test_complete_replay_rejects_foreign_controller_evidence_without_a_start(tmp_path):
    _, root, credential, _, _, _ = _run_dependencies(tmp_path)
    path = root / "g-live-evidence.json"
    payload = json.loads(path.read_bytes())
    payload["manifest_digest"] = "f" * 64
    path.write_bytes(fred_macro.canonical_json(payload))
    path.chmod(0o600)
    opener, keys = _LiveOpener(), []
    result = _live_tool().run_initial(root=root, credential_path=credential,
        lock_path=root.parent / "namespace" / "lock", execution_allowed=True,
        wall_clock=lambda: datetime(2026,8,13,tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda value: keys.append(value), opener_builder=lambda: opener)
    assert result["code"] == "replay_invalid"
    assert result["provider_starts"] == result["credential_reads"] == 0
    assert opener.calls == keys == []
    assert FredMaterialStore(root).campaign_state() == ("QUARANTINED", "replay_invalid")


@pytest.mark.parametrize("position", range(1, 6))
def test_production_initial_first_failure_stops_truthfully(position, tmp_path):
    result, root, _, opener, keys, _ = _run_dependencies(tmp_path, fail_at=position)
    assert result["code"] == "connection_failure"
    assert result["provider_starts"] == result["credential_reads"] == position
    assert len(opener.calls) == len(keys) == position
    evidence = json.loads((root / "g-live-evidence.json").read_bytes())
    assert len(evidence["operations"]) == position
    assert evidence["operations"][-1]["attempt_state"] == "FAILED"
    assert evidence["counts"]["attempts"] == position


def test_clock_rollback_after_first_success_preserves_truthful_counts_and_evidence(tmp_path):
    base = datetime(2026,8,12,tzinfo=timezone.utc)
    values = iter((base, base + timedelta(seconds=2), base + timedelta(seconds=3),
                   base + timedelta(seconds=1), base + timedelta(seconds=1)))
    result, root, _, opener, keys, _ = _run_dependencies(
        tmp_path, wall_clock=lambda: next(values))
    assert result["code"] == "clock_failure"
    assert result["provider_starts"] == result["credential_reads"] == 1
    assert len(opener.calls) == len(keys) == 1
    evidence = json.loads((root / "g-live-evidence.json").read_bytes())
    assert evidence["terminal_code"] == "clock_failure"
    assert evidence["counts"]["succeeded"] == 1


def test_controller_evidence_write_failure_never_resets_live_counts(tmp_path, monkeypatch):
    module = _live_tool()
    monkeypatch.setattr(module, "write_atomic_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("masked")))
    result, root, _, opener, keys, _ = _run_dependencies(
        tmp_path, fail_at=1, module=module)
    assert result["code"] == "evidence_write_failure"
    assert result["provider_starts"] == result["credential_reads"] == 1
    assert len(opener.calls) == len(keys) == 1
    assert not (root / "g-live-evidence.json").exists()


def test_postcommit_store_evidence_failure_is_succeeded_and_exactly_quarantined(
        tmp_path, monkeypatch):
    import tokmo.records.fred_material_store as material_store
    monkeypatch.setattr(material_store, "write_atomic_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("masked")))
    result, root, _, _, _, _ = _run_dependencies(tmp_path)
    assert result["code"] == "durable_success_evidence_failure"
    assert result["provider_starts"] == result["credential_reads"] == 1
    evidence = json.loads((root / "g-live-evidence.json").read_bytes())
    assert evidence["operations"][-1]["attempt_state"] == "SUCCEEDED"
    assert evidence["terminal_code"] == "durable_success_evidence_failure"
    assert FredMaterialStore(root).campaign_state() == (
        "QUARANTINED", "durable_success_evidence_failure")

# --- M7: Missing executable tests ---

def _v3_db_with_r1(root):
    database = root / "fred.sqlite3"
    database.touch(mode=0o600)
    with sqlite3.connect(database) as connection:
        connection.executescript(DDL_V3)
        connection.execute("INSERT INTO campaign VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, fred_macro.SOURCE, fred_macro.PURPOSE, fred_macro.CAMPAIGN_CODE,
            fred_macro.CODE_REVISION, fred_macro.MANIFEST_CODE, fred_macro.MANIFEST_REVISION,
            fred_macro.MANIFEST_DIGEST, "LIVE", "PAYEMS", 10, "QUARANTINED", "bad_request"))
        request = fred_macro.g_live_manifest(release_id=1)[0]
        connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            fred_macro.SOURCE, fred_macro.PURPOSE, "a" * 64, 1, request.operation.value,
            request.request_identity, "LIVE", "2026-08-12T00:00:00.000000Z", "FAILED",
            fred_macro.CAMPAIGN_CODE, fred_macro.MANIFEST_REVISION, fred_macro.MANIFEST_DIGEST))
    return database

def _migrated_r2(tmp_path, module):
    root = tmp_path / "r2"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    module.migrate_r2_schema(root, lock_path=tmp_path / "lock",
                             migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    return root

def test_fresh_v4_fingerprint_matches_migrated_v3_to_v4(tmp_path):
    fresh = FredMaterialStore(tmp_path / "fresh")
    fresh_fp = FredMaterialStore.inspect_existing(fresh.root).fingerprint
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    migrated_fp = FredMaterialStore.inspect_existing(root).fingerprint
    assert fresh_fp == migrated_fp

def test_old_v3_code_failclosed_against_v4_with_zero_writes(tmp_path, monkeypatch):
    """Bind the store to the V3 schema contract and prove a V4 DB is rejected before write."""
    import tokmo.records.fred_material_store as store_module
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(DDL_V3)
        v3_semantics = _schema_semantics(connection)
    monkeypatch.setattr(store_module, "EXPECTED_SCHEMA", v3_semantics)
    with pytest.raises(ValueError, match="schema_fingerprint_mismatch"):
        FredMaterialStore(root)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    assert before == after

def test_receipt_and_evidence_exist_before_any_reservation(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    receipt = json.loads((root / "g-live-r2-migration-receipt.json").read_bytes())
    evidence = json.loads((root / "g-live-r2-evidence.json").read_bytes())
    assert receipt["aggregate_live_high_water"] == 1
    assert evidence["aggregate_live_high_water"] == 1
    assert "schema_fingerprint_after" in receipt
    assert "pre_database_sha256" in receipt
    assert "post_database_sha256" in receipt
    assert "migrated_at" in receipt
    assert "r1_projection_sha256" in receipt
    assert "r1_evidence_sha256" in receipt
    assert evidence["migration_receipt_sha256"]
    assert "latest_post_database_sha256" in evidence
    assert "backup_sha256" in evidence

def test_restart_reconciles_reserved_to_unknown(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    request = fred_macro.g_live_manifest(release_id=1)[0]
    reservation = store.reserve_campaign(fred_macro.R2_CAMPAIGN_CODE, 1, request,
        started_at=datetime(2026,8,12,tzinfo=timezone.utc))
    assert reservation.code == "reserved"
    credential = tmp_path / "cred"
    credential.write_text("metadata\n")
    credential.chmod(0o600)
    result = module.execute_r2(root=root, credential_path=credential,
        lock_path=tmp_path / "lock", execution_allowed=True,
        wall_clock=lambda: datetime(2026,8,12,0,0,1,tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError("no io")),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError("no io")))
    assert result["code"] == "stale_reservation"
    assert result["provider_starts"] == 0
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        state = conn.execute("SELECT state FROM attempts WHERE invocation_identity=?",
                             (reservation.invocation_identity,)).fetchone()
        assert state[0] == "UNKNOWN"

def test_highwater_refund_is_blocked(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    ev_path = root / "g-live-r2-evidence.json"
    ev = json.loads(ev_path.read_bytes())
    ev["aggregate_live_high_water"] = 5
    ev_path.write_bytes(fred_macro.canonical_json(ev))
    ev_path.chmod(0o600)
    with pytest.raises(ValueError, match="aggregate_high_water_regression"):
        module.reconcile_r2_high_water(root)

def test_execute_r2_denied_by_default_allowed_with_flag(tmp_path, capsys):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    credential = tmp_path / "cred"
    credential.write_text("metadata\n")
    credential.chmod(0o600)
    lock = tmp_path / "lock"
    denied = module.run_cli(["execute-r2"], root=root, credential_path=credential,
        lock_path=lock, wall_clock=lambda: datetime(2026,8,12,tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    assert denied == 1
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "g_live_forbidden"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.run_cli(["execute-r2", "--g-live-r2-execute"], root=root,
            credential_path=credential, lock_path=lock,
            wall_clock=lambda: datetime(2026,8,12,tzinfo=timezone.utc),
            monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
            api_key_loader=lambda _: "a" * 32,
            opener_builder=lambda: type("O", (), {
                "open": staticmethod(lambda req, timeout=10: (_ for _ in ()).throw(OSError("blocked")))
            })())
    out2 = json.loads(buf.getvalue())
    assert out2["code"] == "connection_failure"

def test_provenance_constants_asserted_against_contract():
    assert fred_macro.R2_CAMPAIGN_CODE == "FRED_G_LIVE_PAYEMS_R2"
    assert fred_macro.R2_MANIFEST_CODE == "FRED_G_LIVE_PAYEMS_FIVE_R2"
    assert fred_macro.R2_MANIFEST_REVISION == "fred-canonical-stage3-g-live-r2-diagnostic-v1"
    assert fred_macro.R2_CODE_REVISION == "fred-g-live-controller-r2-diagnostic-v1"
    assert fred_macro.R2_MANIFEST_DIGEST == "fa0d82ac80678e74c02e2b05b642ce225a756f383727b814664ff134622fd134"
    assert OFFICIAL_AUDIT_PROVENANCE == {
        "series": ("https://fred.stlouisfed.org/docs/api/fred/series.html", "b293a17e3c76200c69b276e2f0e8edcec05064299ba01b56b4af2b25d5fdcc53"),
        "series_observations": ("https://fred.stlouisfed.org/docs/api/fred/series_observations.html", "2367ce88cb0f34f18e74181e1bcd78d884f3408e68d338ccc3f23af5b88833f6"),
        "series_release": ("https://fred.stlouisfed.org/docs/api/fred/series_release.html", "615616b36011d255588c78ebda0858734c19767ec94f584413d3a8420ecff24d"),
        "release_dates": ("https://fred.stlouisfed.org/docs/api/fred/release_dates.html", "17cfda100487e0dfb269af0cf75a63423fe6a612d6bce2e5b7f6e58fac0cd4ec"),
        "series_vintagedates": ("https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html", "8454837098b7774552a6ec0de02bedb8b2e552e0a6e296da11b9c00255b43c30"),
        "errors": ("https://fred.stlouisfed.org/docs/api/fred/errors.html", "cb5d9b8eef61ca6a27cc33b1a13ac9413a071db88373a771171cd727d90d0fd3"),
    }

def test_http_400_read_error_and_encoding_paths():
    opener = _Http400Opener(b'{"error_code":400,"error_message":"x"}', read_error=True)
    outcome = FredLiveTransport(opener=opener)(fred_macro.g_live_manifest(release_id=1)[0], "a" * 32)
    assert outcome.code == "provider_bad_request_unknown"
    assert (opener.error.reads, opener.error.closes) == (1, 1)
    opener2 = _Http400Opener(b'{"error_code":400,"error_message":"x"}', encoding="gzip")
    outcome2 = FredLiveTransport(opener=opener2)(fred_macro.g_live_manifest(release_id=1)[0], "a" * 32)
    assert outcome2.code == "provider_bad_request_unknown"

def test_r1_evidence_hash_preserved_across_migration(tmp_path):
    import hashlib
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    r1_ev = root / "g-live-evidence.json"
    r1_ev_data = fred_macro.canonical_json({"schema":"tokmo.fred.g-live-evidence.r1","test":True})
    r1_ev.write_bytes(r1_ev_data)
    r1_ev.chmod(0o600)
    pre_hash = hashlib.sha256(r1_ev.read_bytes()).hexdigest()
    receipt = module.migrate_r2_schema(root, lock_path=tmp_path / "lock",
                                       migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    assert receipt["r1_evidence_sha256"] == pre_hash
    post_hash = hashlib.sha256(r1_ev.read_bytes()).hexdigest()
    assert pre_hash == post_hash
    evidence = json.loads((root / "g-live-r2-evidence.json").read_bytes())
    assert evidence["r1_evidence_sha256"] == pre_hash

def test_migration_lock_exclusion(tmp_path):
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    lock = tmp_path / "lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ValueError, match="lock_not_acquired"):
            module.migrate_r2_schema(root, lock_path=lock,
                                     migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    finally:
        os.close(fd)
    module.migrate_r2_schema(root, lock_path=lock,
                             migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))

def test_migration_hot_sidecar_zero_write(tmp_path):
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    sidecar = root / "fred.sqlite3-wal"
    sidecar.write_bytes(b"hot journal")
    sidecar.chmod(0o600)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    lock = tmp_path / "lock"
    with pytest.raises(ValueError, match="sqlite_sidecar_present"):
        module.migrate_r2_schema(root, lock_path=lock,
                                 migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before

def test_cli_error_actions_emit_static_json_no_path(tmp_path, capsys):
    module = _live_tool()
    root = tmp_path / "nonexistent"
    lock = tmp_path / "lock"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.run_cli(["readback-r2"], root=root, lock_path=lock)
    output = buf.getvalue().strip()
    assert json.loads(output)
    assert str(tmp_path) not in output
    assert "Traceback" not in output

def test_cli_initial_action_preserved(tmp_path, capsys):
    module = _live_tool()
    root, credential, lock = tmp_path / "root", tmp_path / "cred", tmp_path / "lock"
    result = module.run_cli(["initial"], root=root, credential_path=credential,
        lock_path=lock, execution_allowed=False,
        wall_clock=lambda: None, monotonic_clock=lambda: None,
        sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["code"] == "g_live_forbidden"

def test_standalone_preflight_does_not_write_evidence(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in root.iterdir()}
    module.preflight_r2(root, lock_path=tmp_path / "lock")
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in root.iterdir()}
    assert before == after

def test_r2_campaign_assert_mode_allowed(tmp_path):
    store = FredMaterialStore(tmp_path / "v4")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    store.assert_mode_allowed(fred_macro.ExecutionMode.LIVE, campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    conn = store._connect()
    try:
        row = store._campaign(conn, fred_macro.R2_CAMPAIGN_CODE)
        assert row is not None and row[0] == fred_macro.R2_CAMPAIGN_CODE
    finally:
        conn.close()


# === M-A: In-lock sidecar re-verification ===

def test_migration_inlock_sidecar_zero_write(tmp_path, monkeypatch):
    """Inject sidecar in the lock-acquisition window; prove zero backup/write."""
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    lock = tmp_path / "lock"
    # Hook canonical_lock to inject a sidecar after lock is acquired but before _migrate_r2_locked
    original_lock = module.canonical_lock
    @contextlib.contextmanager
    def injected_lock(lock_path):
        with original_lock(lock_path):
            sidecar = root / "fred.sqlite3-wal"
            sidecar.write_bytes(b"hot journal injected in lock window")
            sidecar.chmod(0o600)
            yield
    monkeypatch.setattr(module, "canonical_lock", injected_lock)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ValueError, match="sqlite_sidecar_present"):
        module.migrate_r2_schema(root, lock_path=lock,
                                 migrated_at=datetime(2026,8,12,tzinfo=timezone.utc))
    # No backup file created
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.name != "fred.sqlite3-wal"} == \
        {k: v for k, v in before.items()}


def test_rollback_inlock_sidecar_zero_write(tmp_path, monkeypatch):
    """Inject sidecar in rollback lock window; prove zero restore/write."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    lock = tmp_path / "lock"
    original_lock = module.canonical_lock
    @contextlib.contextmanager
    def injected_lock(lock_path):
        with original_lock(lock_path):
            sidecar = root / "fred.sqlite3-wal"
            sidecar.write_bytes(b"hot journal injected in rollback")
            sidecar.chmod(0o600)
            yield
    monkeypatch.setattr(module, "canonical_lock", injected_lock)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ValueError, match="sqlite_sidecar_present"):
        module.rollback_r2_schema(root, lock_path=lock)
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.name != "fred.sqlite3-wal"} == \
        {k: v for k, v in before.items()}


# === M-B: R1 evidence hash mismatch detection ===

def test_r1_evidence_mutation_detected_by_reconcile(tmp_path):
    """Mutate g-live-evidence.json mid-run; reconcile must raise r1_evidence_hash_mismatch."""
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    # Create R1 evidence before migration so its hash is pinned in the receipt.
    r1_ev = root / "g-live-evidence.json"
    r1_ev_data = fred_macro.canonical_json({"schema": "tokmo.fred.g-live-evidence.r1", "test": True})
    r1_ev.write_bytes(r1_ev_data)
    r1_ev.chmod(0o600)
    module.migrate_r2_schema(root, lock_path=tmp_path / "lock",
                             migrated_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    receipt = json.loads((root / "g-live-r2-migration-receipt.json").read_bytes())
    pinned_hash = receipt["r1_evidence_sha256"]
    assert pinned_hash is not None
    # Mutate R1 evidence
    mutated = fred_macro.canonical_json({"schema": "tokmo.fred.g-live-evidence.r1", "mutated": True})
    r1_ev.write_bytes(mutated)
    r1_ev.chmod(0o600)
    with pytest.raises(ValueError, match="r1_evidence_hash_mismatch"):
        module.reconcile_r2_high_water(root)


def test_r1_evidence_mutation_detected_by_preflight(tmp_path):
    """Mutate g-live-evidence.json; preflight must raise r1_evidence_hash_mismatch."""
    module = _live_tool()
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    r1_ev = root / "g-live-evidence.json"
    r1_ev_data = fred_macro.canonical_json({"schema": "tokmo.fred.g-live-evidence.r1", "test": True})
    r1_ev.write_bytes(r1_ev_data)
    r1_ev.chmod(0o600)
    module.migrate_r2_schema(root, lock_path=tmp_path / "lock",
                             migrated_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    # Mutate R1 evidence
    mutated = fred_macro.canonical_json({"schema": "tokmo.fred.g-live-evidence.r1", "mutated": True})
    r1_ev.write_bytes(mutated)
    r1_ev.chmod(0o600)
    with pytest.raises(ValueError, match="r1_evidence_hash_mismatch"):
        module.preflight_r2(root, lock_path=tmp_path / "lock")


# === M-C: FIXTURE negative for R2 campaign ===

def test_r2_campaign_fixture_mode_forbidden(tmp_path):
    """assert_mode_allowed with FIXTURE + R2 campaign must raise fixture_live_campaign_forbidden."""
    store = FredMaterialStore(tmp_path / "v4")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    with pytest.raises(ValueError, match="fixture_live_campaign_forbidden"):
        store.assert_mode_allowed(fred_macro.ExecutionMode.FIXTURE,
                                  campaign_code=fred_macro.R2_CAMPAIGN_CODE)


def test_r2_execute_calls_assert_mode_allowed_through_production(tmp_path, monkeypatch):
    """Production execute_r2 must call assert_mode_allowed(LIVE, R2_CAMPAIGN_CODE)."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    credential = tmp_path / "cred"
    credential.write_text("metadata\n")
    credential.chmod(0o600)
    called = []
    original_assert = FredMaterialStore.assert_mode_allowed
    def spy_assert(self, mode, campaign_code=None):
        called.append((mode, campaign_code))
        return original_assert(self, mode, campaign_code)
    monkeypatch.setattr(FredMaterialStore, "assert_mode_allowed", spy_assert)
    module.execute_r2(root=root, credential_path=credential,
        lock_path=tmp_path / "lock", execution_allowed=True,
        wall_clock=lambda: datetime(2026,8,12,0,0,1,tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda _: "a" * 32,
        opener_builder=lambda: _LiveOpener())
    assert any(mode is fred_macro.ExecutionMode.LIVE and code == fred_macro.R2_CAMPAIGN_CODE
               for mode, code in called)


# === M-D: All CLI actions emit static JSON ===

def test_cli_all_actions_emit_static_json_no_path(tmp_path, capsys):
    """Every CLI action including initial and execute-r2 must emit static JSON."""
    module = _live_tool()
    lock = tmp_path / "lock"
    for action, extra_kwargs in [
        ("readback-r2", {}),
        ("preflight-r2", {}),
        ("initial", {"execution_allowed": False,
                      "wall_clock": lambda: None,
                      "monotonic_clock": lambda: None,
                      "sleeper": lambda _: None,
                      "api_key_loader": lambda _: (_ for _ in ()).throw(AssertionError()),
                      "opener_builder": lambda: (_ for _ in ()).throw(AssertionError())}),
        ("execute-r2", {"wall_clock": lambda: datetime(2026,8,12,tzinfo=timezone.utc),
                         "monotonic_clock": lambda: 1.0,
                         "sleeper": lambda _: None,
                         "api_key_loader": lambda _: "a" * 32,
                         "opener_builder": lambda: (_ for _ in ()).throw(OSError("blocked path /secret"))}),
    ]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            module.run_cli([action], root=tmp_path / "nonexistent",
                          credential_path=tmp_path / "cred", lock_path=lock, **extra_kwargs)
        output = buf.getvalue().strip()
        assert json.loads(output), f"{action}: not valid JSON"
        assert str(tmp_path) not in output, f"{action}: leaks tmp_path"
        assert "Traceback" not in output, f"{action}: leaks traceback"
        assert "/secret" not in output, f"{action}: leaks secret path"


# === M-E: Standalone preflight lock exclusion and read-only ===

def test_standalone_preflight_lock_exclusion(tmp_path):
    """Standalone preflight must acquire canonical lock and exclude concurrent holders."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    lock = tmp_path / "lock"
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ValueError, match="lock_not_acquired"):
            module.preflight_r2(root, lock_path=lock)
    finally:
        os.close(fd)
    # After lock released, preflight succeeds
    module.preflight_r2(root, lock_path=lock)


def test_standalone_preflight_absent_root_zero_create(tmp_path):
    """Standalone preflight against absent root must not create anything."""
    module = _live_tool()
    absent = tmp_path / "absent"
    with pytest.raises((ValueError, OSError)):
        module.preflight_r2(absent, lock_path=tmp_path / "lock")
    assert not absent.exists()


# === M-G: Credential failure writes QUARANTINED evidence ===

def test_r2_credential_failure_writes_quarantined_evidence(tmp_path):
    """R2 credential failure must result in credential_failure code and QUARANTINED evidence."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    credential = tmp_path / "cred"
    credential.write_text("metadata\n")
    credential.chmod(0o600)
    # Use an opener that fails to force credential_failure path
    # credential_failure happens when api_key_provider raises
    result = module.execute_r2(root=root, credential_path=credential,
        lock_path=tmp_path / "lock", execution_allowed=True,
        wall_clock=lambda: datetime(2026,8,12,0,0,1,tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(OSError("credential read failure")),
        # Opener construction is local-only; any .open call would be a forbidden provider start.
        opener_builder=lambda: type("NoProviderOpener", (), {
            "open": staticmethod(lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("provider must not start")))
        })())
    assert result["code"] == "credential_failure"
    assert result["provider_starts"] == 0
    # QUARANTINED evidence must be on disk
    evidence = json.loads((root / "g-live-r2-evidence.json").read_bytes())
    assert evidence.get("terminal_state") == "QUARANTINED"
    assert evidence.get("terminal_code") == "credential_failure"


# === M-F: Missing mandated tests ===

def test_legacy_singleton_update_prohibition(tmp_path):
    """R2 path must never UPDATE the legacy singleton campaign table."""
    from tokmo.records.fred_material_store import FredMaterialStore
    store = FredMaterialStore(tmp_path / "v4")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    # The legacy campaign table should be empty (R2 uses campaigns, not campaign)
    with sqlite3.connect(store.path) as conn:
        legacy_count = conn.execute("SELECT count(*) FROM campaign").fetchone()[0]
    assert legacy_count == 0


def test_r2_composition_cannot_update_legacy_campaign():
    import ast
    source = (Path(__file__).parents[1] / "src/tokmo/fred_composition.py").read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    quarantine_calls = [node for node in calls
                        if isinstance(node.func, ast.Attribute)
                        and node.func.attr == "quarantine"]
    # R1/FIXTURE legacy branch + no-row fallback = 2 quarantine calls
    assert len(quarantine_calls) == 2
    for call in quarantine_calls:
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert call.func.value.id == "store"
    # Both legacy quarantine calls are in the dispatcher.
    dispatcher = next(node for node in tree.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.name == "_quarantine_for_reservation")
    for call in quarantine_calls:
        assert call in list(ast.walk(dispatcher))


def test_r2_aggregate_cap_concurrency_exactly_one_winner(tmp_path):
    """Two concurrent campaign reservations at cap-1 must produce exactly one winner."""
    store = FredMaterialStore(tmp_path / "race")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as conn:
        for ordinal in range(1, 20):  # 19 attempts; 19+1=20 → exactly one winner
            conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "FRED", "CANONICAL_SOURCE_STAGE3", f"{ordinal:064x}", ordinal,
                "SERIES", f"{ordinal + 100:064x}", "LIVE", "2026-01-01T00:00:0" + str(ordinal % 10) + ".000000Z",
                "FAILED", fred_macro.CAMPAIGN_CODE, "legacy", "0" * 64))
        conn.commit()
    import threading
    barrier = threading.Barrier(2)
    results = []
    def reserve(ordinal):
        barrier.wait(timeout=5)
        req = fred_macro.g_live_manifest(release_id=1)[0]
        return store.reserve_campaign(fred_macro.R2_CAMPAIGN_CODE, ordinal, req,
                                       started_at=datetime(2026,8,12,tzinfo=timezone.utc)).code
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(reserve, (1, 1)))
    assert sorted(codes) == ["cap_exhausted", "reserved"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
            (fred_macro.R2_CAMPAIGN_CODE,)).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM attempts WHERE source='FRED' AND purpose='CANONICAL_SOURCE_STAGE3' AND execution_mode='LIVE'"
        ).fetchone() == (20,)


def test_r2_reconciliation_isolation_from_legacy(tmp_path):
    store = FredMaterialStore(tmp_path / "v4")
    store.ensure_live_campaign()
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    request = fred_macro.g_live_manifest(release_id=1)[0]
    reservation = store.reserve_campaign(
        fred_macro.R2_CAMPAIGN_CODE, 1, request,
        started_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    assert reservation.code == "reserved"
    with sqlite3.connect(store.path) as connection:
        legacy_before = connection.execute("SELECT * FROM campaign WHERE singleton=1").fetchone()
    assert store.reconcile_campaign_unknown(
        fred_macro.R2_CAMPAIGN_CODE,
        before=datetime(2026, 8, 12, 0, 0, 1, tzinfo=timezone.utc)) == 1
    with sqlite3.connect(store.path) as connection:
        legacy_after = connection.execute("SELECT * FROM campaign WHERE singleton=1").fetchone()
        r2_state = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (fred_macro.R2_CAMPAIGN_CODE,)).fetchone()
        attempt_state = connection.execute(
            "SELECT state FROM attempts WHERE invocation_identity=?",
            (reservation.invocation_identity,)).fetchone()
    assert legacy_after == legacy_before
    assert r2_state == ("QUARANTINED", "stale_reservation")
    assert attempt_state == ("UNKNOWN",)


def test_official_request_key_allowlist_for_all_five_operations():
    requests = fred_macro.g_live_manifest(release_id=1)
    expected = (
        {"series_id", "realtime_start", "realtime_end", "file_type"},
        {"series_id", "realtime_start", "realtime_end", "observation_start",
         "observation_end", "units", "output_type", "sort_order", "limit", "offset", "file_type"},
        {"series_id", "realtime_start", "realtime_end", "file_type"},
        {"release_id", "include_release_dates_with_no_data", "sort_order", "limit", "offset", "file_type"},
        {"series_id", "sort_order", "limit", "offset", "file_type"},
    )
    for request, allowed in zip(requests, expected, strict=True):
        keys = [key for key, _ in build_query_pairs(request)]
        assert len(keys) == len(set(keys))
        assert set(keys) == allowed
        assert "order_by" not in keys


def test_fred_response_missing_order_by_fails_closed():
    """parse_fred_response with missing order_by must raise, not pass."""
    from tokmo.facts.fred import parse_fred_response
    request = fred_macro.g_live_manifest(release_id=1)[3]  # RELEASE_DATES
    body = json.dumps({"realtime_start":"2026-08-12","realtime_end":"2026-08-12",
                       "sort_order":"desc","count":1,"offset":0,"limit":10,
                       "release_dates":[{"release_id":1,"date":"2026-08-01"}]}).encode()
    with pytest.raises(ValueError, match="closed_keys_mismatch"):
        parse_fred_response(request, body)


def test_r2_credential_path_metadata_one_read_ordering(tmp_path):
    """Credential path is checked once via metadata preflight before any read."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    credential = tmp_path / "cred"
    credential.write_text("metadata\n")
    credential.chmod(0o600)
    read_count = []
    original_credential_preflight = module._credential_preflight
    def counting_preflight(path):
        read_count.append(path)
        return original_credential_preflight(path)
    module._credential_preflight = counting_preflight
    try:
        module.preflight_r2(root, lock_path=tmp_path / "lock", credential_path=credential)
    finally:
        del module._credential_preflight
    # Credential preflight called exactly once
    assert len(read_count) == 1
    assert read_count[0] == credential
