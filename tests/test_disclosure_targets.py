"""Typed recurring disclosure target contracts."""

import pytest

from tokmo.values.disclosure_targets import (
    DartDisclosureTarget, DisclosureProvider, SecDisclosureTarget,
    parse_disclosure_targets,
)


def test_disclosure_targets_are_provider_typed_and_not_hardcoded():
    targets = parse_disclosure_targets({
        "version": "disclosure30-v1",
        "targets": [
            {"provider": "SEC", "symbol": "AAPL", "cik": "0000320193"},
            {"provider": "OPENDART", "symbol": "000660", "corp_code": "00164779"},
        ],
    })

    assert targets == (
        SecDisclosureTarget(DisclosureProvider.SEC, "AAPL", "0000320193"),
        DartDisclosureTarget(DisclosureProvider.OPENDART, "000660", "00164779"),
    )


@pytest.mark.parametrize("payload", [
    {"version": "disclosure30-v1", "targets": []},
    {"version": "disclosure30-v1", "targets": [{"provider": "SEC", "symbol": "000660"}]},
    {"version": "disclosure30-v1", "targets": [{"provider": "OPENDART", "symbol": "AAPL"}]},
    {"version": "disclosure30-v1", "targets": [{"provider": "SEC", "symbol": "AAPL", "corp_code": "00164779"}]},
])
def test_disclosure_targets_fail_closed_on_empty_malformed_or_provider_mismatch(payload):
    with pytest.raises(ValueError, match="disclosure_target_contract"):
        parse_disclosure_targets(payload)
