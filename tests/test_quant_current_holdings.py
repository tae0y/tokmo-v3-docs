"""Synthetic local accounts only; fake models, no broker/Telegram/ledger replay."""

import json
import runpy
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_investment_followup import choice, interpretation
from test_investment_plans import register, supplied, unchanged
from test_investment_price_checkpoint import approved, market, quote
from test_pension_analysis_sources import source as historical_source

from tokmo.programs.investment_followup import review_choice
from tokmo.programs.quant_decision_calculations import portfolio_totals
from tokmo.programs.quant_decision_inputs import load_local_snapshot
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.investment_followup import InvestmentFollowupStore
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.quant_decisions import parse_snapshot

NOW = datetime(2026, 9, 29, 1, tzinfo=UTC)
PERSONAL = "toss-acct-" + "a" * 24
PENSION = "pension-excel-" + "b" * 24


def holdings(
    root,
    rows=(("000001", "17", "100"), ("000002", "8", "200")),
    *,
    captured=NOW - timedelta(hours=1),
):
    accounts = tuple(
        AccountHoldingsV1(
            account,
            ordinal,
            tuple(
                HoldingItemV1(symbol, quantity, cost, "KRW", "KR", "market")
                for symbol, quantity, cost in items
            ),
        )
        for ordinal, (account, items) in enumerate(
            [(PERSONAL, [("000001", "3", "90")]), (PENSION, rows)], 1
        )
    )
    state = CurrentHoldingsV1(
        1,
        accounts,
        True,
        captured,
        {
            a.account_scope: AccountScopeIdentityV1(
                a.account_scope, None, a.ordinal, None
            )
            for a in accounts
        },
    )
    CurrentHoldingsStore(
        root / "stores/current_holdings.json", clock=lambda: captured
    ).replace(
        state,
        identity_fingerprints={a.account_scope: str(a.ordinal) * 64 for a in accounts},
    )


def sources(root):
    holdings(root)
    with sqlite3.connect(root / "stores/pension_registry.sqlite3") as connection:
        connection.executescript(
            "CREATE TABLE pension_revisions(revision_id, source_as_of, state);"
            "CREATE TABLE pension_active_pointer(active_revision_id);"
        )
        connection.execute(
            "INSERT INTO pension_revisions VALUES(?,?,?)",
            ("synthetic", "2026-09-20T00:00:00+00:00", "ACTIVE"),
        )
        connection.execute("INSERT INTO pension_active_pointer VALUES('synthetic')")
    with sqlite3.connect(root / "stores/news_episodes.sqlite3") as connection:
        connection.execute("CREATE TABLE news_decision_events(state TEXT)")
    # A real, valid historical manifest whose obsolete inventory differs.
    manifest = root / "old-analysis.json"
    manifest.write_text(json.dumps(dict(historical_source(), account=PENSION)))
    supplement = root / "prices.json"
    supplement.write_text(
        json.dumps(
            {
                "instruments": [
                    dict(
                        symbol=s,
                        market="KR",
                        price=p,
                        price_at=(NOW - timedelta(minutes=1)).isoformat(),
                    )
                    for s, p in [
                        ("000001", "150"),
                        ("000002", "250"),
                        ("000003", "400"),
                    ]
                ],
                "positions": [
                    dict(
                        account=PENSION,
                        key="KR:000001",
                        quantity="999",
                        average_cost="1",
                    )
                ],
            }
        )
    )
    return manifest, supplement


def register_choices(root, lane):
    from tokmo.programs.investment_price_checkpoint import register_checkpoint

    path = root / "private/investment-followup.sqlite3"
    with closing(InvestmentFollowupStore(path)) as with_store:
        for index in range(2):
            if lane in {"plan", "monitored_plan"}:
                value = supplied(
                    review_at=NOW.isoformat(), monitor=lane == "monitored_plan"
                )
                value["interpretation"].update(account=PENSION, scope="retirement")
                register(with_store, root, value, source=f"synthetic:{index}")
            elif lane == "checkpoint":
                value = approved()
                value["interpretation"]["account"] = PENSION
                value["checkpoint"].update(
                    starts_at=NOW.isoformat(),
                    expires_at=(NOW + timedelta(days=1)).isoformat(),
                )
                register_checkpoint(
                    with_store, value, source_id=f"synthetic:{index}", at=NOW
                )
            else:
                choice(
                    with_store,
                    source=f"synthetic:{index}",
                    at=NOW,
                    value=dict(interpretation(), account=PENSION, scope="retirement"),
                )
    if lane == "checkpoint":
        market(root, at=NOW)
        quote(root, at=NOW)
    return path


