"""Common calculations/validation and selected-model execution, without live I/O."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest
from test_quant_decisions import response, sample_input
from test_quant_repeat_latency import NOW, fake_adapter

from tokmo.programs.quant_decision_analysis import (
    build_report,
    make_packet,
    validate_analysis,
)
from tokmo.programs.quant_dialogue import (
    complete_dialogue,
    prepare_dialogue,
    report_pages,
)
from tokmo.values.quant_decisions import parse_snapshot


def case():
    data = sample_input()
    data["positions"][0]["quantity"] = "67"
    data["positions"][0]["sellable"] = "67"
    data["cash"] = []
    data["instruments"][0]["price"] = "17590"
    data["analysis_requests"] = [
        dict(
            key="KR:000001",
            account="personal-a",
            scope="personal",
            quantity="100",
            budget="1800000",
            side="NEW_BUY",
            currency="KRW",
        )
    ]
    s = parse_snapshot(data)
    return s, make_packet(s, s.instruments[0], "personal-a", "personal")


def test_new_quantity_is_not_old_holding_or_buying_power():
    _, packet = case()
    assert packet["holdings"][0]["quantity"] == "67"
    assert packet["hypothesis"]["entry_amount"] == "1759000"
    assert packet["hypothesis"]["quantity"] == "100"
    assert packet["hypothesis"]["execution_eligible"] is False
    assert not [o for o in packet["options"] if o["action"] == "BUY"]
    for text in (
        "약180만원의 가상 예산에서 약176만원이 필요해.",
        "약 180만원 예산으로 신규100주를 비교해.",
    ):
        answer = response(packet)
        answer["summary"] = text
        assert validate_analysis(answer, packet)["summary"] == text


@pytest.mark.parametrize(
    "text",
    ["신규101주를 사려면 돼.", "예산은 180만달러야.", "가상 대금은 190만원이야."],
)
def test_wrong_units_and_invented_amounts_still_fail(text):
    _, packet = case()
    answer = response(packet)
    answer["summary"] = text
    with pytest.raises(ValueError, match="unsupported monetary/share"):
        validate_analysis(answer, packet)


def test_proposal_arithmetic_can_be_validated_again_without_becoming_order():
    _, packet = case()
    answer = response(packet)
    answer["price_proposal"] = dict(
        quantity="100",
        entry_price="17450",
        exit_price="17850",
        reason="확인된 가격 범위 안에서 진입을 비교해.",
        condition="추세 유지 시 가정이야.",
    )
    answer["summary"] = "174만5천원 대신 대금 1745000원과 비용 전 차익 4만원을 비교해."
    validated = validate_analysis(answer, packet)
    assert validated["price_proposal"]["gross_profit"] == "40000"
    assert validated["price_proposal"]["budget"] == "1800000"
    assert (
        Decimal(validated["price_proposal"]["average_cost_after"])
        == (Decimal(67) * 7000 + Decimal(100) * 17450) / 167
    )
    assert validated["price_proposal"]["holding_after"] == "167"
    assert validated["price_proposal"]["cost_basis_before"] == "469000"
    assert "new_money_fraction_of_confirmed_assets" in validated["price_proposal"]
    assert validated["option_id"] is None
    assert validate_analysis(validated, packet) == validated
    for change in ({"quantity": "101"}, {"side": "SELL"}, {"budget": "2000000"}):
        bad = deepcopy(answer)
        bad["price_proposal"].update(change)
        with pytest.raises(ValueError):
            validate_analysis(bad, packet)


def test_explicit_moving_average_price_is_money_but_gap_is_not():
    _, packet = case()
    packet["price_context"]["moving_averages"] = {
        "20": dict(value="17447.25", latest_quote_gap="0.12345")
    }
    answer = response(packet)
    answer["summary"] = "20일선은 약17,447원이야."
    assert validate_analysis(answer, packet)["summary"] == answer["summary"]
    answer["summary"] = "17447달러야."
    with pytest.raises(ValueError):
        validate_analysis(answer, packet)


@pytest.mark.parametrize(
    "change",
    [
        {"key": "KR:999999"},
        {"account": "unknown"},
        {"scope": "retirement"},
        {"quantity": True},
        {"budget": "NaN"},
        {"currency": "USD"},
    ],
)
def test_bad_request_fails_before_analysis(change):
    data = sample_input()
    data["analysis_requests"] = [
        dict(key="KR:000001", account="personal-a", scope="personal", quantity="100")
    ]
    data["analysis_requests"][0].update(change)
    with pytest.raises(ValueError):
        parse_snapshot(data)


def dialogue_answer(report):
    prepared = prepare_dialogue(report)
    return dict(
        report_id=report["report_id"],
        author_model="codex:gpt-6-astra",
        responses={
            r["identity"]: dict(
                packet_sha256=r["packet_sha256"], analysis=response(r["packet"])
            )
            for r in prepared["rows"]
        },
    )


def test_dialogue_and_automatic_analysis_share_calculations_and_validation():
    snapshot, _ = case()
    prepared = build_report(snapshot, new_limit=0)
    original = deepcopy(prepared)
    answer = dialogue_answer(prepared)
    completed = complete_dialogue(
        prepared,
        answer,
        author_model=answer["author_model"],
        completed_at=NOW.isoformat(),
    )
    automatic = build_report(snapshot, new_limit=0, model=lambda _: response({}))
    row = completed["results"][0]
    assert row["analysis"] == automatic["results"][0]["analysis"]
    assert row["packet"] == automatic["results"][0]["packet"]
    assert row["status"] == "ANALYZED" and row["analysis_source"] == "dialogue"
    assert row["analysis_origin"]["source"] == "current_conversation_declared"
    assert completed["dialogue"]["additional_model_calls"] == 0
    assert prepared == original and completed["report_id"] != prepared["report_id"]
    bad = deepcopy(answer)
    next(iter(bad["responses"].values()))["analysis"]["summary"] = (
        "새 돈 9999999원을 넣자."
    )
    assert (
        complete_dialogue(
            prepared,
            bad,
            author_model=answer["author_model"],
            completed_at=NOW.isoformat(),
        )["results"][0]["status"]
        == "FAILED"
    )
    bad = deepcopy(answer)
    next(iter(bad["responses"].values()))["packet_sha256"] = "bad"
    with pytest.raises(ValueError, match="packet mismatch"):
        complete_dialogue(
            prepared,
            bad,
            author_model=answer["author_model"],
            completed_at=NOW.isoformat(),
        )


def test_dialogue_reserves_real_budget_and_rejects_double_completion():
    prepared = build_report(parse_snapshot(sample_input()), new_limit=0)
    answer = dialogue_answer(prepared)
    row = prepared["results"][0]
    option = next(o for o in row["packet"]["options"] if o["action"] == "BUY")
    answer["responses"][row["identity"]]["analysis"] = response(
        row["packet"], "ADD", option
    )
    completed = complete_dialogue(
        prepared,
        answer,
        author_model=answer["author_model"],
        completed_at=NOW.isoformat(),
    )
    actual = next(
        r
        for r in completed["allocation"]
        if r["scenario"] == "cash" and r["currency"] == "KRW"
    )
    assert Decimal(actual["remaining_after_buys"]) == Decimal(2000000) + Decimal(
        option["cash_delta"]
    )
    with pytest.raises(ValueError, match="unanalysed"):
        complete_dialogue(
            completed,
            answer,
            author_model=answer["author_model"],
            completed_at=NOW.isoformat(),
        )


@pytest.mark.parametrize("author", ["codex:gpt-6-astra", "openai-codex/gpt-6-astra"])
def test_complete_cli_never_refreshes_live_inputs_or_calls_model(
    monkeypatch, tmp_path, capsys, author
):
    import quant_decision_models as models
    from run_quant_decisions import main

    import tokmo.kis_composition as kis

    def unexpected(*a, **kw):
        pytest.fail("completion attempted external input/model")

    monkeypatch.setattr(kis, "refresh_kis_etf_inputs", unexpected)
    monkeypatch.setattr(models, "QuantModel", unexpected)
    prepared = build_report(parse_snapshot(sample_input()), new_limit=0)
    path = tmp_path / "prepared.json"
    path.write_text(json.dumps(prepared))
    answer = dialogue_answer(prepared)
    answer["author_model"] = author
    response_file = tmp_path / "answer.json"
    response_file.write_text(json.dumps(answer))
    output = tmp_path / "output"
    assert (
        main(
            [
                "--complete-report",
                str(path),
                "--responses",
                str(response_file),
                "--author-model",
                answer["author_model"],
                "--output",
                str(output),
            ]
        )
        == 0
    )
    reports = [
        json.loads(p.read_text())
        for p in output.glob("qd-*.json")
        if "schema_version" in json.loads(p.read_text())
    ]
    assert len(reports) == 1 and reports[0]["dialogue"]["additional_model_calls"] == 0
    assert reports[0]["dialogue"]["author_model"] == author
    assert not list(output.glob("model-receipts/*"))


@pytest.mark.parametrize(
    "provider,model,labels",
    [
        ("codex", "codex:gpt-6-astra", ["Session"]),
        (
            "claude",
            "claude:claude-fable-5-1",
            ["Current session", "Current week", "Fable week"],
        ),
    ],
)
def test_selected_subscription_model_has_no_glm_first_call(
    monkeypatch, tmp_path, provider, model, labels
):
    import quant_decision_models as models
    import subscription_json_model as subscription

    base, physical, quota, route = fake_adapter(monkeypatch, tmp_path)
    q = json.loads(quota.read_text())
    q["providers"][provider] = dict(
        ok=True, windows=[dict(label=l, remaining=50) for l in labels]
    )
    quota.write_text(json.dumps(q))
    called = []

    def infer(prompt, **kwargs):
        called.append(kwargs)
        return "{}", dict(_usage={"total_tokens": 7}, _model=kwargs["model"])

    monkeypatch.setattr(subscription, "infer", infer)
    selected = models.QuantModel(
        env_file=tmp_path / "unused",
        model=model,
        on_demand=True,
        receipt_dir=tmp_path / "receipts",
    )
    assert selected("synthetic") == "{}"
    assert not physical and len(called) == 1 and called[0]["provider"] == provider
    assert selected.calls[0]["transport_started"] is True
    assert selected.calls[0]["resolved_model"] == model.split(":", 1)[1]


@pytest.mark.parametrize("finish,content", [("length", "{}"), ("stop", "")])
def test_glm_empty_or_truncated_response_is_failure(
    monkeypatch, tmp_path, finish, content
):
    model, physical, _, _ = fake_adapter(monkeypatch, tmp_path)

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(
                {
                    "choices": [
                        {"finish_reason": finish, "message": {"content": content}}
                    ]
                }
            ).encode()

    monkeypatch.setattr(model.opener, "open", lambda *a, **kw: Reply())
    with pytest.raises(ValueError):
        model("synthetic")
    assert len(model.calls) == 1 and model.calls[0]["status"] == "FAILED"


def test_plain_pages_keep_emoji_conditions_and_links_without_loss(monkeypatch):
    from tokmo.programs import quant_dialogue

    text = ("가나다😀 확인 조건 https://example.invalid/test\n" * 100).rstrip("\n")
    monkeypatch.setattr(quant_dialogue, "render_report", lambda _: text)
    pages = report_pages({})
    assert len(pages) == 2 and "\n".join(pages) == text
    assert all(len(p.encode("utf-16-le")) // 2 <= 3800 for p in pages)
    with pytest.raises(ValueError, match="QUANT_DISPLAY_TOO_LONG"):
        report_pages({}, limit=300)


def test_claude_single_inference_disables_tools_and_preserves_cache_usage(monkeypatch):
    import subscription_json_model as subscription

    seen = []

    def run(argv, *args):
        seen.append(argv)
        return json.dumps(
            dict(
                type="result",
                subtype="success",
                is_error=False,
                result="{}",
                usage=dict(
                    input_tokens=10,
                    cache_read_input_tokens=20,
                    cache_creation_input_tokens=5,
                    output_tokens=7,
                ),
            )
        )

    monkeypatch.setattr(subscription, "_run", run)
    text, meta = subscription.infer(
        "synthetic", provider="claude", model="claude-fable-5-1", timeout=120
    )
    assert text == "{}" and meta["_usage"]["total_tokens"] == 42
    assert meta["_usage"]["prompt_tokens_details"]["cached_tokens"] == 20
    argv = seen[0]
    assert (
        argv[argv.index("--tools") + 1] == ""
        and argv[argv.index("--max-turns") + 1] == "1"
    )
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
