"""A chain inside the real engine: whole-chain waits are keyed by the chain, not by a member."""

import json
from datetime import timedelta

import pytest
from subscription_json_model import SubscriptionInferenceError
from test_news_decision_engine import decision, observation
from test_news_incremental_review import AT
from test_news_review_efficiency import system

from tokmo.programs.model_chain import ModelChain
from tokmo.programs.news_model_wait import ModelWait
from tokmo.records.news_decision_store import kst


class Member:
    def __init__(self, model, failures=()):
        self.model, self.failures, self.calls = model, list(failures), 0
        self.reasoning_effort, self.max_output_tokens, self.policy_fingerprint = (
            "low",
            4096,
            "stable",
        )

    def __call__(self, request):
        self.calls += 1
        if self.failures:
            category = self.failures.pop(0)  # None = this call succeeds
            if category:
                raise SubscriptionInferenceError(category, exit_code=1)
        return dict(
            decision(
                [r["id"] for r in request["evidence"]],
                notify=False,
                change="NONE",
                urgency="NORMAL",
                review_dependencies=dict(all_portfolio=False, keys=["KR:1"]),
                followup_scope=dict(all_choices=False, choice_ids=["choice1"]),
                recheck=None,
            ),
            _model=self.model,
        )


@pytest.mark.parametrize("legacy_outer", [False, True])
def test_chain_exhaustion_waits_under_the_chain_identity_and_recovers_by_itself(
    tmp_path, legacy_outer
):
    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    clock = {"at": AT}
    a, b = Member("glm-5.3", ["QUOTA"]), Member("codex:gpt-5.6-terra", ["TRANSIENT"])
    chain = ModelChain(
        [a, b],
        stage="analyze",
        state_path=tmp_path / "chain.json",
        clock=lambda: clock["at"],
    )
    engine.analyze = chain
    try:
        engine.tick([observation("a", AT)], AT)
        assert store.counts() == {"WAITING": 1} and a.calls == 1 and b.calls == 1
        waits = store.controls("model_wait:")
        assert (
            len(waits) == 1
            and waits[0]["model"]
            == chain.model
            == "chain:analyze[glm-5.3>codex:gpt-5.6-terra]"
        )
        assert waits[0]["category"] == "QUOTA"
        if legacy_outer:
            store.set_control(
                ModelWait(store).key(chain),
                dict(waits[0], retry_at=None, manual_required=True),
            )
        else:
            assert waits[0]["retry_at"] == kst(AT + timedelta(minutes=5))
            assert waits[0]["manual_required"] is False
        # Another chain that shares the preferred model is a different key: it is not blocked.
        other = ModelChain(
            [Member("glm-5.3"), Member("claude:claude-haiku-4-5-20251001")],
            stage="analyze-first",
            state_path=tmp_path / "chain.json",
            clock=lambda: clock["at"],
        )
        ModelWait(store).check(other, AT)
        clock["at"] = AT + timedelta(minutes=4)
        engine.model_wait = ModelWait(store)
        assert (
            engine.tick([], clock["at"])["calls"] == 0 and a.calls == 1 and b.calls == 1
        )
        assert store.counts() == {"WAITING": 1}
        # Restart both layers; the due fallback can answer while the primary stays blocked.
        clock["at"] = AT + timedelta(minutes=5)
        chain = ModelChain(
            [a, b],
            stage="analyze",
            state_path=tmp_path / "chain.json",
            clock=lambda: clock["at"],
        )
        engine.analyze = chain
        engine.model_wait = ModelWait(store)
        recovered = engine.tick([], clock["at"])
        assert (
            recovered["calls"] == 1
            and store.counts() == {"DONE": 1}
            and store.controls("model_wait:") == []
        )
        assert (
            a.calls == 1
            and b.calls == 2
            and set(chain.summary()["waiting"]) == {"glm-5.3"}
        )
    finally:
        store.close()


