import json

import pytest

from tokmo.values.model_registry import (
    ModelRegistryError,
    ModelSpec,
    codex_catalog,
    known_models,
    load_registry,
    news_chains,
    parse_chain,
    parse_spec,
    unknown_models,
)


def test_spec_forms_and_names():
    assert parse_spec("glm-5.3") == ModelSpec("zai", "glm-5.3", None, None)
    assert parse_spec("codex:gpt-5.6-terra@low") == ModelSpec(
        "codex", "gpt-5.6-terra", "low", None
    )
    assert parse_spec("claude:claude-haiku-4-5-20251001") == ModelSpec(
        "claude", "claude-haiku-4-5-20251001", None, None
    )
    assert (
        parse_spec({"model": "codex:gpt-5.6-sol", "transport": "direct"}).transport
        == "direct"
    )
    obj = parse_spec(
        {"model": "codex:gpt-5.6-sol", "effort": "high", "transport": "hermes"}
    )
    assert obj == ModelSpec("codex", "gpt-5.6-sol", "high", "hermes")
    assert (
        parse_spec("codex:gpt-5.6-sol").name == "codex:gpt-5.6-sol"
        and parse_spec("glm-5.3").name == "glm-5.3"
    )
    assert str(parse_spec("codex:gpt-5.6-terra@low")) == "codex:gpt-5.6-terra@low"
    assert parse_spec(obj) is obj


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "openai:gpt-5",
        "codex:GPT-5.6",
        "glm-5.3@low",
        "codex:gpt-5.6-terra@fast",
        "codex:gpt-5.6-terra@default",
        "latest",
        {"model": "glm-5.3", "transport": "hermes"},
        {"model": "glm-5.3", "extra": 1},
        {"model": "codex:x@low", "effort": "high"},
    ],
)
def test_invalid_specs_are_rejected(bad):
    with pytest.raises(ModelRegistryError):
        parse_spec(bad)


def test_chain_is_ordered_and_unique():
    chain = parse_chain(["glm-5.3", "codex:gpt-5.6-terra@low"])
    assert [s.name for s in chain] == ["glm-5.3", "codex:gpt-5.6-terra"]
    assert parse_chain("glm-5.3") == (ModelSpec("zai", "glm-5.3"),)
    with pytest.raises(ModelRegistryError):
        parse_chain([])
    with pytest.raises(ModelRegistryError):
        parse_chain(["glm-5.3", "zai:glm-5.3"])


def test_registry_file_and_precedence_over_legacy_keys(tmp_path):
    path = tmp_path / "models.json"
    assert load_registry(path) == {}
    path.write_text(
        json.dumps(
            {
                "schema": "tokmo-models-v1",
                "_comment": "ignored",
                "news": {
                    "screen": ["glm-5.3", "codex:gpt-5.6-terra@low"],
                    "confirm": "codex:gpt-5.6-sol@high",
                },
            }
        )
    )
    registry = load_registry(path)
    assert [s.name for s in registry["news"]["screen"]] == [
        "glm-5.3",
        "codex:gpt-5.6-terra",
    ]
    legacy = {
        "screen_model": "glm-5.3-flash",
        "analysis_model": "glm-5.3",
        "first_pass_model": "glm-5.3",
    }
    chains = news_chains(registry, legacy)
    assert [s.name for s in chains["screen"]] == [
        "glm-5.3",
        "codex:gpt-5.6-terra",
    ]  # registry wins
    assert [s.name for s in chains["confirm"]] == ["codex:gpt-5.6-sol"] and chains[
        "confirm"
    ][0].effort == "high"
    assert [s.name for s in chains["first_pass"]] == [
        "glm-5.3"
    ]  # legacy key still applies
    assert (
        news_chains({}, {})["first_pass"] is None
        and news_chains({}, {})["screen"][0].name == "glm-5.3"
    )
    assert [
        s.name
        for s in news_chains({}, {"screen_model": ["glm-5.3", "codex:gpt-5.6-terra"]})[
            "screen"
        ]
    ] == ["glm-5.3", "codex:gpt-5.6-terra"]


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        {"schema": "other"},
        {"schema": "tokmo-models-v1", "macro": {}},
        {"schema": "tokmo-models-v1", "news": {"analysis": "glm-5.3"}},
        {"schema": "tokmo-models-v1", "news": ["glm-5.3"]},
        {"schema": "tokmo-models-v1", "news": {"screen": "gpt-5.6-terra@low@low"}},
    ],
)
def test_invalid_registry_files_fail_at_load(tmp_path, payload):
    path = tmp_path / "models.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    with pytest.raises(ModelRegistryError):
        load_registry(path)


def test_codex_catalog_comes_from_the_cli_cache_and_hidden_models_are_excluded(
    tmp_path,
):
    assert codex_catalog(tmp_path) is None
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-6-astra", "visibility": "list"},
                    {"slug": "gpt-reserve", "visibility": "hide"},
                    {"slug": "gpt-5.6-terra", "visibility": "list"},
                ]
            }
        )
    )
    assert codex_catalog(tmp_path) == ("gpt-6-astra", "gpt-5.6-terra")
    catalog = known_models(tmp_path)
    assert (
        catalog["codex"] == ("gpt-6-astra", "gpt-5.6-terra")
        and "glm-5.3" in catalog["zai"]
    )
    chain = parse_chain(["glm-5.3", "codex:gpt-5.3-codex-spark", "codex:gpt-5.6-terra"])
    assert [s.name for s in unknown_models(chain, catalog)] == [
        "codex:gpt-5.3-codex-spark"
    ]
    (tmp_path / "models_cache.json").write_text('{"models": "broken"}')
    assert (
        known_models(tmp_path)["codex"] == known_models()["codex"]
    )  # unreadable cache -> built-in list
    # A readable catalog with nothing visible is a real answer: no codex id is usable, none falls back to the built-in list.
    (tmp_path / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "gpt-5.6-terra", "visibility": "hide"}]})
    )
    assert codex_catalog(tmp_path) == () and known_models(tmp_path)["codex"] == ()
    assert [
        s.name
        for s in unknown_models(
            parse_chain(["glm-5.3", "codex:gpt-5.6-terra"]), known_models(tmp_path)
        )
    ] == ["codex:gpt-5.6-terra"]
