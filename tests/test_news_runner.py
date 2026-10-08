"""One-shot NEWS runner must reuse the production manager and ledger path."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from tokmo.facts.google_news_live import GoogleNewsConnector, GoogleNewsQuery
from tokmo.values.external_news import NewsReceipt, SaveOutcome
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus


RUNNER = Path(__file__).parents[1] / "tools" / "run_news_connector.py"


def _runner_module():
    spec = importlib.util.spec_from_file_location("run_news_connector_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_shot_runner_reuses_manager_ledger_and_provider_specific_credentials():
    source = RUNNER.read_text()

    assert "NewsCollectionManager" in source
    assert "FetchAttemptLedger" in source
    assert "NewsCollectionJob" in source
    assert "_reserve_start" not in source
    assert "class NewsCredentials" not in source
    assert "from tokmo.facts.news_live import NewsCredentials" not in source
    assert "NaverNewsCredentials" in source
    assert "FinnhubNewsCredentials" in source
    for source_name in ("yna-market", "yna-industry", "jtbc-breaking",
                        "google-news-search", "investing-analyst"):
        assert source_name in source


def test_google_connector_remains_importable_and_one_shot_cli_addressable():
    assert GoogleNewsConnector is not None
    assert GoogleNewsQuery is not None
    source = RUNNER.read_text()
    assert '"google-news-search"' in source
    assert "GoogleNewsConnector(http)" in source


def test_connector_main_reports_revision_separately_from_duplicate(
        tmp_path, monkeypatch, capsys):
    module = _runner_module()
    db = tmp_path / "external.sqlite3"
    db.touch()

    class Ledger:
        def __init__(self, path): self.path = path
        def bootstrap(self, *, now): pass

    result = NewsFetchResult(NewsFetchStatus.EMPTY, (), bytes_received=17)
    outcome = SimpleNamespace(result=result)
    manager = SimpleNamespace(
        collect=lambda *args, **kwargs: SimpleNamespace(results=(outcome,)))
    monkeypatch.setattr(module, "FetchAttemptLedger", Ledger)
    monkeypatch.setattr(module, "NewsHttpTransport", lambda **kwargs: object())
    monkeypatch.setattr(module, "BbcBusinessConnector", lambda http: object())
    monkeypatch.setattr(module, "NewsCollectionManager", lambda **kwargs: manager)
    monkeypatch.setattr(module, "persist_news_result", lambda *args, **kwargs: (
        NewsReceipt(SaveOutcome.DUPLICATE, "doc-1", "req-1", "receipt-1"),
        NewsReceipt(SaveOutcome.DUPLICATE, "doc-1", "req-2", "receipt-2",
                    revision_evidence_id="a" * 64),
    ))

    assert module.main([
        "--source", "bbc", "--db", str(db),
        "--ledger", str(tmp_path / "ledger.sqlite3"),
        "--campaign-id", "campaign", "--ceiling", "1",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["revision"] == 1
    assert payload["duplicate"] == 1


@pytest.mark.parametrize("source", _runner_module()._parser()._option_string_actions["--source"].choices)
def test_every_connector_reports_durable_partial_storage_failure(tmp_path, monkeypatch, capsys, source):
    from datetime import datetime, timezone
    import sqlite3
    from tokmo.records.external_news import ExternalNewsStore
    from tokmo.programs.external_news_storage import bootstrap_external_news
    from tokmo.values.news_connectors import NewsConnectorItem
    module = _runner_module()
    db = tmp_path / "news.sqlite3"
    now = datetime.now(timezone.utc)
    bootstrap_external_news(db, now=now)
    items = tuple(NewsConnectorItem(canonical_url=f"https://example.com/{i}",
        title="fixture", source_article_id=str(i), observed_at=now) for i in range(2))
    result = NewsFetchResult(NewsFetchStatus.COLLECTED, items, 200, 123)
    monkeypatch.setattr(module.NewsCollectionManager, "collect", lambda *a, **kw:
        SimpleNamespace(results=(SimpleNamespace(result=result),)))
    monkeypatch.setattr(module, "_load_values", lambda path: dict(
        NAVER_NEWS_CLIENT_ID="fixture-id", NAVER_NEWS_CLIENT_SECRET="fixture-secret",
        FINNHUB_API_KEY="fixture-key"))
    save = ExternalNewsStore.save_connector_news
    def fail_second(self, spec, value, **kwargs):
        if value.source_article_id == "1":
            raise RuntimeError("token=SECRET https://host/?query=PRIVATE body=HIDDEN")
        return save(self, spec, value, **kwargs)
    monkeypatch.setattr(ExternalNewsStore, "save_connector_news", fail_second)
    assert module.main(["--source", source, "--db", str(db),
        "--ledger", str(tmp_path / "ledger.sqlite3"), "--campaign-id", "test-campaign",
        "--ceiling", "1", "--query", "fixture", "--symbol", "NVDA",
        "--from-date", "2026-09-28", "--to-date", "2026-09-29"]) == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "STORAGE_FAILURE"
    assert payload["inserted"] == 1
    diag = payload["diagnostics"]
    assert (diag["stage"], diag["cause"], diag["received"], diag["persisted"]) == (
        "PERSIST", "UNKNOWN", 2, 1)
    assert diag["elapsed_ms"] >= 0
    assert len(diag["trace"]) == 16
    assert '"persisted":1' in captured.err
    assert all(secret not in captured.out + captured.err for secret in (
        "SECRET", "PRIVATE", "HIDDEN", "https://", "token=", "Traceback"))
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM intake_receipts").fetchone() == (1,)


@pytest.mark.parametrize("source", _runner_module()._parser()._option_string_actions["--source"].choices)
@pytest.mark.parametrize("kind,cause", [("http", "HTTP_503"), ("timeout", "TIMEOUT"),
                                        ("connection", "CONNECTION_ERROR"), ("unknown", "UNKNOWN")])
def test_every_connector_keeps_transport_cause_without_wire_text(tmp_path, monkeypatch, capsys, source, kind, cause):
    from urllib.error import HTTPError, URLError
    module = _runner_module()
    errors = dict(http=HTTPError("https://host/?token=SECRET", 503, "BODY", {}, None),
                  timeout=URLError(TimeoutError("SECRET")), connection=URLError("SECRET"),
                  unknown=RuntimeError("SECRET"))
    class Opener:
        def open(self, *args, **kwargs):
            raise errors[kind]
    transport = module.NewsHttpTransport(allowed_hosts=module._ALLOWED_HOSTS, opener=Opener())
    monkeypatch.setattr(module, "NewsHttpTransport", lambda **kw: transport)
    monkeypatch.setattr(module, "_load_values", lambda path: dict(
        NAVER_NEWS_CLIENT_ID="fixture-id", NAVER_NEWS_CLIENT_SECRET="fixture-secret",
        FINNHUB_API_KEY="fixture-key"))
    assert module.main(["--source", source, "--db", str(tmp_path / "news.sqlite3"),
        "--bootstrap", "--ledger", str(tmp_path / "ledger.sqlite3"),
        "--campaign-id", "transport-fixture", "--ceiling", "1", "--query", "fixture",
        "--symbol", "NVDA", "--from-date", "2026-09-28", "--to-date", "2026-09-29"]) == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["diagnostics"]["cause"] == cause
    assert payload["diagnostics"]["stage"] == "FETCH"
    assert payload["http_status"] == (503 if kind == "http" else None)
    assert payload["inserted"] == 0
    assert "SECRET" not in captured.out + captured.err


def test_missing_database_has_safe_preparation_diagnostics(tmp_path, capsys):
    module = _runner_module()
    assert module.main(["--source", "bbc", "--db", str(tmp_path / "missing"),
        "--ledger", str(tmp_path / "ledger"), "--campaign-id", "fixture", "--ceiling", "1"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_UNAVAILABLE"
    assert payload["diagnostics"]["stage"] == "PREPARE"
    assert payload["diagnostics"]["cause"] == "FILE_MISSING"
