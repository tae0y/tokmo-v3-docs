"""Local, best-effort NEWS diagnostics. Never part of prompts or decision fingerprints."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from tokmo.records.news_decision_store import encoded

SCHEMA = 1
BASELINE_KEY = '_usage_diagnostic_baseline'
_ACTIVE = ContextVar('news_usage_call', default=None)
MAX_LEAVES = 1024
MAX_DIFFS = 40
DETAILED_ROWS = 8


def size(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return dict(chars=len(text), utf8_bytes=len(text.encode('utf-8')))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def usage_view(value):
    if not isinstance(value, dict):
        return None
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    if not all(type(value.get(k)) is int and value[k] >= 0 for k in keys):
        return None
    p, o, t = (value[k] for k in keys)
    if p + o != t:
        return None
    result = dict(zip(keys, (p, o, t)))
    input_details = value.get('prompt_tokens_details')
    output_details = value.get('completion_tokens_details')
    cached = input_details.get('cached_tokens') if isinstance(input_details, dict) else None
    if cached is None:
        cached = value.get('cached_input_tokens', value.get('prompt_cache_hit_tokens'))
    reasoning = output_details.get('reasoning_tokens') if isinstance(output_details, dict) else None
    if reasoning is None:
        reasoning = value.get('reasoning_tokens')
    if type(cached) is int and 0 <= cached <= p:
        result['cached_input_tokens'] = cached
    if type(reasoning) is int and 0 <= reasoning <= o:
        result['reasoning_tokens'] = reasoning
    return result


def comparison_context(event, context, scoped):
    from tokmo.programs.news_context_efficiency import context_basis
    if scoped:
        from tokmo.programs.news_incremental_review import dependency_context
        return dependency_context(event, context)
    result = context_basis(context)
    if isinstance(result.get('investment_followup'), dict):
        result['investment_followup'].pop('delivered_reviews', None)
    return result


def baseline(event, context, scoped):
    """Hash only; identity labels never contain raw account identifiers or values."""
    value = comparison_context(event, context, scoped)
    leaves = {}
    grouped = []
    omitted = 0

    def walk(item, path):
        nonlocal omitted
        if len(leaves) >= MAX_LEAVES:
            omitted += 1
            return
        if isinstance(item, dict) and item:
            for key in sorted(item):
                walk(item[key], path + '/' + key.replace('~', '~0').replace('/', '~1'))
        elif isinstance(item, list) and item:
            # Sorting follows the existing context_basis policy. Stable identities
            # let a quantity change remain a quantity change, not a list replacement.
            ordered = sorted(item, key=encoded)
            if len(ordered) > DETAILED_ROWS:
                # Large tables (e.g. ETF constituents) retain field-level changes,
                # rather than one long path and hash per cell. Original rows remain
                # in the existing request ledger if an exact row must be inspected.
                grouped.append(path)
                if all(isinstance(row, dict) for row in ordered):
                    def fields(row, prefix=''):
                        if isinstance(row, dict) and row:
                            for key in sorted(row):
                                yield from fields(row[key], prefix+'/'+key.replace('~','~0').replace('/','~1'))
                        else:
                            yield prefix, row
                    columns = {}
                    for row in ordered:
                        identity = {k:row[k] for k in ('id','key','symbol','code','account','scope') if k in row}
                        for key, value in fields(row):
                            columns.setdefault(key, []).append([identity, value])
                    for key, values in sorted(columns.items()):
                        field = path+'/*'+key
                        if len(leaves) < MAX_LEAVES:
                            leaves[field] = digest(sorted(values, key=encoded))
                        else:
                            omitted += 1
                else:
                    leaves[path] = digest(ordered)
                return
            identities = []
            for row in ordered:
                identity = {k: row[k] for k in ('id', 'key', 'symbol', 'code', 'account', 'scope') if isinstance(row, dict) and k in row}
                identities.append(digest(identity)[:16] if identity else None)
            for i, row in enumerate(ordered):
                label = identities[i]
                label = '@' + label if label and identities.count(label) == 1 else str(i)
                walk(row, path + '/' + label)
        else:
            leaves[path or '/'] = digest(item)

    walk(value, '')
    return dict(schema_version=SCHEMA, root=digest(value), leaves=leaves, omitted=omitted, grouped_lists=grouped,
                scope=digest(dict(scoped=scoped, dependencies=event.get('review_dependencies'), followup=event.get('followup_scope'))))


def changes(previous, current):
    if not previous or not current:
        return dict(status='BASELINE_UNAVAILABLE', fields=[], omitted=0)
    if previous.get('schema_version') != current.get('schema_version'):
        return dict(status='DIAGNOSTIC_SCHEMA_CHANGED', fields=[], omitted=0)
    old, new = previous['leaves'], current['leaves']
    paths = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    return dict(status='COMPARED', fields=paths[:MAX_DIFFS], changed_count=len(paths),
                omitted=max(0, len(paths)-MAX_DIFFS), baseline_truncated=previous.get('omitted', 0),
                current_truncated=current.get('omitted', 0), root_changed=previous['root'] != current['root'],
                grouped_lists=sorted(set(previous.get('grouped_lists', [])) | set(current.get('grouped_lists', []))),
                scope_changed=previous.get('scope') != current.get('scope'))


def code_version(project):
    """Actual installed files, not a potentially unrelated checkout HEAD."""
    paths = ('src/tokmo/programs/news_decision_engine.py', 'src/tokmo/programs/news_context_efficiency.py',
             'src/tokmo/programs/news_incremental_review.py', 'src/tokmo/programs/news_portfolio_context.py',
             'src/tokmo/programs/news_usage_diagnostics.py', 'tools/news_decision_models.py',
             'src/tokmo/programs/news_model_wait.py', 'src/tokmo/records/news_decision_store.py',
             'tools/subscription_json_model.py',
             'tools/run_news_decision_worker.py')
    files = {}
    for name in paths:
        p = Path(project)/name
        files[name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return dict(id=digest(files), files=files)


class UsageDiagnostics:
    def __init__(self, directory, *, now=None, purpose='unknown', task_id=None):
        self.directory = Path(directory)
        self.run_id = uuid4().hex
        self.now = now or datetime.now(timezone.utc)
        self.purpose = purpose if purpose in {'operation', 'recovery', 'replay', 'verification'} else 'unknown'
        # A task label is optional; never accept a whole free-form request here.
        self.task_id = task_id if isinstance(task_id, str) and len(task_id) <= 80 and all(c.isalnum() or c in '-_.' for c in task_id) else None
        self.counts = Counter()
        self.errors = 0
        self.elapsed = 0.0
        self.gates = {}
        self.parents = {}
        self.evaluation_id = None

    def safe(self, method, *args, **kwargs):
        start = time.monotonic()
        try:
            return getattr(self, method)(*args, **kwargs)
        except Exception:
            self.errors += 1
            return None
        finally:
            self.elapsed += time.monotonic()-start

    def emit(self, kind, **data):
        record = dict(schema_version=SCHEMA, run_id=self.run_id, purpose=self.purpose, task_id=self.task_id,
                      kind=kind, recorded_at=datetime.now(timezone.utc).isoformat(), **data)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        path = self.directory/(self.now.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()+'.jsonl')
        # Existing worker lock provides serialization. One append per complete row;
        # a crash-truncated tail is reported by the reader, never silently counted.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as handle:
                fd = None
                handle.write(encoded(record)+'\n')
        finally:
            if fd is not None:
                os.close(fd)
        self.counts[kind] += 1

    def start(self, project, config, screen, analyze):
        settings = {k: config.get(k) for k in ('context_mode', 'reuse_unchanged_reviews', 'scoped_review_reuse',
                    'event_detail_enabled', 'typed_review_enabled', 'model_wait_enabled',
                    'incremental_market_observations', 'screen_model', 'analysis_model', 'analysis_reasoning_effort')}
        policies = {name: getattr(model, 'policy_fingerprint', None) for name, model in [('screen', screen), ('analyze', analyze)]}
        self.emit('run_started', at=self.now.isoformat(), code=code_version(project), settings=settings, policies=policies)

    def evaluation(self, task, event_id=None):
        self.evaluation_id = uuid4().hex
        self.emit('evaluation_started', evaluation_id=self.evaluation_id, task=task, event_id=event_id,
                  gate_id=self.gates.pop(event_id, None))
        return self.evaluation_id

    def gate(self, event, *, at, outcome, reasons, context=None, scoped=False):
        gate_id = uuid4().hex
        self.gates[event['id']] = gate_id
        current = baseline(event, context, scoped) if context is not None else None
        plan = event.get('review_reuse_plan') or {}
        self.emit('review_gate', gate_id=gate_id, event_id=event['id'], at=at,
                  outcome=outcome, reasons=list(reasons), changes=changes(event.get(BASELINE_KEY), current),
                  assessed_at=event.get('updated_at'), next_check_at=event.get('next_check_at'),
                  recheck_at=plan.get('after'), plan_reference=dict(event_id=event['id'], judged_at=plan.get('judged_at')),
                  recheck_kind=plan.get('kind'), safety_at=plan.get('safety_at'),
                  source_state=(event.get('source_enrichment') or {}).get('status'))

    def baseline(self, event, context, scoped):
        return baseline(event, context, scoped)

    def call_start(self, call_id, stage, request, at):
        evaluation_id = self.evaluation_id
        parent = self.parents.get(evaluation_id)
        is_followup = bool(request.get('correction') or request.get('repair'))
        self.parents[evaluation_id] = call_id
        self.emit('call_started', call_id=call_id, evaluation_id=evaluation_id, stage=stage, at=at,
                  parent_call_id=parent if is_followup else None, review_reason=request.get('review_reason'),
                  correction=bool(request.get('correction')), correction_reason=(request.get('correction') or {}).get('reason'),
                  input_ids=[r['id'] for r in request.get('evidence', [])],
                  prior_event_ids=[r.get('id', r.get('event_id')) for r in request.get('events', [])],
                  review_trigger=request.get('review_trigger', {}).get('reasons', []),
                  evidence_changed=request.get('review_trigger', {}).get('evidence_changed'),
                  recovery_attribution='run_may_include_new_inputs' if self.purpose == 'recovery' else None,
                  decision_plan={k: v for k, v in (request.get('review_trigger', {}).get('recheck_plan') or {}).items()
                                 if k in {'judged_at', 'after'}},
                  plan_reason_digest=digest((request.get('review_trigger', {}).get('recheck_plan') or {}).get('reason')))


    def call_finished(self, key, result):
        self.emit('call_finished', call_id=key, evaluation_id=self.evaluation_id,
                  usage=usage_view(result.get('_usage')), model=result.get('_model'),
                  decoded=not bool(result.get('invalid_model_reason')),
                  decode_error=result.get('invalid_model_reason'))

    def prompt(self, call_id, prompt, request, schema):
        # Areas are overlapping views, not additive token allocations.
        areas = {k: size(v) for k, v in request.items() if k in {'context', 'events', 'evidence', 'comparison', 'continuity'}}
        for k, v in (request.get('context') or {}).items():
            areas['context.'+k] = size(v)
        request_text = json.dumps(request, ensure_ascii=False)
        self.emit('prompt', call_id=call_id, scope='application_prompt', size=size(prompt), areas=areas,
                  schema_size=size(schema) if schema is not None else None, request_size=size(request_text),
                  policy_and_framing_chars=len(prompt)-len(request_text), area_sizes_are_nonadditive=True,
                  counts={k: len(request.get(k) or []) for k in ('evidence', 'events')})

    def finish(self, report):
        health = report.get('health') or {}
        ticks = report.get('ticks') or []
        self.emit('run_finished', status=report.get('status'), input_states=report.get('input_states'),
                  at=datetime.now(timezone.utc).isoformat(),
                  work=dict(calls=sum(t.get('calls', 0) for t in ticks),
                            reused=sum(len(t.get('reused_reviews', [])) for t in ticks),
                            review_errors=len(health.get('review_errors', [])),
                            input_errors=sum(r.get('count', 0) for r in health.get('input_errors', [])),
                            pending_proposals=report.get('pending_proposals'),
                            queued=(report.get('delivery') or {}).get('queued')),
                  counts=dict(self.counts), logging_errors=self.errors, instrumentation_ms=round(self.elapsed*1000, 3))
        return self.status()

    def status(self):
        return dict(run_id=self.run_id, coverage='incomplete' if self.errors else 'complete',
                    logging_errors=self.errors, records=sum(self.counts.values()), instrumentation_ms=round(self.elapsed*1000, 3))


@contextmanager
def observe_call(recorder, call_id, stage, request, at):
    if recorder is None:
        yield
        return
    recorder.safe('call_start', call_id, stage, request, at)
    token = _ACTIVE.set((recorder, call_id))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def transport_observation(kind, **data):
    active = _ACTIVE.get()
    if active is not None:
        recorder, call_id = active
        recorder.safe('emit', kind, call_id=call_id, **data)


def prompt_observation(prompt, request, schema):
    active = _ACTIVE.get()
    if active is not None:
        recorder, call_id = active
        recorder.safe('prompt', call_id, prompt, request, schema)


def usage_observation(metadata):
    if _ACTIVE.get() is None:
        return
    try:
        transport_observation('provider_received', usage=usage_view(metadata.get('_usage')),
                              model=metadata.get('_model'), effort=metadata.get('_reasoning_effort'),
                              elapsed_seconds=metadata.get('_elapsed_seconds'))
    except Exception:
        _ACTIVE.get()[0].errors += 1
