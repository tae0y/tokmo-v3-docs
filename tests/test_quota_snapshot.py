from datetime import timedelta
import json

from tokmo.programs.quota_snapshot import quota_reset_reader
from tokmo.records.news_decision_store import timestamp

AT = timestamp('2026-09-25T19:38:00+09:00')


def snapshot(path, observed='2026-09-25T09:00:47+00:00', **providers):
    path.write_text(json.dumps({'schema': 'model-quota-latest-v1', 'observed_at': observed, 'providers': providers}))


def test_reset_is_the_earliest_future_reset_of_an_exhausted_window(tmp_path):
    path = tmp_path / 'model-quota-latest.json'
    # The live shape on 2026-09-25: the 5h window is full, the weekly window is what blocks us (UTC instants;
    # Z.AI's own Hong Kong clock never enters the comparison).
    snapshot(path, zai={'ok': True, 'windows': [{'label': '5시간', 'remaining': 100.0, 'reset_at': None},
                                               {'label': '주간', 'remaining': 0.0, 'reset_at': '2026-09-26T13:39:00.983000+00:00'},
                                               {'label': '월간 MCP', 'remaining': 91.0, 'reset_at': '2026-09-25T13:39:00+00:00'}]},
                  codex={'ok': True, 'windows': [{'label': 'Session', 'remaining': 0.0, 'reset_at': '2026-09-25T10:00:00+00:00'}]})
    reset = quota_reset_reader(path)
    assert reset('zai', AT) == timestamp('2026-09-26T22:39:00.983000+09:00')
    assert reset('codex', AT) is None          # its only exhausted window already reset (past instant)
    assert reset('claude', AT) is None         # provider absent


def test_unknown_when_nothing_is_exhausted_or_the_file_is_missing_stale_or_broken(tmp_path):
    path = tmp_path / 'model-quota-latest.json'
    reset = quota_reset_reader(path)
    assert reset('zai', AT) is None                                        # no file
    snapshot(path, zai={'ok': True, 'windows': [{'label': '주간', 'remaining': 37.0, 'reset_at': '2026-09-26T13:39:00+00:00'}]})
    assert reset('zai', AT) is None                                        # nothing exhausted -> the block is not explained
    snapshot(path, observed='2026-09-24T09:00:00+00:00', zai={'ok': True, 'windows': [{'label': '주간', 'remaining': 0.0, 'reset_at': '2026-09-26T13:39:00+00:00'}]})
    assert reset('zai', AT) is None                                        # observed more than 24h ago
    snapshot(path, observed='2026-09-25T09:00:00+00:00', zai={'ok': True, 'windows': [{'label': '주간', 'remaining': 0.0, 'reset_at': 'soon'}]})
    assert reset('zai', AT) is None                                        # unparseable reset
    path.write_text('{')
    assert reset('zai', AT) is None                                        # broken json
    snapshot(path, zai={'ok': True, 'windows': 'none'})
    assert reset('zai', AT) is None


def test_a_fresher_snapshot_is_picked_up_by_the_next_call(tmp_path):
    path = tmp_path / 'model-quota-latest.json'
    snapshot(path, zai={'ok': True, 'windows': [{'label': '주간', 'remaining': 0.0, 'reset_at': None}]})
    reset = quota_reset_reader(path)
    assert reset('zai', AT) is None
    snapshot(path, zai={'ok': True, 'windows': [{'label': '주간', 'remaining': 0.0, 'reset_at': '2026-09-26T13:39:00+00:00'}]})
    assert reset('zai', AT + timedelta(hours=6)) == timestamp('2026-09-26T13:39:00+00:00')