@pytest.mark.parametrize(
    "lane",
    [
        "periodic",
        "news",
        "plan",
        "monitored_plan",
        "checkpoint",
        "interrupted",
        "missing",
        "corrupt",
    ],
)
def test_each_judgment_reads_saved_holdings_after_previous_model_call(
    tmp_path, monkeypatch, capsys, lane
):
    import run_investment_followup as cli

    sources(tmp_path)
    db = register_choices(tmp_path, lane)
    now = NOW

    class Clock:
        @staticmethod
        def now(tz):
            return now.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    if lane == "news":
        event = dict(
            id="synthetic-event",
            updated_at=NOW.isoformat(),
            decision=dict(evidence_ids=["synthetic-source"]),
            evidence=[
                dict(
                    id="synthetic-source",
                    observed_at=NOW.isoformat(),
                    title="합성 소식",
                    text="수요 변화",
                )
            ],
        )
        with sqlite3.connect(tmp_path / "stores/news_episodes.sqlite3") as connection:
            connection.execute(
                "INSERT INTO news_decision_events VALUES(?)", (json.dumps(event),)
            )
    seen = []

    def model(prompt):
        nonlocal now
        if "\nINPUT\n" not in prompt:
            choices = json.loads(prompt.rsplit("\n", 1)[1])["choices"]
            return dict(
                matches=[
                    dict(
                        choice_id=c["id"],
                        event_ids=["synthetic-event"],
                        reason="합성 수요 점검",
                        assessment="REVIEW",
                    )
                    for c in choices
                ],
                unmatched_reason="없음",
            )
        packet = json.loads(prompt.split("\nINPUT\n")[1])
        seen.append(packet)
        if lane == "interrupted" and len(seen) == 2:
            raise KeyboardInterrupt("synthetic interrupted B call")
        if len(seen) == 1:
            now += timedelta(minutes=12)
            if lane != "interrupted":
                holdings(
                    tmp_path,
                    [("000001", "23", "120"), ("000003", "6", "300")],
                    captured=now,
                )
        return unchanged(prompt)

    monkeypatch.setattr(cli, "QuantModel", lambda **kw: model)
    args = [
        "tick",
        "--state",
        str(db),
        "--data-root",
        str(tmp_path),
        "--project-root",
        str(tmp_path),
        "--supplement",
        str(tmp_path / "prices.json"),
    ]
    if lane == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            cli.main(args)
        now += timedelta(minutes=5)
    if lane == "missing":
        (tmp_path / "stores/current_holdings.json").unlink()
    elif lane == "corrupt":
        (tmp_path / "stores/current_holdings.json").write_text("{broken")
    code = cli.main(args)
    result = json.loads(capsys.readouterr().out)
    if lane in {"missing", "corrupt"}:
        assert (
            code == 2
            and result["errors"][0]["stage"] == "snapshot"
            and not result["reviewed"]
            and not seen
        )
        return
    if lane == "interrupted":
        with sqlite3.connect(db) as connection:
            rows = connection.execute(
                "SELECT choice_id, count(*) FROM investment_reviews GROUP BY choice_id"
            ).fetchall()
            journal = connection.execute(
                "SELECT status, count(*) FROM investment_calls WHERE id LIKE 'review%' GROUP BY status"
            ).fetchall()
        assert (len(seen) - 2, rows, journal) == (
            0,
            [(seen[0]["choice"]["id"], 1)],
            [("OK", 1), ("STARTED", 1)],
        )
        assert code == 2 and len(result["reviewed"]) == 1
        assert [
            (e["choice_id"], e["error"], e["retryable"], e["retry_at"])
            for e in result["errors"]
        ] == [(seen[1]["choice"]["id"], "ValueError", False, None)]
        assert cli.main(args + ["--retry-attempt", "checked-2"]) == 0
        assert (
            len(json.loads(capsys.readouterr().out)["reviewed"]) == 1 and len(seen) == 3
        )
        assert (
            seen[-1]["choice"]["id"] == seen[1]["choice"]["id"]
            and seen[-1]["as_of"] == now.isoformat()
        )
        return
    assert code == 0
    assert len(result["reviewed"]) == len(seen) == 2
    for packet, expected, captured in [
        (seen[0], "17", NOW - timedelta(hours=1)),
        (seen[1], "23", now),
    ]:
        if lane in {"periodic", "news"}:
            assert packet["quant"] == {}
            context = packet["current_holdings"]
            assert context["account"] == PENSION and context["scope"] == "retirement"
            assert set(p["key"] for p in context["positions"]) == {"KR:000001"}
            positions = context["positions"]
        else:
            positions = packet["quant"]["KR:000001"]["holdings"]
        assert (positions[0]["quantity"], positions[0]["average_cost"]) == (
            expected,
            "100" if expected == "17" else "120",
        )
        assert positions[0]["captured_at"] == captured.isoformat()
        assert positions[0]["observed_at"] == "2026-09-20T00:00:00+00:00"
        assert positions[0]["source_status"] == "PENSION_SOURCE_DATED"
        assert packet["choice"]["execution_status"] == "NOT_VERIFIED"
    assert seen[1]["as_of"] == now.isoformat()
    if lane in {"plan", "monitored_plan", "checkpoint"}:
        portfolio = seen[1]["quant"]["KR:000001"]["portfolio_holdings"]
        assert {p["key"] for p in portfolio} == {"KR:000001", "KR:000003"}

    if lane == "periodic":
        with closing(InvestmentFollowupStore(db)) as store:
            first = store.latest_review(seen[0]["choice"]["id"], at=now)
            next_review = review_choice(
                store,
                seen[0]["choice"],
                seen[0]["evidence"],
                snapshot=parse_snapshot(
                    load_local_snapshot(tmp_path, tmp_path, as_of=now)
                ),
                model=model,
                at=now,
                trigger=seen[0]["trigger"],
                quant_context=False,
            )
            assert first["input_key"] != next_review["input_key"] and len(seen) == 3


