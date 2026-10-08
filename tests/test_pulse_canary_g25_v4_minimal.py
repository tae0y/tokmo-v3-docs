"""Minimal user-approved v4 live verification contract.

Reuses the existing sandbox wrapper and runner.  The immutable v1-v3 ledgers
contribute 22 starts; one additional 5-tick TOKEN+PRICE_BATCH run contributes
10, for an exact family cap of 32.  No retry, daemon, cron, or extra harness.
"""

from tokmo.programs import pulse_canary as pc


def test_v4_is_one_minimal_additional_run():
    assert pc.CANARY_CAMPAIGN_ID == "g25-quote-kr-005930-v4"
    assert pc.CANARY_CAMPAIGN_VERSION == 4
    assert pc.CANARY_FAMILY_CUMULATIVE_CAP == 32
    assert pc.CANARY_MAX_PROVIDER_STARTS_LIMIT == 32
    assert pc.CANARY_MAX_PROVIDER_STARTS_DEFAULT == 32
    assert tuple((x.campaign_id, x.version) for x in pc._PRIOR_CAMPAIGN_SPECS) == (
        ("g25-quote-kr-005930-v1", 1),
        ("g25-quote-kr-005930-v2", 2),
        ("g25-quote-kr-005930-v3", 3),
    )


def test_v4_full_plan_fits_exactly_after_inherited_22():
    cfg = pc.CanaryConfigV1(
        market=pc.CANARY_CAMPAIGN_MARKET,
        symbol=pc.CANARY_CAMPAIGN_SYMBOL,
        max_ticks=5,
        max_provider_starts=32,
    )
    assert 22 + cfg.max_ticks * 2 == cfg.max_provider_starts
