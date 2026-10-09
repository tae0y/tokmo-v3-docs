import hashlib
import json
import os
from datetime import UTC, datetime

from tokmo.programs.execution_dialogue import consume_inbound
from tokmo.programs.news_event_admission import run_news_event_admission
from tokmo.programs.news_feedback import (
    build_news_callback_handler,
    run_news_feedback_reconcile,
)
from tokmo.programs.pulse_production_deps import build_pulse_production_deps
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.session_gate import Market

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _bound(tmp_path):
    news = NewsEpisodeStore(tmp_path / "news.sqlite")
    ops = OpsKernelStore(
        tmp_path / "ops" / "kernel.sqlite", stable_phase_ids=frozenset()
    )
    episode = news.upsert("feedback-flow", (), NOW)
    rid = run_news_event_admission(
        store=news,
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
            "material_change_observed_at": NOW,
            "links": ("https://example.com",),
        },
        activation_enabled=True,
        now=NOW,
    )
    claimed = ops.claim_pending_telegram(ProducerRole.HERMES, NOW)
    ops.mark_sent(ProducerRole.HERMES, claimed.alert_id, NOW, "42")
    news.bind_delivery(rid, "42", now=NOW)
    return news, ops, rid


def test_mixed_one_poll_routes_n1_without_cursor_theft(tmp_path):
    state = tmp_path / "state"
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "allocation.json").write_text('{"risk":"fixed"}', encoding="utf-8")
    before = hashlib.sha256((policy / "allocation.json").read_bytes()).hexdigest()
    news, ops, rid = _bound(state)
    dialogue = ExecutionDialogueStore(state / "dialogue.sqlite", clock=lambda: NOW)
    calls = []
    updates = (
        {
            "update_id": 1,
            "callback": True,
            "callback_query_id": "q",
            "message_id": 99,
            "data": "q1:none",
        },
        {
            "update_id": 2,
            "callback": True,
            "callback_query_id": "n",
            "message_id": 42,
            "data": "n1:u:" + rid[:32],
        },
        {
            "update_id": 3,
            "callback": False,
            "message_id": 100,
            "reply_to_message_id": None,
            "text": "hello",
        },
    )
    handler = build_news_callback_handler(news, ops.read_outbox, now=lambda: NOW)
    consume_inbound(
        dialogue_store=dialogue,
        updates_provider=lambda offset: calls.append(offset) or updates,
        playbook_root=policy,
        playbook_validator=lambda root: [],
        news_callback_handler=handler,
        activation_enabled=True,
        send_eligible_after=NOW,
        clock=lambda: NOW,
        now=NOW,
    )
    assert calls == [0] and dialogue.read_inbound_cursor() == 4
    assert news.connection.execute(
        "SELECT count(*) FROM alert_reactions"
    ).fetchone() == (1,)
    assert (
        hashlib.sha256((policy / "allocation.json").read_bytes()).hexdigest() == before
    )


def test_durable_inbound_reconcile_reapplies_after_handler_crash(tmp_path):
    news, ops, rid = _bound(tmp_path)
    dialogue = ExecutionDialogueStore(tmp_path / "dialogue.sqlite", clock=lambda: NOW)
    dialogue.record_inbound(
        provider_update_id=7,
        message_id=42,
        reply_to_message_id=42,
        raw_text=("n1:d:" + rid[:32]).encode(),
        next_offset=8,
    )
    handler = build_news_callback_handler(news, ops.read_outbox, now=lambda: NOW)
    assert run_news_feedback_reconcile(dialogue, handler) == ("RECORDED",)
    assert run_news_feedback_reconcile(dialogue, handler) == ("DUPLICATE",)


def test_news_lane_is_absent_without_activation_gate(tmp_path):
    deps = build_pulse_production_deps(
        root=tmp_path,
        clock=lambda: NOW,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=NOW,
    )
    assert deps.news_callback_handler is None and deps.news_outbox_reconcile is None
    assert deps.news_delivery_runner is None
    assert deps.news_producer_deps is None
    assert not (tmp_path / "stores" / "news_episodes.sqlite3").exists()


