from tokmo.values.quant_multifactor_core import MarketV1


def test_v01_closed_market_enum_is_pinned() -> None:
    assert tuple(item.value for item in MarketV1) == (
        "KR_KOSPI",
        "KR_KOSDAQ",
        "US_NYSE_NASDAQ",
    )
