from datetime import datetime, timezone
from decimal import Decimal

from tokmo.facts.eia import get_wti, parse_wti_response
from tokmo.values import UnavailableFact


def test_missing_key_returns_typed_unavailable_without_transport():
    calls = []
    result = get_wti(api_key=None, transport=lambda *_: calls.append(True), clock=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc))
    assert isinstance(result, UnavailableFact)
    assert result.source == "eia_wti_rwtc"
    assert result.reason == "config_missing"
    assert calls == []


def test_parses_latest_wti_value_as_decimal():
    body = b'{"response":{"data":[{"period":"2026-07-24","series":"RWTC","value":"78.42","units":"$/BBL"}]}}'
    observed = datetime(2026, 7, 27, tzinfo=timezone.utc)
    fact = parse_wti_response(body, observed_at=observed)
    assert fact.series == "RWTC"
    assert fact.period.isoformat() == "2026-07-24"
    assert fact.usd_per_barrel == Decimal("78.42")
    assert fact.observed_at == observed