def test_live_composition_injects_its_single_canonical_ops_kernel(
    tmp_path, monkeypatch
):
    import tokmo.programs.pulse_production_deps as pulse_deps
    from tokmo import composition

    created = []
    captured = []
    real_store = OpsKernelStore
    real_builder = pulse_deps.build_pulse_production_deps

    def store_factory(path, *args, **kwargs):
        store = real_store(path, *args, **kwargs)
        created.append(store)
        return store

    def builder(*args, **kwargs):
        captured.append(kwargs["news_ops_kernel"])
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(composition, "OpsKernelStore", store_factory)
    monkeypatch.setattr(pulse_deps, "build_pulse_production_deps", builder)
    project = tmp_path / "project"
    (project / "watchlists").mkdir(parents=True)
    for market in ("KR", "US"):
        (project / "watchlists" / f"{market}.json").write_text(
            json.dumps({"schema_version": 1, "market": market, "entries": []})
        )
    validator = project / "scripts" / "playbooks" / "validate_playbooks.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("def validate_tree(root): return []\n")
    (project / ".env.toss").write_text("TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    gate = runtime / "news_activation.json"
    gate.write_text(
        json.dumps({"enabled": True, "cutoff": NOW.isoformat(), "approved_by": "woong"})
    )
    os.chmod(gate, 0o600)
    deps = composition.build_pulse_production_live_deps(
        project_root=project,
        clock=lambda: NOW,
        runtime_root=runtime,
        dialogue_cutoff=NOW,
    )
    assert len(created) == 1 and captured == created
    assert created[0].path == runtime / "ops" / "ops_kernel.sqlite3"
    assert callable(deps.news_callback_handler)
    assert (runtime / "stores" / "news_episodes.sqlite3").exists()


def test_news_lane_activates_only_with_approved_gate(tmp_path):
    def build(root):
        return build_pulse_production_deps(
            root=root,
            clock=lambda: NOW,
            watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
            dialogue_cutoff=NOW,
        )

    enabled = tmp_path / "enabled"
    enabled.mkdir()
    gate = enabled / "news_activation.json"
    gate.write_text(
        json.dumps({"enabled": True, "cutoff": NOW.isoformat(), "approved_by": "woong"})
    )
    os.chmod(gate, 0o600)
    deps = build(enabled)
    assert all(
        callable(value)
        for value in (
            deps.news_callback_handler,
            deps.news_outbox_reconcile,
            deps.news_delivery_runner,
        )
    )
    for index, payload in enumerate(
        ("", '{"enabled":false}', json.dumps({"enabled": True, "approved_by": "woong"}))
    ):
        root = tmp_path / f"invalid-{index}"
        root.mkdir()
        path = root / "news_activation.json"
        path.write_text(payload)
        os.chmod(path, 0o600)
        invalid = build(root)
        assert (
            invalid.news_callback_handler
            is invalid.news_outbox_reconcile
            is invalid.news_delivery_runner
            is None
        )
        assert invalid.news_producer_deps is None
        assert not (root / "stores" / "news_episodes.sqlite3").exists()
    mode = tmp_path / "bad-mode"
    mode.mkdir()
    path = mode / "news_activation.json"
    path.write_text(
        json.dumps({"enabled": True, "cutoff": NOW.isoformat(), "approved_by": "woong"})
    )
    os.chmod(path, 0o644)
    invalid = build(mode)
    assert (
        invalid.news_callback_handler
        is invalid.news_outbox_reconcile
        is invalid.news_delivery_runner
        is None
    )
    assert invalid.news_producer_deps is None


def test_live_deps_survive_unhealthy_ops_kernel_when_news_gate_off(tmp_path):
    from tokmo import composition

    project = tmp_path / "project"
    (project / "watchlists").mkdir(parents=True)
    for market in ("KR", "US"):
        (project / "watchlists" / f"{market}.json").write_text(
            json.dumps({"schema_version": 1, "market": market, "entries": []})
        )
    validator = project / "scripts" / "playbooks" / "validate_playbooks.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("def validate_tree(root): return []\n")
    (project / ".env.toss").write_text("TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n")
    runtime = tmp_path / "runtime"
    (runtime / "ops").mkdir(parents=True)
    os.chmod(runtime / "ops", 0o700)
    kernel = runtime / "ops" / "ops_kernel.sqlite3"
    kernel.write_bytes(b"not-a-sqlite-database-header" * 8)
    os.chmod(kernel, 0o600)
    assert not (runtime / "news_activation.json").exists()
    deps = composition.build_pulse_production_live_deps(
        project_root=project,
        clock=lambda: NOW,
        runtime_root=runtime,
        dialogue_cutoff=NOW,
    )
    assert deps.news_callback_handler is None and deps.news_outbox_reconcile is None
    assert deps.news_delivery_runner is None
    assert deps.news_producer_deps is None
    assert not (runtime / "stores" / "news_episodes.sqlite3").exists()


def _write_gate(root, payload, mode=0o600, name="news_activation.json"):
    path = root / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    os.chmod(path, mode)
    return path


def _gate_body(**extra):
    return {"enabled": True, "cutoff": NOW.isoformat(), "approved_by": "woong", **extra}


def _build(root):
    return build_pulse_production_deps(
        root=root,
        clock=lambda: NOW,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=NOW,
    )


def test_r_gate_2_3_producer_mode_matrix_fails_closed(tmp_path):
    from tokmo.programs.news_production import NewsProducerDepsV1

    absent = tmp_path / "absent"
    absent.mkdir()
    _write_gate(absent, _gate_body())
    deps = _build(absent)
    assert deps.news_producer_deps is None
    assert all(
        callable(value)
        for value in (
            deps.news_callback_handler,
            deps.news_outbox_reconcile,
            deps.news_delivery_runner,
        )
    )
    for index, mode_token in enumerate(("LIVE", "live", "on", 1, None, "SHADOW ")):
        root = tmp_path / f"mode-{index}"
        root.mkdir()
        _write_gate(root, _gate_body(producer_mode=mode_token))
        assert _build(root).news_producer_deps is None, mode_token
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    _write_gate(shadow, _gate_body(producer_mode="SHADOW"))
    deps = _build(shadow)
    assert isinstance(deps.news_producer_deps, NewsProducerDepsV1)
    assert not hasattr(deps.news_producer_deps, "news_producer_runner")
    assert not hasattr(deps, "news_producer_runner")
    kernel = OpsKernelStore(
        shadow / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    assert tuple(
        kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE channel='TELEGRAM'")
        .fetchone()
    ) == (0,)


def test_r_gate_4_world_readable_gate_rejects_producer(tmp_path):
    _write_gate(tmp_path, _gate_body(producer_mode="SHADOW"), mode=0o644)
    deps = _build(tmp_path)
    assert deps.news_producer_deps is None
    assert deps.news_callback_handler is None


def test_r_gate_6_replacement_race_never_adopts_second_payload(tmp_path, monkeypatch):
    from tokmo.programs.pulse_production_deps import read_news_gate

    gate = _write_gate(tmp_path, _gate_body())
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(_gate_body(producer_mode="SHADOW")))
    os.chmod(replacement, 0o644)
    real_fstat = os.fstat
    swapped = {"done": False}

    def racing_fstat(fd):
        if not swapped["done"]:
            swapped["done"] = True
            os.replace(replacement, gate)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", racing_fstat)
    activation, mode = read_news_gate(tmp_path)
    assert mode == "OFF"
    assert swapped["done"] is True


def test_r_gate_7_8_9_10_degenerate_gate_files_are_off(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate

    target = tmp_path / "target"
    target.mkdir()
    real = _write_gate(target, _gate_body(producer_mode="SHADOW"))
    link_root = tmp_path / "symlink"
    link_root.mkdir()
    (link_root / "news_activation.json").symlink_to(real)
    activation, mode = read_news_gate(link_root)
    assert activation.enabled is False and mode == "OFF"
    hard_root = tmp_path / "hardlink"
    hard_root.mkdir()
    hard = _write_gate(hard_root, _gate_body(producer_mode="SHADOW"))
    os.link(hard, hard_root / "extra-link.json")
    activation, mode = read_news_gate(hard_root)
    assert activation.enabled is False and mode == "OFF"
    big_root = tmp_path / "oversize"
    big_root.mkdir()
    _write_gate(big_root, _gate_body(producer_mode="SHADOW", padding="x" * 5000))
    activation, mode = read_news_gate(big_root)
    assert activation.enabled is False and mode == "OFF"
    for index, payload in enumerate(
        ("{not json", '"scalar"', "[1,2]", b"\xff\xfe".decode("latin-1"))
    ):
        bad_root = tmp_path / f"malformed-{index}"
        bad_root.mkdir()
        _write_gate(bad_root, payload)
        activation, mode = read_news_gate(bad_root)
        assert activation.enabled is False and mode == "OFF"


def test_r_gate_11_wrapper_matches_legacy_judgments(tmp_path):
    from tokmo.programs.pulse_production_deps import (
        read_news_activation,
        read_news_gate,
    )
    from tokmo.records.dialogue_activation import read_dialogue_activation

    cases = (
        {"enabled": True, "approved_by": "woong", "producer_mode": "SHADOW"},
        {
            "enabled": True,
            "cutoff": "2026-08-21T00:00:00",
            "approved_by": "woong",
            "producer_mode": "SHADOW",
        },
        {
            "enabled": True,
            "cutoff": NOW.isoformat(),
            "approved_by": "",
            "producer_mode": "SHADOW",
        },
        {
            "enabled": "true",
            "cutoff": NOW.isoformat(),
            "approved_by": "woong",
            "producer_mode": "SHADOW",
        },
    )
    for index, payload in enumerate(cases):
        root = tmp_path / f"case-{index}"
        root.mkdir()
        path = _write_gate(root, payload)
        activation, mode = read_news_gate(root)
        assert activation.enabled is False and mode == "OFF"
        assert read_news_activation(root) == activation
        assert read_dialogue_activation(path).enabled is False
    good = tmp_path / "good"
    good.mkdir()
    path = _write_gate(good, _gate_body())
    assert read_news_activation(good) == read_dialogue_activation(path)


def _composition_project(tmp_path):
    project = tmp_path / "project"
    (project / "watchlists").mkdir(parents=True)
    for market in ("KR", "US"):
        (project / "watchlists" / f"{market}.json").write_text(
            json.dumps({"schema_version": 1, "market": market, "entries": []})
        )
    validator = project / "scripts" / "playbooks" / "validate_playbooks.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("def validate_tree(root): return []\n")
    (project / ".env.toss").write_text("TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    return project, runtime


def test_r_gate_12_assembly_opens_gate_exactly_once(tmp_path, monkeypatch):
    import tokmo.programs.pulse_production_deps as pulse_deps
    from tokmo import composition

    project, runtime = _composition_project(tmp_path)
    _write_gate(runtime, _gate_body(producer_mode="SHADOW"))
    reads = []
    real_gate = pulse_deps.read_news_gate

    def counting_gate(root):
        snapshot = real_gate(root)
        reads.append(snapshot)
        return snapshot

    monkeypatch.setattr(pulse_deps, "read_news_gate", counting_gate)
    deps = composition.build_pulse_production_live_deps(
        project_root=project,
        clock=lambda: NOW,
        runtime_root=runtime,
        dialogue_cutoff=NOW,
    )
    assert len(reads) == 1
    assert deps.news_producer_deps is not None
    assert all(
        callable(value)
        for value in (
            deps.news_callback_handler,
            deps.news_outbox_reconcile,
            deps.news_delivery_runner,
        )
    )
    assert (runtime / "ops" / "ops_kernel.sqlite3").exists()


def test_r_gate_13_snapshot_is_passed_not_reread(tmp_path, monkeypatch):
    import tokmo.programs.pulse_production_deps as pulse_deps
    from tokmo import composition

    project, runtime = _composition_project(tmp_path)
    _write_gate(runtime, _gate_body(producer_mode="SHADOW"))
    real_builder = pulse_deps.build_pulse_production_deps
    real_gate = pulse_deps.read_news_gate
    seen = {"snapshots": [], "builder_gate": [], "reads_inside_builder": 0}

    def counting_gate(root):
        snapshot = real_gate(root)
        seen["snapshots"].append(snapshot)
        if seen.get("in_builder"):
            seen["reads_inside_builder"] += 1
        return snapshot

    def capturing_builder(*args, **kwargs):
        seen["builder_gate"].append(kwargs.get("news_gate"))
        seen["in_builder"] = True
        try:
            return real_builder(*args, **kwargs)
        finally:
            seen["in_builder"] = False

    monkeypatch.setattr(pulse_deps, "read_news_gate", counting_gate)
    monkeypatch.setattr(pulse_deps, "build_pulse_production_deps", capturing_builder)
    deps = composition.build_pulse_production_live_deps(
        project_root=project,
        clock=lambda: NOW,
        runtime_root=runtime,
        dialogue_cutoff=NOW,
    )
    assert seen["builder_gate"] == [seen["snapshots"][0]]
    assert seen["reads_inside_builder"] == 0
    assert deps.news_producer_deps is not None
    # off: absent gate resolves the whole lane disabled through one snapshot
    off_root = tmp_path / "off"
    off_root.mkdir()
    off_project, off_runtime = _composition_project(off_root)
    seen["builder_gate"].clear()
    seen["snapshots"].clear()
    off = composition.build_pulse_production_live_deps(
        project_root=off_project,
        clock=lambda: NOW,
        runtime_root=off_runtime,
        dialogue_cutoff=NOW,
    )
    assert seen["builder_gate"] and seen["builder_gate"][0][0].enabled is False
    assert off.news_callback_handler is None and off.news_producer_deps is None
    assert not (off_runtime / "stores" / "news_episodes.sqlite3").exists()
    assert not (off_runtime / "ops" / "ops_kernel.sqlite3").exists()
