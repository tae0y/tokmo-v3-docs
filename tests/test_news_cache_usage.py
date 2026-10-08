import json

from report_news_decisions import usage_by_model


def test_partial_cache_reporting_does_not_claim_full_coverage_or_charge_twice():
    def row(usage, status='OK'):
        return dict(stage='analyze', status=status, tokens=9000,
            response=json.dumps({'_model':'astra', '_usage':usage}))
    usage = dict(prompt_tokens=100, completion_tokens=10, total_tokens=110)
    rows = [row(usage | {'prompt_tokens_details':{'cached_tokens':80}}), row(usage),
            row(usage | {'prompt_tokens_details':{'cached_tokens':0}}), row({}, 'FAILED')]
    value = usage_by_model(rows)[0]
    assert value['calls'] == 4 and value['usage_reported_calls'] == 3
    assert value['total_tokens'] == 330 and value['input_tokens'] == 300
    assert value['cache_reported_calls'] == 2 and value['cache_unknown_calls'] == 2
    assert value['cache_measured_input_tokens'] == 200 and value['cached_input_tokens'] == 80
    assert value['uncached_input_tokens_measured'] == 120 and value['cache_hit_ratio_measured'] == .4
