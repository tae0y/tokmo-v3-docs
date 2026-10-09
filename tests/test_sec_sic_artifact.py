import json
from datetime import UTC
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

TOOL = Path(__file__).parents[1] / "tools" / "build_sec_sic_artifact.py"


def load_tool():
    assert TOOL.is_file()
    spec = spec_from_file_location("build_sec_sic_artifact", TOOL)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sic_html_parser_is_sorted_unique_and_fail_closed():
    parse = load_tool().parse_sic_html
    assert parse(
        b'<table data-tokmo-authority="sec-sic"><tr><td scope="col"><strong>SIC Code</strong></td><td scope="col"><strong>Office</strong></td><td scope="col"><strong>Industry Title</strong></td></tr><tr><td>700</td><td>A</td><td>ALPHA</td></tr><tr><td>3571</td><td>B</td><td>BETA</td></tr></table>'
    ) == ("0700", "3571")
    for raw in (
        b"",
        b"<td>3571</td><td>3571</td>",
        b"<td>357A</td>",
        b"<div>3571</div>",
    ):
        with pytest.raises(ValueError):
            parse(raw)


def test_public_manifest_binds_official_source_without_private_relay_identifiers():
    tool = load_tool()
    source = b'<table data-tokmo-authority="sec-sic"><tr><th>SIC Code</th><th>Office</th><th>Industry Title</th></tr><tr><td>3571</td><td>A</td><td>ELECTRONIC COMPUTERS</td></tr></table>'
    capture = type(
        "Capture",
        (),
        {
            "body": source,
            "body_sha256": sha256(source).hexdigest(),
            "captured_at": __import__("datetime").datetime.fromisoformat(
                "2026-08-13T01:30:00+00:00"
            ),
            "status": 200,
            "body_byte_count": len(source),
            "capture_id": "capture-1",
        },
    )()

    class Store:
        def read(self, capture_id):
            assert capture_id == "capture-1"
            return capture

    _artifact, manifest_bytes = tool.build_from_capture_store(Store(), "capture-1")
    manifest = json.loads(manifest_bytes)

    assert manifest["official_source_sha256"] == sha256(source).hexdigest()
    assert "raw_body" not in TOOL.read_text()
    assert "response_body" not in TOOL.read_text()
    assert set(manifest) == {
        "artifact_sha256",
        "capture_instant",
        "classification",
        "extraction_contract_version",
        "official_source_sha256",
        "row_count",
        "source_row_count",
        "source",
    }


def test_arbitrary_bytes_and_capture_metadata_cannot_be_blessed_official():
    tool = load_tool()
    assert not hasattr(tool, "build")
    with pytest.raises(ValueError, match="sic_authority_table"):
        tool.parse_sic_html(b"<table><tr><td>3571</td></tr></table>")


def test_sic_parser_rejects_lookalike_or_ambiguous_authority_tables():
    parse = load_tool().parse_sic_html
    valid = b"<table><tr><td><strong>SIC Code</strong></td><td><strong>Office</strong></td><td><strong>Industry Title</strong></td></tr><tr><td>3571</td><td>A</td><td>B</td></tr></table>"
    with pytest.raises(ValueError, match="sic_authority_table"):
        parse(valid.replace(b"Industry Title", b"Industry"))
    with pytest.raises(ValueError, match="sic_authority_table"):
        parse(valid + valid)


@pytest.mark.parametrize(
    "bad_row",
    (
        b"<tr><td></td><td>A</td><td>B</td></tr>",
        b"<tr><td>3571</td><td></td><td>B</td></tr>",
        b"<tr><td>3571</td><td>A</td><td></td></tr>",
        b"<tr><td>3571</td><td>A</td><td>B</td><td>1234</td></tr>",
    ),
)
def test_sic_parser_requires_exactly_one_code_from_every_data_row(bad_row):
    parse = load_tool().parse_sic_html
    header = b"<table><tr><th>SIC Code</th><th>Office</th><th>Industry Title</th></tr>"
    with pytest.raises(ValueError):
        parse(header + bad_row + b"</table>")


def test_sic_parser_rejects_unclosed_or_out_of_row_data_cells():
    parse = load_tool().parse_sic_html
    header = b"<table><tr><th>SIC Code</th><th>Office</th><th>Industry Title</th></tr>"
    valid = b"<tr><td>3571</td><td>A</td><td>B</td></tr>"
    with pytest.raises(ValueError):
        parse(header + valid + b"<tr><td>7372</td><td>A</td><td>B</td></table>")
    with pytest.raises(ValueError):
        parse(header + valid + b"<td>7372</td><td>A</td><td>B</td></table>")


def test_builder_main_writes_loader_compatible_manifest_name(tmp_path):
    from datetime import datetime

    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    tool = load_tool()
    source = b"<table><tr><th>SIC Code</th><th>Office</th><th>Industry Title</th></tr><tr><td>3571</td><td>A</td><td>B</td></tr></table>"
    store_path = tmp_path / "capture.sqlite3"
    store = OfficialMembershipCaptureStore(store_path)
    receipt = store.append_success(
        body=source, captured_at=datetime(2026, 8, 14, tzinfo=UTC)
    )
    output = tmp_path / "sec_sic_html_v1.json"
    assert tool.main([str(store_path), receipt.capture_id, str(output)]) is None
    assert output.is_file()
    assert output.with_suffix(".manifest.json").is_file()
    assert not output.with_name(output.name + ".manifest.json").exists()