def test_failed_member_attempt_keeps_its_reservation_in_the_budget_and_the_call_is_reused(
    tmp_path,
):
    from tokmo.records.news_decision_store import NewsDecisionStore

    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    a, b = Member("glm-5.3", ["TRANSIENT"]), Member("codex:gpt-5.6-terra")
    chain = ModelChain(
        [a, b], stage="analyze", state_path=tmp_path / "chain.json", clock=lambda: AT
    )
    engine.analyze = chain
    try:
        engine.tick([observation("a", AT)], AT)
        rows = store.connection.execute(
            "SELECT id,status,tokens,reservation,error FROM news_decision_calls WHERE stage='analyze' ORDER BY id"
        ).fetchall()
        assert store.counts() == {"DONE": 1} and a.calls == 1 and b.calls == 1
        main = [r for r in rows if "#" not in r[0]]
        attempts = [r for r in rows if "#" in r[0]]
        assert len(main) == 1 and main[0][1] == "OK"
        assert (
            len(attempts) == 1
            and attempts[0][0] == main[0][0] + "#glm-5.3"
            and attempts[0][1] == "ERROR"
        )
        assert (
            attempts[0][2] == attempts[0][3] == main[0][3]
            and attempts[0][4] == "chain member failed: TRANSIENT"
        )
        recorded = store.failed_attempts(main[0][0])
        assert [(r["model"], r["tokens"], r["error"]) for r in recorded] == [
            ("glm-5.3", main[0][3], "chain member failed: TRANSIENT")
        ]
        reservation = main[0][3]
        assert chain.attempt_sink is None  # unbound again after the call
        # The same finished request is served from the journal: no member is called again.
        report = {"calls": 0}
        request = json.loads(
            store.connection.execute(
                "SELECT request FROM news_decision_calls WHERE id=?", (main[0][0],)
            ).fetchone()[0]
        )
        assert (
            engine._invoke("analyze", chain, request, report, AT)["_model"]
            == "codex:gpt-5.6-terra"
        )
        assert report["calls"] == 0 and a.calls == 1 and b.calls == 1
    finally:
        store.close()
    from tokmo.programs.news_model_wait import ModelWait

    def fresh(name, budget, *members):
        store = NewsDecisionStore(tmp_path / name)
        engine.store, engine.model_wait, engine.token_budget = (
            store,
            ModelWait(store),
            budget,
        )
        engine.analyze = ModelChain(
            list(members),
            stage="analyze",
            state_path=tmp_path / (name + ".chain.json"),
            clock=lambda: AT,
        )
        return store

    def analyze_rows(store):
        return store.connection.execute(
            "SELECT id,status,tokens FROM news_decision_calls WHERE stage='analyze' ORDER BY id"
        ).fetchall()

    # Budget exactly 2R: A's failed attempt (R) plus B's attempt (R) fit, so B is called.
    a, b = Member("glm-5.3", ["TRANSIENT"]), Member("codex:gpt-5.6-terra")
    store2 = fresh("two.db", 2 * reservation, a, b)
    try:
        engine.tick([observation("b", AT)], AT)
        assert a.calls == 1 and b.calls == 1 and store2.counts() == {"DONE": 1}
        rows = analyze_rows(store2)
        assert (
            len(rows) == 2
            and sum(r[2] for r in rows) == 2 * reservation
            and {r[1] for r in rows} == {"OK", "ERROR"}
        )
    finally:
        store2.close()
    # Budget below 2R: A fails, B is refused (not silently paid for); one row = the one attempt that happened.
    a, b = Member("glm-5.3", ["TRANSIENT"]), Member("codex:gpt-5.6-terra")
    store3 = fresh("short.db", 2 * reservation - 1, a, b)
    try:
        engine.tick([observation("c", AT)], AT)
        assert a.calls == 1 and b.calls == 0
        assert (
            store3.counts() == {"ERROR": 1}
            and "budget" in store3.error_details()[0]["error"]
        )
        rows = analyze_rows(store3)
        assert [(r[1], r[2]) for r in rows] == [
            ("ERROR", reservation)
        ] and "#" not in rows[0][0]
    finally:
        store3.close()
    # Both fail within budget: two rows for two attempts (2R), and the whole-chain wait is recorded, not a budget error.
    a, b = (
        Member("glm-5.3", ["TRANSIENT"]),
        Member("codex:gpt-5.6-terra", ["TRANSIENT"]),
    )
    store4 = fresh("both.db", 3 * reservation, a, b)
    try:
        engine.tick([observation("d", AT)], AT)
        assert a.calls == 1 and b.calls == 1 and store4.counts() == {"WAITING": 1}
        assert store4.controls("model_wait:")[0]["model"] == engine.analyze.model
        rows = analyze_rows(store4)
        assert (
            len(rows) == 2
            and sum(r[2] for r in rows) == 2 * reservation
            and all(r[1] == "ERROR" for r in rows)
        )
        main_id = next(r[0] for r in rows if "#" not in r[0])
        assert [row["model"] for row in store4.failed_attempts(main_id)] == ["glm-5.3"]
    finally:
        store4.close()


