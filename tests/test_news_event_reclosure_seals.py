import ast
import json
from datetime import UTC
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src/tokmo"


def _imports(path):
    tree = ast.parse(path.read_text())
    return {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }


def test_public_alias_fixture_is_loaded_source_of_truth():
    assert json.loads(
        (SRC / "values/news_discovery_aliases.json").read_text()
    ) == json.loads((ROOT / "tests/fixtures/news_discovery_aliases.json").read_text())
    text = (SRC / "programs/news_topic_discovery.py").read_text()
    assert "load_public_aliases" in text and "_PUBLIC_ALIASES = {" not in text


def test_news_programs_have_no_sql_live_policy_or_order_surface():
    for name in (
        "news_topic_discovery.py",
        "news_investigation.py",
        "news_event_admission.py",
        "news_alert_delivery.py",
        "news_feedback.py",
        "news_production.py",
        "news_source_health.py",
        "news_universe.py",
    ):
        path = SRC / "programs" / name
        tree = ast.parse(path.read_text())
        imports = _imports(path)
        assert not any(
            token in item
            for item in imports
            for token in (
                "sqlite3",
                "telegram",
                "facts.",
                "policy_store",
                "preference",
                "order_writer",
            )
        ), path
        assert not any(
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and any(
                sql in n.value.upper()
                for sql in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
            )
            for n in ast.walk(tree)
        ), path


def test_classifier_public_payload_modules_cannot_import_private_types():
    for path in (
        SRC / "values/news_classifier.py",
        SRC / "programs/news_investigation.py",
        SRC / "programs/news_production.py",
    ):
        text = path.read_text().casefold()
        assert not any(
            token in text
            for token in (
                "currentholdings",
                "playbook",
                "account_scope",
                "pension",
                "watchlist",
            )
        )


def test_no_fixture_or_writer_shims_in_news_src():
    text = "\n".join(
        path.read_text()
        for folder in (SRC / "records", SRC / "programs")
        for path in folder.glob("news_*.py")
    )
    assert (
        "fixture" not in text.casefold()
        and "order_writer" not in text
        and "preference_mutations" not in text
    )


def test_alias_seal_is_ast_based_not_whitespace_match():
    tree = ast.parse((SRC / "programs/news_topic_discovery.py").read_text())
    assignments = (
        node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))
    )
    for node in assignments:
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        assert not (
            any(
                isinstance(target, ast.Name) and target.id == "_PUBLIC_ALIASES"
                for target in targets
            )
            and isinstance(node.value, ast.Dict)
        )


def test_deterministic_stages_never_invoke_classifier_call(tmp_path):
    from datetime import datetime

    from tokmo.programs.news_alert_delivery import run_alert_delivery
    from tokmo.programs.news_event_admission import (
        AdmissionInput,
        admit_news_event,
        run_news_event_admission,
    )
    from tokmo.programs.news_feedback import build_news_callback_handler
    from tokmo.programs.news_investigation import run_news_investigation
    from tokmo.programs.news_topic_discovery import discover_topics
    from tokmo.records.news_episode_store import NewsEpisodeStore
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.values.news_classifier import DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    from tokmo.values.news_discovery import NewsDocument
    from tokmo.values.ops_kernel import ProducerRole

    now = datetime(2026, 8, 21, tzinfo=UTC)
    calls = []
    spy = lambda request: (
        calls.append(request)
        or json.dumps(
            {
                "provider": request.provider,
                "model_id": request.model_id,
                "citations": ["ev"],
            }
        )
    )
    discover_topics(
        tuple(NewsDocument(str(i), "unknown burst", str(i), f"s{i}") for i in range(3)),
        holdings=set(),
    )
    admit_news_event(
        AdmissionInput("nep", 1, "ORDINARY", "SCALE_CHANGE", now, now, False, False)
    )
    assert calls == []
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    episode = store.upsert("classifier-boundary", (), now)
    ops = OpsKernelStore(
        tmp_path / "ops" / "kernel.sqlite", stable_phase_ids=frozenset()
    )
    revision_id = run_news_event_admission(
        store=store,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        draft={
            "episode_id": episode.episode_id,
            "revision_no": 1,
            "revision_kind": "INITIAL",
            "body_text": "body",
            "portfolio_context_digest": "b" * 64,
            "confidence": 70,
            "material_change_kind": "FIRST_ADMISSION",
            "material_change_observed_at": now,
            "links": ("https://example.com",),
        },
        activation_enabled=True,
        now=now,
    )
    assert calls == []
    delivered = run_alert_delivery(
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        telegram_send=lambda body, reply_markup=None: "42",
        keyboard_resolver=lambda alert_id: None,
        delivery_binder=store.bind_delivery_by_ops_alert,
        now=now,
    )
    assert delivered["sent"] == 1 and calls == []
    callback = build_news_callback_handler(store, ops.read_outbox, now=lambda: now)
    assert (
        callback({"data": "n1:u:" + revision_id[:32], "message_id": 42, "update_id": 1})
        == "RECORDED"
    )
    assert calls == []
    result = run_news_investigation(
        store=store,
        episode_id=episode.episode_id,
        purpose="EVENT_CLASS",
        evidence={"ev": "public"},
        prompt_policy_digest="a" * 64,
        model_contract_digest="b" * 64,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: "openai-codex"},
        classifier_call=spy,
        now=now,
    )
    assert result["status"] == "OK" and len(calls) == 1
    assert tuple(
        ops._connect().execute("SELECT count(*) FROM alert_outbox").fetchone()
    ) == (1,)
