"""NEWS poll manifest validation tests."""

import json
import os

import pytest

from tokmo.programs.news_poll_manifest import (
    load_news_poll_manifest,
    plan_news_group,
)
from tokmo.programs.news_poll_policy import NewsGroup


def write_manifest(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def valid_payload():
    return {
        "version": "news16-v1",
        "naver": {
            "enabled": False,
            "query": "SK hynix OR Hyundai Motor",
            "display": 10,
        },
        "finnhub-company": {"symbols": ["GOOGL", "NVDA", "MSFT", "TSLA"]},
    }


def test_manifest_loads_only_target_parameters_for_fixed_groups(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, valid_payload())

    result = load_news_poll_manifest(path)

    assert result.version == "news16-v1"
    assert result.naver_enabled is False
    assert result.naver_query == "SK hynix OR Hyundai Motor"
    assert result.naver_display == 10
    assert result.finnhub_symbols == ("GOOGL", "NVDA", "MSFT", "TSLA")


@pytest.mark.parametrize("mutation", ["google", "legacy"])
def test_manifest_rejects_google_key_and_legacy_version(tmp_path, mutation):
    path = tmp_path / "manifest.json"
    payload = valid_payload()
    if mutation == "google":
        payload["google-news-search"] = {"query": "GOOGL"}
    else:
        payload["version"] = "news15-v1"
    write_manifest(path, payload)

    with pytest.raises(ValueError, match="manifest_contract"):
        load_news_poll_manifest(path)


def test_manifest_rejects_missing_target_and_unknown_keys(tmp_path):
    path = tmp_path / "manifest.json"
    payload = valid_payload()
    del payload["naver"]
    payload["extra"] = {}
    write_manifest(path, payload)

    with pytest.raises(ValueError, match="manifest_contract"):
        load_news_poll_manifest(path)


def test_manifest_requires_private_regular_file(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, valid_payload())
    os.chmod(path, 0o644)

    with pytest.raises(ValueError, match="manifest_safety"):
        load_news_poll_manifest(path)


def test_group_plan_expands_only_finnhub_company_symbols(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, valid_payload())
    manifest = load_news_poll_manifest(path)

    plan = plan_news_group(NewsGroup.B, manifest, on_date="2026-08-02")

    assert [item.source for item in plan] == [
        "finnhub-company",
        "finnhub-company",
        "finnhub-company",
        "finnhub-company",
        "jtbc-breaking",
        "investing-analyst",
    ]
    assert [item.symbol for item in plan[:4]] == ["GOOGL", "NVDA", "MSFT", "TSLA"]
    assert all(item.from_date == "2026-08-01" for item in plan[:4])
    assert all(item.to_date == "2026-08-02" for item in plan[:4])


def test_group_plan_keeps_google_out_of_recurring_invocations(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, valid_payload())
    manifest = load_news_poll_manifest(path)

    a = plan_news_group(NewsGroup.A, manifest, on_date="2026-08-02")
    c = plan_news_group(NewsGroup.C, manifest, on_date="2026-08-02")

    assert [item.source for item in a] == ["bbc", "yna-market"]
    assert [item.source for item in c] == ["finnhub-market", "yna-industry"]


def test_group_plan_injects_enabled_naver_parameters(tmp_path):
    path = tmp_path / "manifest.json"
    payload = valid_payload()
    payload["naver"]["enabled"] = True
    write_manifest(path, payload)
    manifest = load_news_poll_manifest(path)

    plan = plan_news_group(NewsGroup.A, manifest, on_date="2026-08-02")

    assert [item.source for item in plan] == ["bbc", "yna-market", "naver"]
    assert plan[-1].query == manifest.naver_query
    assert plan[-1].display == manifest.naver_display


def test_group_plan_omits_provider_deferred_naver(tmp_path):
    path = tmp_path / "manifest.json"
    payload = valid_payload()
    payload["naver"]["enabled"] = False
    write_manifest(path, payload)
    manifest = load_news_poll_manifest(path)

    plan = plan_news_group(NewsGroup.A, manifest, on_date="2026-08-02")

    assert manifest.naver_enabled is False
    assert [item.source for item in plan] == ["bbc", "yna-market"]
