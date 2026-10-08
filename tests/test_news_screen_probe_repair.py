"""Frozen public-source probe parity; no live IO or canonical-store opening."""
import importlib.util
import json
import pytest
from pathlib import Path

from tokmo.programs.news_decision_engine import model_evidence

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / '.ai/evidence/news-screen-linkage-20260929'


def test_probe_preserves_all_nine_public_source_payloads():
    data = json.loads((BASE / 'semantic-repair/cases.json').read_text())
    sources = json.loads((BASE / 'semantic-repair/public-sources.json').read_text())
    items = [c['input'] for c in data['cases']] + [r['source'] for r in data['catalog']]
    assert len(items) == len({r['id'] for r in items}) == len(sources) == 9
    for item in items:
        assert item == sources[item['id']], f"metadata lost: {item['id']}"
    filing = next(item for item in items if item['id'].startswith('dart:'))
    view = model_evidence([filing], text_limit=700)[0]
    assert view['body_verified'] is False
    assert view['content_scope'] == 'listing_only'
    assert view['stock_code'] == '307950'


def test_repair_captures_existing_correction_without_live_calls():
    path = BASE / 'semantic-repair/probe.py'
    spec = importlib.util.spec_from_file_location('screen_repair_probe', path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    legacy, request, evidence, events = probe.apple_replay_inputs()
    correction = probe.capture_correction(evidence, events, legacy)
    assert correction['correction']['previous_response']['invalid_model_reason'] == 'invalid_json'
    assert correction['correction']['previous_response']['invalid_model_response'] == legacy['invalid_model_response']
    assert correction['correction']['allowed_evidence_ids'] == [evidence['id']]
    assert len(correction['correction']['allowed_event_ids']) == 4
    assert correction['context'] == {}
    assert correction['correction']['reason'] == 'model output: invalid_json'


@pytest.mark.parametrize('example_kind', ['first_response', 'correction'])
def test_ignored_examples_preserve_the_existing_linkage_field(example_kind):
    import news_decision_models as models
    expected = set(models.response_schema('screen')['properties']['ignored']['items']['required'])
    if example_kind == 'first_response':
        shape, _ = json.JSONDecoder().raw_decode(models.SCREEN[models.SCREEN.index('{'):])
    else:
        spec = importlib.util.spec_from_file_location('screen_repair_probe', BASE/'semantic-repair/probe.py')
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        previous, _, evidence, events = probe.apple_replay_inputs()
        shape = probe.capture_correction(evidence, events, previous)['correction']['response_shape']
    assert set(shape['ignored'][0]) == expected
    assert shape['ignored'][0]['event_id'] is None