def test_no_member_eligible_means_hold_without_a_row_a_reservation_or_a_wait_attempt(
    tmp_path,
):
    """Reviewer scenario: A backs off to 6h after three TRANSIENT failures, then B hits QUOTA. The
    whole-chain wait must not retry before any member may be tried, and a pass in between must not
    journal anything or count as a ModelWait attempt."""
    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    clock = {"at": AT}
    a = Member("glm-5.3", ["TRANSIENT", "TRANSIENT", "TRANSIENT"])
    b = Member("codex:gpt-5.6-terra", [None, None, None, "QUOTA"])
    chain = ModelChain(
        [a, b],
        stage="analyze",
        state_path=tmp_path / "chain.json",
        clock=lambda: clock["at"],
    )
    engine.analyze = chain
    rows = lambda: store.connection.execute(
        "SELECT count(*), coalesce(sum(tokens),0) FROM news_decision_calls WHERE stage='analyze'"
    ).fetchone()
    try:
        for n, minutes in enumerate((0, 5, 20)):
            clock["at"] = AT + timedelta(minutes=minutes)
            engine.model_wait = ModelWait(store)
            engine.tick([observation("abc"[n], clock["at"])], clock["at"])
        assert store.counts() == {"DONE": 3} and a.calls == 3 and b.calls == 3
        assert chain.summary()["waiting"]["glm-5.3"]["retry_at"] == kst(
            AT + timedelta(hours=6, minutes=20)
        )
        clock["at"] = AT + timedelta(minutes=21)
        engine.model_wait = ModelWait(store)
        engine.tick([observation("d", clock["at"])], clock["at"])
        assert (
            store.counts() == {"DONE": 3, "WAITING": 1}
            and a.calls == 3
            and b.calls == 4
        )
        wait = store.controls("model_wait:")[0]
        assert wait["category"] == "TRANSIENT" and wait["attempts"] == 1
        assert wait["retry_at"] == kst(
            AT + timedelta(hours=6, minutes=20)
        )  # A's eligibility, not 5 minutes
        before = rows()
        # A pass while every member is still waiting: held, nothing journaled, no attempt counted.
        clock["at"] = AT + timedelta(minutes=26)
        engine.model_wait = ModelWait(store)
        report = engine.tick([], clock["at"])
        assert (
            report["calls"] == 0 and a.calls == 3 and b.calls == 4 and rows() == before
        )
        assert store.controls("model_wait:")[0]["attempts"] == 1 and store.counts() == {
            "DONE": 3,
            "WAITING": 1,
        }
        # Even if the whole-chain wait were already over, the hold applies before any journal row.
        store.set_control(
            ModelWait(store).key(chain),
            dict(wait, retry_at=kst(AT + timedelta(minutes=25))),
        )
        engine.model_wait = ModelWait(store)
        report = engine.tick([], clock["at"])
        assert (
            report["calls"] == 0
            and a.calls == 3
            and rows() == before
            and report["model_deferred"][0]["held"] is True
        )
        assert report["model_deferred"][0]["retry_at"] == kst(
            AT + timedelta(hours=6, minutes=20)
        )
        # When A is eligible again it is really tried, answers, and the input completes.
        clock["at"] = AT + timedelta(hours=6, minutes=20)
        engine.model_wait = ModelWait(store)
        engine.tick([], clock["at"])
        assert (
            a.calls == 4
            and store.counts() == {"DONE": 4}
            and store.controls("model_wait:") == []
        )
    finally:
        store.close()


def test_a_chain_without_model_wait_is_refused_because_held_inputs_could_never_resume(
    tmp_path,
):
    import pytest
    from test_news_decision_engine import screen

    from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
    from tokmo.records.news_decision_store import NewsDecisionStore

    store = NewsDecisionStore(tmp_path / "state.db")
    chain = ModelChain(
        [Member("glm-5.3"), Member("codex:gpt-5.6-terra")],
        stage="analyze",
        clock=lambda: AT,
    )
    try:
        with pytest.raises(ValueError, match="model_wait_enabled"):
            NewsDecisionEngine(store, screen, chain, clock=RECORDED_TIME)
        with pytest.raises(ValueError, match="model_wait_enabled"):
            NewsDecisionEngine(
                store,
                screen,
                Member("glm-5.3"),
                clock=RECORDED_TIME,
                first_pass=chain,
                model_wait_enabled=False,
            )
        assert (
            NewsDecisionEngine(
                store, screen, chain, clock=RECORDED_TIME, model_wait_enabled=True
            ).model_wait
            is not None
        )
    finally:
        store.close()


def test_unclassified_failures_pass_through_the_chain_into_the_engine_streak_rule(
    tmp_path,
):
    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    a, b = Member("glm-5.3", ["EXECUTOR_UNKNOWN"] * 3), Member("codex:gpt-5.6-terra")
    chain = ModelChain(
        [a, b], stage="analyze", state_path=tmp_path / "chain.json", clock=lambda: AT
    )
    engine.analyze = chain
    try:
        for n, key in enumerate("abc"):
            engine.tick(
                [observation(key, AT + timedelta(minutes=n))], AT + timedelta(minutes=n)
            )
        assert (
            a.calls == 3 and b.calls == 0 and chain.events == []
        )  # never a fallback for an unclassified failure
        waits = store.controls("model_wait:")
        assert (
            len(waits) == 1
            and waits[0]["category"] == "TRANSIENT"
            and waits[0]["model"] == chain.model
        )
    finally:
        store.close()
