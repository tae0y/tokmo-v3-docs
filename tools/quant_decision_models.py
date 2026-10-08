"""Bounded Quant product adapter; activation is deliberately fail-closed."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
from uuid import uuid4
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from tokmo.programs.news_classifier_executor import read_zai_credential_file
from tokmo.records.quant_decision_reports import atomic_private

ROUTING_PATH = Path.home()/'.hermes/MODEL_ROUTING_STATE.md'
QUOTA_PATH = Path.home()/'.hermes/cron/state/model-quota-latest.json'
RESOLVER_PATH = Path.home()/'.hermes/scripts/model_routing.py'   # the installed shared resolver (byte-identical to shared/model_routing); see tools/install_product_gate.py
CORE_PATH = Path.home()/'.hermes/CRITICAL_OPERATING_CONTRACT.md'
RUNTIME_ROOT = Path(__file__).resolve().parents[1]  # the canonical project tree is the installed product (2026-09-24); the approval manifest lives at its root
PRODUCT_FILES = (
    'src/tokmo/programs/etf_context.py', 'src/tokmo/programs/etf_portfolio_context.py',
    'src/tokmo/programs/etf_portfolio_refresh.py', 'src/tokmo/programs/kis_etf_refresh.py',
    'src/tokmo/facts/etf_issuer.py', 'src/tokmo/facts/etf_issuer_live.py',
    'src/tokmo/facts/kis_etf.py', 'src/tokmo/programs/kis_etf.py',
    'src/tokmo/values/kis_etf.py', 'src/tokmo/records/kis_state.py',
    'src/tokmo/records/kis_context.py', 'src/tokmo/kis_composition.py',
    'src/tokmo/programs/quant_investor_flows.py',
    'tools/run_quant_decisions.py', 'tools/quant_decision_models.py',
    'src/tokmo/programs/quant_decision_analysis.py', 'src/tokmo/programs/quant_decision_context.py',
    'src/tokmo/programs/quant_decision_inputs.py', 'src/tokmo/records/quant_decision_reports.py',
    'src/tokmo/programs/quant_hypotheses.py', 'src/tokmo/programs/quant_dialogue.py',
    'src/tokmo/values/quant_decisions.py', 'tools/subscription_json_model.py',
    'tools/run_investment_followup.py', 'src/tokmo/programs/investment_plans.py',
    'tools/run_investment_followup_cron.py',
    'src/tokmo/programs/investment_followup.py', 'src/tokmo/programs/investment_followup_worker.py',
    'src/tokmo/programs/investment_price_checkpoint.py', 'src/tokmo/records/investment_followup.py',
)
# Calling bounds are product approval, not a model-version catalog.
PROVIDER_PROFILES = {
    'zai':dict(contract='zai-chat', reasoning_effort='max', max_tokens=16384, timeout=240),
    'codex':dict(contract='codex-cli', reasoning_effort='medium', max_tokens=None, timeout=120),
    'claude':dict(contract='claude-cli', reasoning_effort='default', max_tokens=None, timeout=120),
}
PRODUCT_SCOPE = dict(on_demand=True, scheduled_plans=True, providers=PROVIDER_PROFILES, format='json_object')


def _resolver():
    raw = _product_bytes(RESOLVER_PATH)
    namespace = {'__name__':'quant_model_routing', '__file__':str(RESOLVER_PATH)}
    exec(compile(raw, str(RESOLVER_PATH), 'exec'), namespace)
    return namespace, sha256(raw).hexdigest()


def _model_key(route):
    return route['model'] if route['provider']=='zai' else route['provider']+':'+route['model']


def model_profile(selection, routing=None, *, resolver=None):
    resolver = resolver if resolver is not None else _resolver()[0]
    routing = _product_bytes(ROUTING_PATH) if routing is None else routing
    route = resolver['resolve'](routing.decode('utf-8'), selection, use='product')
    bounds = PROVIDER_PROFILES.get(route['provider'])
    if bounds is None or route['contract'] != bounds['contract']:
        raise ValueError('MODEL_NOT_IN_SCOPE')
    return {**route, **bounds}


def model_profiles():
    routing = _product_bytes(ROUTING_PATH)
    resolver, _ = _resolver()
    return {_model_key(dict(model=model, **route)): model_profile(model, routing, resolver=resolver)
            for model, route in resolver['parse_policy'](routing.decode('utf-8'))['routes'].items()
            if 'product' in route['uses'] and route['provider'] in PROVIDER_PROFILES}


def canonical_model(name):
    if name in ('off', 'current', 'recorded'):
        return name
    return _model_key(model_profile(name))


def utc_now():
    return datetime.now(timezone.utc)


def _product_bytes(path, *, private=False):
    # Reject symlinks in the entire path, including an alternate runtime root.
    if path.resolve(strict=True) != path:
        raise ValueError('symlink or noncanonical path')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or (private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValueError('file ownership or mode')
        return stream.read()


def require_product_scope():
    """Owner installs ONE private approval record only after review/policy approval.

    No activation writer, runtime argument, callback or environment override.
    The owner is the trust boundary; this is not a sandbox against an owner
    deliberately modifying the running Python process or approval record.
    """
    try:
        root = RUNTIME_ROOT
        info = root.stat()
        if (root.resolve(strict=True) != root or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('runtime root')
        primary_entry = root/'tools/run_quant_decisions.py'
        entry = Path(sys.argv[0]).absolute()
        if (entry not in {primary_entry,root/'tools/run_investment_followup.py',root/'tools/run_investment_followup_cron.py'}
                or Path(sys.modules['__main__'].__file__).absolute() != entry
                or Path(__file__).absolute() != root/'tools/quant_decision_models.py'):
            raise ValueError('installed process entrypoint')
        manifest_path = root/'quant-on-demand-approval.json'
        raw = _product_bytes(manifest_path, private=True)
        approval = json.loads(raw)
        review = approval['review']
        candidate = review['candidate_sha256']
        if (approval['schema'] != 'quant-on-demand-install-v2'
                or approval['runtime_root'] != str(root) or approval['entrypoint'] != str(primary_entry)
                or review['verdict'] != 'PASS' or not isinstance(candidate, str)
                or len(candidate) != 64 or any(c not in '0123456789abcdef' for c in candidate)
                or not isinstance(approval['policy_approval'], str) or not approval['policy_approval'].strip()
                or json.dumps(approval['scope'], sort_keys=True) != json.dumps(PRODUCT_SCOPE, sort_keys=True)
                or set(approval['files']) != set(PRODUCT_FILES)
                or set(approval['authority']) != {str(CORE_PATH), str(ROUTING_PATH)}):
            raise ValueError('approval facts')
        for name in PRODUCT_FILES:
            path = root/name
            for directory in path.parents:
                if directory == root: break
                info = directory.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise ValueError('runtime directory ownership or mode')
            if sha256(_product_bytes(path)).hexdigest() != approval['files'][name]:
                raise ValueError('reviewed file drift')
            module_name = '__main__' if path == entry else (
                Path(name).stem if name.startswith('tools/') else name[4:-3].replace('/', '.'))
            module = sys.modules.get(module_name)
            if module is not None and Path(module.__file__).absolute() != path:
                    raise ValueError('loaded module outside installed root')
        authority = {str(p):sha256(_product_bytes(p)).hexdigest() for p in (CORE_PATH, ROUTING_PATH)}
        if (authority[str(CORE_PATH)] != approval['authority'][str(CORE_PATH)]
                or approval['authority'][str(ROUTING_PATH)] != 'current'
                or sha256(_product_bytes(RESOLVER_PATH)).hexdigest() != approval['resolver_sha256']):
            raise ValueError('approved authority drift')
        return dict(manifest_path=str(manifest_path), manifest_sha256=sha256(raw).hexdigest(),
            review_candidate_sha256=candidate, runtime_root=str(root), entrypoint=str(entry),
            runtime_files_sha256=sha256(json.dumps(approval['files'], sort_keys=True,
                                                  separators=(',', ':')).encode()).hexdigest(),
            core_path=str(CORE_PATH), core_sha256=authority[str(CORE_PATH)],
            routing_state_sha256=authority[str(ROUTING_PATH)], resolver_sha256=approval['resolver_sha256'], checked_at=utc_now().isoformat())
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise PermissionError('QUANT_PRODUCT_SCOPE_NOT_APPROVED') from error


def route_authority(model):
    """Resolve the current product route and model-specific quota from one snapshot.

    Restricted NEWS/probe/group routes cannot grant Quant product scope.
    """
    try:
        with ROUTING_PATH.open('rb') as stream:
            routing = stream.read()
            routing_mtime = os.fstat(stream.fileno()).st_mtime_ns
        with QUOTA_PATH.open('rb') as stream:
            quota = stream.read()
            quota_mtime = os.fstat(stream.fileno()).st_mtime_ns
        resolver, parser_digest = _resolver()
        profile = model_profile(model, routing, resolver=resolver)
        data = json.loads(quota)
        if data['schema'] != 'model-quota-latest-v1':
            raise ValueError('QUOTA_SCHEMA_INVALID')
        at = datetime.fromisoformat(data['observed_at'].replace('Z','+00:00'))
        if at.tzinfo is None or not -300 <= (utc_now()-at).total_seconds() <= 2700:
            raise ValueError('QUOTA_FRESHNESS_INVALID')
        provider = data['providers'][profile['provider']]
        if provider['ok'] is not True or not isinstance(provider['windows'], list):
            raise ValueError('QUOTA_PROVIDER_INVALID')
        labels = profile['windows']
        for label in labels:
            windows = [w for w in provider['windows'] if w['label'] == label]
            if len(windows) != 1:
                raise ValueError('QUOTA_WINDOW_INVALID')
            remaining = windows[0]['remaining']
            if (not isinstance(remaining, (int,float)) or isinstance(remaining,bool)
                    or not math.isfinite(remaining) or not 0 < remaining <= 100):
                raise ValueError('QUOTA_UNAVAILABLE')
        return {'routing_state_path':str(ROUTING_PATH.resolve()), 'routing_state_mtime_ns':routing_mtime,
                'routing_state_sha256':sha256(routing).hexdigest(),
                'quota_snapshot_path':str(QUOTA_PATH.resolve()), 'quota_snapshot_mtime_ns':quota_mtime,
                'quota_snapshot_sha256':sha256(quota).hexdigest(), 'quota_observed_at':at.isoformat(),
                'route_admission':'ADMITTED',
                'route_parser_sha256':parser_digest, 'provider':profile['provider'], 'model':profile['model']}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise PermissionError('QUANT_ROUTE_OR_QUOTA_BLOCKED') from error


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class QuantModel:
    def __init__(self, *, env_file, model=None, timeout=None, max_tokens=None, opener=None,
                 reasoning_effort=None, on_demand=False, receipt_dir=None, max_calls=None):
        model = model or ('glm' if on_demand else 'glm-5.3')
        profile = {}
        if on_demand:
            try:
                profile = model_profile(model)
                model = _model_key(profile)
            except (OSError, ValueError):
                pass  # The physical call records a fail-closed denial after checking product approval.

        if timeout is None:timeout=profile.get('timeout',120)
        if max_tokens is None:max_tokens=profile.get('max_tokens',4096)
        if reasoning_effort is None:reasoning_effort=profile.get('reasoning_effort','medium' if model.startswith('codex:') else 'low')
        self.env_file, self.model = env_file, model
        self.reasoning_effort = reasoning_effort
        self.timeout, self.max_tokens = timeout, max_tokens
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.on_demand, self.receipt_dir = on_demand, receipt_dir
        if max_calls is not None and (type(max_calls) is not int or max_calls<1):raise ValueError('invalid call budget')
        self.max_calls=max_calls
        self.calls = []
        self.receipt_failed = False

    @property
    def reuse_contract(self):
        return {'provider':self.model.split(':',1)[0] if ':' in self.model else 'zai',
                'model':self.model,'reasoning_effort':self.reasoning_effort,
                'max_tokens':self.max_tokens,'format':'json_object',
                'adapter_sha256':sha256(Path(__file__).read_bytes()).hexdigest()}

    def _legacy_call(self, prompt):
        record = {'model': self.model, 'reasoning_effort': self.reasoning_effort,
                  'status': 'STARTED', 'input_chars': len(prompt)}
        self.calls.append(record)
        start = time.monotonic()
        try:
            if self.model.startswith('codex:'):
                from subscription_json_model import infer
                provider, model = self.model.split(':', 1)
                text, metadata = infer(prompt, provider=provider, model=model, timeout=self.timeout,
                                       effort=self.reasoning_effort)
                record.update(status='RETURNED', usage=metadata['_usage'], response=text,
                    resolved_model=metadata['_model'])
                return text
            payload = {'model':self.model,'messages':[{'role':'user','content':prompt}], 'stream':False,
                       'thinking':{'type':'enabled'}, 'reasoning_effort':'low',
                       'max_tokens':self.max_tokens,'response_format':{'type':'json_object'}}
            req = Request('https://api.z.ai/api/coding/paas/v4/chat/completions', method='POST',
                          data=json.dumps(payload,ensure_ascii=False).encode(),
                          headers={'Authorization':'Bearer '+read_zai_credential_file(self.env_file),
                                   'Content-Type':'application/json'})
            with self.opener.open(req,timeout=self.timeout) as reply:
                raw=reply.read(1_048_577)
                if len(raw)>1_048_576: raise ValueError('model response too large')
                data=json.loads(raw)
            text=data['choices'][0]['message']['content']
            if not isinstance(text,str): raise ValueError('model content missing')
            record.update(status='RETURNED', usage={k:v for k,v in data.get('usage',{}).items()
                          if k in {'prompt_tokens','completion_tokens','total_tokens'} and isinstance(v,int)})
            record['response'] = text
            return text
        except Exception as error:
            record.update(status='FAILED',error_class=type(error).__name__)
            raise
        finally:
            record['elapsed_seconds']=round(time.monotonic()-start,3)

    def _recheck(self, authority):
        scope = require_product_scope()
        before = {k:v for k,v in authority['product_scope'].items() if k != 'checked_at'}
        after = {k:v for k,v in scope.items() if k != 'checked_at'}
        route = {k:v for k,v in authority.items() if k != 'product_scope'}
        if before != after or route_authority(self.model) != route:
            raise PermissionError('QUANT_PREFLIGHT_STALE')

    def __call__(self, prompt):
        if not self.on_demand:
            return self._legacy_call(prompt)
        profile = {}
        record = {'schema':'quant-model-call-v1', 'call_id':uuid4().hex,
                  'ordinal':len(self.calls)+1, 'provider':profile.get('provider'), 'model':self.model,
                  'reasoning_effort':self.reasoning_effort, 'max_tokens':self.max_tokens,
                  'timeout_seconds':self.timeout,
                  'status':'BLOCKED', 'transport_started':False, 'provider_started':'FALSE',
                  'requested_at':utc_now().isoformat(), 'input_chars':len(prompt)}
        self.calls.append(record)
        start = time.monotonic()
        started = False
        path = Path(self.receipt_dir)/(record['call_id']+'.json') if self.receipt_dir else None
        try:
            if self.max_calls is not None and len(self.calls)>self.max_calls:
                raise PermissionError('QUANT_CALL_BUDGET_EXHAUSTED')
            product_scope = require_product_scope()
            if not isinstance(product_scope, dict) or not product_scope.get('routing_state_sha256'):
                raise PermissionError('QUANT_PRODUCT_SCOPE_NOT_APPROVED')
            try:
                profile = model_profile(self.model)
            except (OSError, ValueError) as error:
                raise PermissionError('QUANT_ROUTE_OR_QUOTA_BLOCKED') from error
            record['provider'] = profile['provider']
            if self.receipt_failed or path is None:
                raise PermissionError('QUANT_RECEIPT_UNAVAILABLE')
            if self.max_tokens != profile['max_tokens'] or self.reasoning_effort != profile['reasoning_effort'] or not 0 < self.timeout <= profile['timeout']:
                raise PermissionError('QUANT_BOUNDS_NOT_ADMITTED')
            record['authority'] = route_authority(self.model)
            if (record['authority']['routing_state_sha256'] != product_scope['routing_state_sha256']
                    or record['authority']['route_parser_sha256'] != product_scope.get('resolver_sha256')):
                raise PermissionError('QUANT_PRODUCT_SCOPE_NOT_APPROVED')
            record['authority']['product_scope'] = product_scope
            if profile['provider']!='zai':
                # Product-owned inference: same route/quota check and durable
                # receipt as GLM; existing OAuth CLI, no extra agent/tool loop.
                from subscription_json_model import infer
                record.update(status='STARTING',transport_started=None,provider_started='UNKNOWN')
                atomic_private(path,record)
                self._recheck(record['authority'])
                started=True
                text,metadata=infer(prompt,provider=profile['provider'],model=profile['model'],
                    timeout=self.timeout,effort=self.reasoning_effort,diagnostic_path=path.with_suffix('.failure.json'))
                record.update(status='RETURNED',transport_started=True,provider_started='TRUE',usage=metadata['_usage'],
                    resolved_model=metadata['_model'],response=text)
                if not text.strip():raise ValueError('model content empty')
                return text
            payload = {'model':self.model,'messages':[{'role':'user','content':prompt}], 'stream':False,
                       'thinking':{'type':'enabled'}, 'reasoning_effort':self.reasoning_effort,
                       'max_tokens':self.max_tokens,'response_format':{'type':'json_object'}}
            req = Request('https://api.z.ai/api/coding/paas/v4/chat/completions', method='POST',
                          data=json.dumps(payload,ensure_ascii=False).encode(),
                          headers={'Authorization':'Bearer '+read_zai_credential_file(self.env_file),
                                   'Content-Type':'application/json'})
            # Durable intent precedes the only transport attempt. If this write
            # fails, open is unreachable; unknown outcomes never auto-retry.
            record.update(status='STARTING', transport_started=None, provider_started='UNKNOWN')
            atomic_private(path, record)
            self._recheck(record['authority'])
            started = True
            record.update(status='STARTED', transport_started=True, provider_started='UNKNOWN',
                          started_at=utc_now().isoformat())
            with self.opener.open(req,timeout=self.timeout) as reply:
                raw=reply.read(1_048_577)
                record['provider_started'] = 'TRUE'
                if len(raw)>1_048_576: raise ValueError('model response too large')
            data=json.loads(raw)
            text=data['choices'][0]['message']['content']
            if not isinstance(text,str): raise ValueError('model content missing')
            record['response']=text
            record['finish_reason']=data['choices'][0].get('finish_reason')
            usage = data.get('usage',{})
            record['usage'] = {k:v for k,v in usage.items() if k in
                {'prompt_tokens','completion_tokens','total_tokens'} and type(v) is int and v>=0}
            reasoning = usage.get('completion_tokens_details',{}).get('reasoning_tokens')
            if type(reasoning) is int and reasoning>=0: record['usage']['reasoning_tokens'] = reasoning
            cached=usage.get('prompt_tokens_details',{}).get('cached_tokens')
            if type(cached) is int and cached>=0:record['usage']['cached_tokens']=cached
            if record['finish_reason']=='length':raise ValueError('model output limit reached')
            if not text.strip():raise ValueError('model content empty')
            record['status'] = 'RETURNED'
            return text
        except Exception as error:
            record.update(status='FAILED' if started else 'BLOCKED', transport_started=started,
                          provider_started=record['provider_started'] if started else 'FALSE', error_class=type(error).__name__)
            if getattr(error,'category',None) in {'TRANSIENT','EXECUTOR_UNKNOWN','AUTH','QUOTA','REQUEST'}:
                record.update(error_category=error.category,exit_code=error.exit_code,
                              diagnostic_path=error.diagnostic_path,retryable=error.inference_retryable)
            if isinstance(error,PermissionError): record['block_reason'] = str(error)
            raise
        finally:
            record.update(elapsed_seconds=round(time.monotonic()-start,3), ended_at=utc_now().isoformat())
            if path is not None:
                try: atomic_private(path, record)
                except OSError:
                    self.receipt_failed = True
                    raise
