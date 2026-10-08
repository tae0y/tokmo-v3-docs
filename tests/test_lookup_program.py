import os
from datetime import date, datetime, timezone
from decimal import Decimal

from tokmo.facts.eia import HttpResponse
from tokmo.programs.lookup import format_yield_curve, main
from tokmo.values import YieldCurveFact

NOW = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)
EIA_BODY = b'{"response":{"data":[{"period":"2026-07-24","series":"RWTC","value":"78.420","units":"$/BBL"}]}}'
TREASURY_XML = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"><entry><content><m:properties><d:NEW_DATE>2026-07-24T00:00:00</d:NEW_DATE><d:BC_2YEAR>3.85</d:BC_2YEAR><d:BC_10YEAR>4.40</d:BC_10YEAR></m:properties></content></entry></feed>'''


def unreachable(*_args, **_kwargs):
    raise AssertionError("this dependency must not be touched")


def test_program_formats_fact_without_fact_returning_a_sentence():
    fact = YieldCurveFact("us_treasury_daily_par_yield_curve", "US", date(2026,7,24), Decimal("3.85"), Decimal("4.40"), datetime(2026,7,27,tzinfo=timezone.utc))
    assert format_yield_curve(fact) == "미국 국채금리 (2026-07-24): 2년 3.85% · 10년 4.40% · 스프레드 0.55%p"


def test_wti_lookup_uses_only_injected_transport_and_clock(capsys, monkeypatch):
    monkeypatch.setattr(os, "getenv", unreachable)
    monkeypatch.setattr(os.environ, "get", unreachable)

    code = main(
        ["wti"], api_key="fixture-key",
        eia_transport=lambda _request: HttpResponse(200, EIA_BODY, {}),
        treasury_transport=unreachable, clock=lambda: NOW,
    )

    assert code == 0
    assert "78.42" in capsys.readouterr().out


def test_wti_lookup_unavailable_returns_typed_exit_code(capsys):
    code = main(
        ["wti"], api_key=None,
        eia_transport=unreachable, treasury_transport=unreachable,
        clock=lambda: NOW,
    )

    assert code == 2
    assert "config_missing" in capsys.readouterr().out


def test_treasury_lookup_uses_injected_transport_and_clock(capsys, monkeypatch):
    monkeypatch.setattr(os, "getenv", unreachable)
    calls = []

    def transport(url, timeout):
        calls.append(url)
        return TREASURY_XML

    code = main(
        ["treasury"], api_key=None,
        eia_transport=unreachable, treasury_transport=transport,
        clock=lambda: NOW,
    )

    assert code == 0
    assert "field_tdr_date_value_month=202607" in calls[0]
    assert "2026-07-24" in capsys.readouterr().out


def test_unknown_source_returns_usage_error(capsys):
    code = main(
        ["mystery"], api_key=None,
        eia_transport=unreachable, treasury_transport=unreachable,
        clock=lambda: NOW,
    )

    assert code == 64
    assert "treasury | wti" in capsys.readouterr().err
