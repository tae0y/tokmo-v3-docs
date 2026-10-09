import json
from datetime import timedelta

import pytest

from tokmo.programs.model_chain import ChainExhausted, ModelChain
from tokmo.programs.news_model_wait import failure_category
from tokmo.records.news_decision_store import timestamp

T0 = timestamp("2026-09-25T10:00:00+09:00")


class Failure(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__("subscription inference failed: " + category)


class Member:
    def __init__(self, model, answers=(), effort="low", max_output_tokens=4096):
        self.model, self.answers, self.calls = model, list(answers), []
        self.reasoning_effort, self.max_output_tokens, self.policy_fingerprint = (
            effort,
            max_output_tokens,
            "fp-" + model,
        )

    def __call__(self, request):
        self.calls.append(request)
        answer = self.answers.pop(0) if self.answers else {"ok": self.model}
        if isinstance(answer, Exception):
            raise answer
        return dict(answer, _model=self.model)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def chain(tmp_path, clock, *members, stage="screen"):
    return ModelChain(
        members, stage=stage, state_path=tmp_path / "model-chain.json", clock=clock
    )


def test_preferred_model_answers_and_nothing_is_recorded(tmp_path):
    a, b = Member("glm-5.3"), Member("codex:gpt-5.6-terra")
    c = chain(tmp_path, Clock(T0), a, b)
    assert c({"q": 1}) == {"ok": "glm-5.3", "_model": "glm-5.3"}
    assert b.calls == [] and c.notices == [] and c.events == [] and c.active is a
    assert not (tmp_path / "model-chain.json").exists()


def test_chain_identity_is_stable_whichever_member_answers(tmp_path):
    a, b = (
        Member("glm-5.3", [Failure("QUOTA")], effort="low", max_output_tokens=4096),
        Member("codex:gpt-5.6-terra", effort="high", max_output_tokens=8192),
    )
    c = chain(tmp_path, Clock(T0), a, b)
    identity = "chain:screen[glm-5.3>codex:gpt-5.6-terra]"
    assert (
        c.model == identity
        and c.reasoning_effort == "low"
        and c.max_output_tokens == 8192
        and c.policy_fingerprint == "fp-glm-5.3"
    )
    assert c({"q": 1})["_model"] == "codex:gpt-5.6-terra"
    assert (
        c.model == identity and c.reasoning_effort == "low" and c.active is b
    )  # journal/wait keys do not move
    assert not hasattr(c, "no_such_attribute") and not hasattr(
        ModelChain.__new__(ModelChain), "members"
    )


def test_quota_failure_switches_within_the_call_and_stays_switched_until_the_probe(
    tmp_path,
):
    clock = Clock(T0)
    a, b = Member("glm-5.3", [Failure("QUOTA")]), Member("codex:gpt-5.6-terra")
    c = chain(tmp_path, clock, a, b)
    assert c({"q": 1})["_model"] == "codex:gpt-5.6-terra"
    assert c.notices == [
        "screen: glm-5.3 QUOTA · 6시간 뒤 다시 시도",
        "screen: glm-5.3 대신 codex:gpt-5.6-terra가 답함",
    ]
    assert [e["kind"] for e in c.events] == ["failed", "answered_by"] and c.events[0][
        "category"
    ] == "QUOTA"
    state = json.loads((tmp_path / "model-chain.json").read_text())["screen"]["glm-5.3"]
    assert (
        state["category"] == "QUOTA"
        and state["retry_at"] == "2026-09-25T16:00:00+09:00"
    )
    assert c.active is b and c.summary()["waiting"] == {
        "glm-5.3": {
            "category": "QUOTA",
            "retry_at": "2026-09-25T16:00:00+09:00",
            "attempts": 1,
            "retry_due": False,
        }
    }
    # A new worker pass an hour later: the preferred model is skipped without a call, and nothing new is announced.
    clock.at = T0 + timedelta(hours=1)
    c2 = chain(tmp_path, clock, a, b)
    assert c2.active is b and c2({"q": 2})["_model"] == "codex:gpt-5.6-terra"
    assert (
        len(a.calls) == 1
        and c2.notices == []
        and [e["kind"] for e in c2.events] == ["answered_by"]
    )
    # The probe interval passing is not a recovery: the fallback stays "active" and the wait stays
    # visible (retry_due) until the preferred model actually answers.
    clock.at = T0 + timedelta(hours=6)
    c3 = chain(tmp_path, clock, a, b)
    assert (
        c3.active is b
        and c3.summary()["waiting"]["glm-5.3"]["retry_due"] is True
        and c3.available(clock.at) == [a, b]
    )
    assert c3({"q": 3})["_model"] == "glm-5.3"
    assert (
        c3.active is a
        and c3.notices == ["screen: glm-5.3 복귀"]
        and c3.summary()["waiting"] == {}
    )
    assert json.loads((tmp_path / "model-chain.json").read_text()) == {"screen": {}}


def test_failed_attempts_are_handed_to_the_bound_sink_only_when_another_member_follows(
    tmp_path,
):
    a, b = Member("glm-5.3", [Failure("QUOTA")]), Member("codex:gpt-5.6-terra")
    c = chain(tmp_path, Clock(T0), a, b)
    seen = []
    c.attempt_sink = lambda member, category, at: seen.append(
        (member.model, category, at)
    )
    assert c({"q": 1})["_model"] == "codex:gpt-5.6-terra" and seen == [
        ("glm-5.3", "QUOTA", T0)
    ]
    # Both fail: only the first attempt goes to the sink; the last attempt is the call row itself.
    a2, b2 = (
        Member("glm-5.3", [Failure("QUOTA")]),
        Member("codex:gpt-5.6-terra", [Failure("AUTH")]),
    )
    c2 = chain(tmp_path, Clock(T0), a2, b2, stage="analyze")
    seen.clear()
    c2.attempt_sink = lambda member, category, at: seen.append(member.model)
    with pytest.raises(ChainExhausted):
        c2({"q": 1})
    assert seen == ["glm-5.3"]
    # The sink may refuse the next member (budget): the failure stays recorded, the fallback is not called.
    a3, b3 = Member("glm-5.3", [Failure("QUOTA")]), Member("codex:gpt-5.6-terra")
    c3 = chain(tmp_path, Clock(T0), a3, b3, stage="analyze-first")

    def refuse(member, category, at):
        raise ValueError("daily token budget exhausted")

    c3.attempt_sink = refuse
    with pytest.raises(ValueError, match="budget"):
        c3({"q": 1})
    assert b3.calls == [] and c3.summary()["waiting"]["glm-5.3"]["category"] == "QUOTA"


def test_quota_failure_waits_for_a_known_reset_plus_five_minutes_else_six_hours(
    tmp_path,
):
    clock = Clock(T0)
    known = {"at": None}
    reset = lambda provider, at: known["at"] if provider == "zai" else None

    def failing_chain():
        return ModelChain(
            [Member("glm-5.3", [Failure("QUOTA")]), Member("codex:gpt-5.6-terra")],
            stage="screen",
            state_path=tmp_path / "model-chain.json",
            clock=clock,
            quota_reset=reset,
        )

    # 13:38-style probe with no reset known: flat 6 hours.
    c = failing_chain()
    c({"q": 1})
    state = c.summary()["waiting"]["glm-5.3"]
    assert (
        state["retry_at"] == "2026-09-25T16:00:00+09:00"
        and c._state["glm-5.3"]["retry_source"] == "probe_interval"
    )
    assert c.notices[0] == "screen: glm-5.3 QUOTA · 6시간 뒤 다시 시도"
    # The next probe learns the reset (22:39): retry moves to 22:44 and the notice says so.
    clock.at = T0 + timedelta(hours=6)
    known["at"] = timestamp("2026-09-25T22:39:00+09:00")
    c = failing_chain()
    c({"q": 2})
    state = c._state["glm-5.3"]
    assert (
        state["retry_at"] == "2026-09-25T22:44:00+09:00"
        and state["retry_source"] == "quota_reset"
        and state["quota_reset_at"] == "2026-09-25T22:39:00+09:00"
    )
    assert (
        c.notices[0]
        == "screen: glm-5.3 QUOTA · 초기화 09-25 22:39 뒤 09-25 22:44에 복귀 확인"
    )
    # Once set, the retry time stands even if the lookup later returns nothing: no earlier probe is scheduled.
    clock.at = T0 + timedelta(hours=8)
    known["at"] = None
    c = ModelChain(
        [Member("glm-5.3"), Member("codex:gpt-5.6-terra")],
        stage="screen",
        state_path=tmp_path / "model-chain.json",
        clock=clock,
        quota_reset=reset,
    )
    assert (
        c.down(c.members[0], clock.at)
        and c({"q": 3})["_model"] == "codex:gpt-5.6-terra"
        and len(c.members[0].calls) == 0
    )
    # A reset in the past or more than a week ahead is not trusted: flat 6 hours again.
    for bad in (T0 - timedelta(minutes=1), T0 + timedelta(days=8)):
        (tmp_path / "model-chain.json").unlink(missing_ok=True)
        known["at"] = bad
        clock.at = T0
        c = failing_chain()
        c({"q": 4})
        assert (
            c._state["glm-5.3"]["retry_source"] == "probe_interval"
            and c._state["glm-5.3"]["retry_at"] == "2026-09-25T16:00:00+09:00"
        )
    # TRANSIENT and AUTH never consult the reset lookup.
    known["at"] = timestamp("2026-09-25T22:39:00+09:00")
    for category, expected in (
        ("TRANSIENT", "2026-09-25T10:05:00+09:00"),
        ("AUTH", "2026-09-25T16:00:00+09:00"),
    ):
        (tmp_path / "model-chain.json").unlink(missing_ok=True)
        c = ModelChain(
            [Member("glm-5.3", [Failure(category)]), Member("codex:gpt-5.6-terra")],
            stage="screen",
            state_path=tmp_path / "model-chain.json",
            clock=Clock(T0),
            quota_reset=reset,
        )
        c({"q": 5})
        assert (
            c._state["glm-5.3"]["retry_at"] == expected
            and c._state["glm-5.3"]["retry_source"] == "probe_interval"
        )


def test_transient_backoff_is_5_then_15_minutes_then_probe_interval(tmp_path):
    clock = Clock(T0)
    a = Member(
        "glm-5.3", [Failure("TRANSIENT"), Failure("TRANSIENT"), Failure("TRANSIENT")]
    )
    b = Member("codex:gpt-5.6-terra")
    retries = []
    for minutes in (0, 5, 20):
        clock.at = T0 + timedelta(minutes=minutes)
        c = chain(tmp_path, clock, a, b)
        assert c({"q": minutes})["_model"] == "codex:gpt-5.6-terra"
        retries.append(c.summary()["waiting"]["glm-5.3"]["retry_at"])
    assert retries == [
        "2026-09-25T10:05:00+09:00",
        "2026-09-25T10:20:00+09:00",
        "2026-09-25T16:20:00+09:00",
    ]
    assert c.notices[0] == "screen: glm-5.3 TRANSIENT · 6시간 뒤 다시 시도"


@pytest.mark.parametrize(
    "error",
    [Failure("REQUEST"), Failure("EXECUTOR_UNKNOWN"), ValueError("invalid response")],
)
def test_bad_request_unknown_failure_or_bad_answer_never_falls_back(tmp_path, error):
    a, b = Member("glm-5.3", [error]), Member("codex:gpt-5.6-terra")
    c = chain(tmp_path, Clock(T0), a, b)
    with pytest.raises(type(error)):
        c({"q": 1})
    assert (
        b.calls == []
        and c.notices == []
        and c.events == []
        and c.summary()["waiting"] == {}
    )
    assert not (tmp_path / "model-chain.json").exists()


def test_all_members_down_raises_the_preferred_category_for_model_wait(tmp_path):
    a, b = (
        Member("glm-5.3", [Failure("QUOTA")]),
        Member("codex:gpt-5.6-terra", [Failure("AUTH")]),
    )
    c = chain(tmp_path, Clock(T0), a, b)
    with pytest.raises(ChainExhausted) as info:
        c({"q": 1})
    assert info.value.category == "QUOTA" and failure_category(info.value) == "QUOTA"
    assert info.value.retry_at == T0 + timedelta(hours=6) and c.next_retry_at(
        T0
    ) == T0 + timedelta(hours=6)
    assert c.next_retry_at(T0 + timedelta(hours=6)) is None
    held = c.hold_state(T0 + timedelta(minutes=1))
    assert (
        held["held"] is True
        and held["category"] == "QUOTA"
        and held["retry_at"] == "2026-09-25T16:00:00+09:00"
        and held["model"] == c.model
    )
    assert c.active is a and c.model == "chain:screen[glm-5.3>codex:gpt-5.6-terra]"
    assert set(c.summary()["waiting"]) == {"glm-5.3", "codex:gpt-5.6-terra"}
    # Next pass inside the wait: no member is tried, the exhaustion is raised at once.
    c2 = chain(tmp_path, Clock(T0 + timedelta(minutes=1)), a, b)
    with pytest.raises(ChainExhausted):
        c2({"q": 2})
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_stages_alternate_failures_and_recoveries_in_one_state_file_without_clobbering(
    tmp_path,
):
    clock = Clock(T0)
    path = tmp_path / "model-chain.json"
    a1, a2 = (
        Member("glm-5.3", [Failure("QUOTA")]),
        Member("glm-5.3", [Failure("TRANSIENT")]),
    )
    screen = ModelChain(
        [a1, Member("codex:gpt-5.6-terra")],
        stage="screen",
        state_path=path,
        clock=clock,
    )
    first = ModelChain(
        [a2, Member("codex:gpt-5.6-terra")],
        stage="analyze-first",
        state_path=path,
        clock=clock,
    )
    screen({"q": 1})  # screen writes its glm wait
    first({"q": 1})  # analyze-first writes its own glm wait; screen's must survive
    payload = json.loads(path.read_text())
    assert (
        payload["screen"]["glm-5.3"]["category"] == "QUOTA"
        and payload["analyze-first"]["glm-5.3"]["category"] == "TRANSIENT"
    )
    clock.at = T0 + timedelta(
        minutes=5
    )  # analyze-first's glm recovers; screen's 6h wait stays
    first2 = ModelChain(
        [Member("glm-5.3"), Member("codex:gpt-5.6-terra")],
        stage="analyze-first",
        state_path=path,
        clock=clock,
    )
    assert first2.active.model == "codex:gpt-5.6-terra"  # retry due, not yet confirmed
    assert first2({"q": 2})["_model"] == "glm-5.3" and first2.notices == [
        "analyze-first: glm-5.3 복귀"
    ]
    payload = json.loads(path.read_text())
    assert (
        payload["analyze-first"] == {}
        and payload["screen"]["glm-5.3"]["category"] == "QUOTA"
    )
    screen2 = ModelChain(
        [Member("glm-5.3"), Member("codex:gpt-5.6-terra")],
        stage="screen",
        state_path=path,
        clock=clock,
    )
    assert screen2.active.model == "codex:gpt-5.6-terra"
