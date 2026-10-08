import importlib.resources

import pytest

from tokmo.facts.quant_membership import load_production_sic_snapshot


def test_official_sic_resources_are_conditionally_absent_before_capture():
    root = importlib.resources.files("tokmo.data")
    artifact = root.joinpath("sec_sic_html_v1.json")
    manifest = root.joinpath("sec_sic_html_v1.manifest.json")
    if not artifact.is_file() or not manifest.is_file():
        with pytest.raises(ValueError, match="production_sic_resource_missing"):
            load_production_sic_snapshot()
        pytest.skip("authorized official SIC capture/promotion has not occurred")
    snapshot = load_production_sic_snapshot()
    assert snapshot.classification == "OFFICIAL_CAPTURE"
    assert len(snapshot.codes) == 444
    assert snapshot.codes[:5] == ("0100", "0200", "0700", "0800", "0900")
    assert "3571" in snapshot.codes


def test_tokmo_data_python_package_always_exists_before_capture():
    root = importlib.resources.files("tokmo.data")
    assert root.joinpath("__init__.py").is_file()