@pytest.mark.parametrize("reader", ["loader", "ordinary_cli"])
def test_current_inventory_costs_zero_and_historical_comparison_are_separate(
    tmp_path, monkeypatch, capsys, reader
):
    import run_quant_decisions as cli

    manifest, supplement = sources(tmp_path)
    snapshots = []
    build = cli.build_report

    def report(snapshot, **kw):
        snapshots.append(snapshot)
        return build(snapshot, **kw)

    monkeypatch.setattr(cli, "build_report", report)
    monkeypatch.setattr(
        "tokmo.kis_composition.refresh_kis_etf_inputs",
        lambda **kw: {"status": "SYNTHETIC", "requests": 0},
    )

    def read():
        if reader == "loader":
            return parse_snapshot(
                load_local_snapshot(
                    tmp_path,
                    tmp_path,
                    as_of=NOW,
                    supplement=supplement,
                    include_context=False,
                    pension_analysis=manifest,
                )
            )
        assert (
            cli.main(
                [
                    "--data-root",
                    str(tmp_path),
                    "--project-root",
                    str(tmp_path),
                    "--supplement",
                    str(supplement),
                    "--model",
                    "off",
                    "--new-limit",
                    "0",
                    "--output",
                    str(tmp_path / "reports"),
                ]
            )
            == 0
        )
        capsys.readouterr()
        return snapshots[-1]

    before = (tmp_path / "stores/current_holdings.json").read_bytes()
    current = read()
    assert portfolio_totals(current)["retirement:KRW"]["equity"] == Decimal(4550)
    assert [
        (p.quantity, p.average_cost) for p in current.positions if p.scope == "personal"
    ] == [(Decimal(3), Decimal(90))]
    historical = load_local_snapshot(
        tmp_path,
        tmp_path,
        as_of=NOW,
        supplement=supplement,
        pension_analysis=manifest,
        historical_pension=True,
    )
    assert [
        p["quantity"] for p in historical["positions"] if p["scope"] == "retirement"
    ] == ["100"]
    assert (tmp_path / "stores/current_holdings.json").read_bytes() == before
    holdings(tmp_path, [("000001", "0", "120"), ("000003", "6", "300")])
    changed = read()
    assert portfolio_totals(changed)["retirement:KRW"]["equity"] == Decimal(2400)
    assert {
        p.key: p.quantity for p in changed.positions if p.scope == "retirement"
    } == {"KR:000001": Decimal(0), "KR:000003": Decimal(6)}
    holdings(tmp_path, [])
    assert not [p for p in read().positions if p.scope == "retirement"]


@pytest.mark.parametrize("entry", ["cron", "conversation"])
def test_wrappers_do_not_forward_legacy_pension_overlays(
    tmp_path, monkeypatch, capsys, entry
):
    import os

    import run_investment_followup_cron as cron

    root = tmp_path / "var/data"
    root.mkdir(parents=True)
    (root / "investment_followup.json").write_text(
        json.dumps(
            dict(
                enabled=True,
                pension_analysis="obsolete.json",
                pension_baseline="obsolete-baseline.json",
            )
        )
    )
    seen = []

    def run(args):
        seen.extend(args)
        print(json.dumps(dict(errors=[], reviewed=[])))
        return 0

    if entry == "cron":
        monkeypatch.setattr(cron, "run", run)
        assert cron.main(["--root", str(root), "--project-root", str(tmp_path)]) == 0
        capsys.readouterr()
    else:
        relative = "ops/hermes-cron/tokmo_investment_followup.py"
        script = tmp_path / relative
        script.parent.mkdir(parents=True)
        script.write_text((Path(__file__).resolve().parents[1] / relative).read_text())
        monkeypatch.setattr(sys, "argv", [str(script), "tick"])
        monkeypatch.setattr(
            os, "execve", lambda executable, args, env: seen.extend(args)
        )
        runpy.run_path(str(script), run_name="synthetic_shim")
    assert "--pension-analysis" not in seen and "--pension-baseline" not in seen
