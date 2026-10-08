"""Invoke the existing approved Quant CLI, with no alternate provider path."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess

from tokmo.records.quant_decision_reports import atomic_private
from tokmo.programs.rotation_analysis import RotationAnalysisError


_BLOCK_CODES = frozenset({
    'QUANT_PRODUCT_SCOPE_NOT_APPROVED', 'QUANT_ROUTE_OR_QUOTA_BLOCKED',
    'QUANT_CALL_BUDGET_EXHAUSTED', 'QUANT_CALLER_OR_MODEL_NOT_ADMITTED',
    'QUANT_RECEIPT_UNAVAILABLE', 'QUANT_BOUNDS_NOT_ADMITTED',
})


def failure_code(output, *, key, purpose):
    """Read this attempt's failed report; return a closed code, never raw text."""
    try:
        reports=[p for p in output.iterdir() if re.fullmatch(r'qd-[a-f0-9]{16}\.json',p.name)]
        if len(reports)!=1 or reports[0].is_symlink():return 'quant_entry_failed'
        value=json.loads(reports[0].read_text())
        rows=value['results']
        if (value.get('report_id')!=reports[0].stem or value.get('purpose')!=purpose
                or not isinstance(rows,list) or len(rows)!=1 or rows[0].get('key')!=key
                or rows[0].get('status')!='FAILED'):return 'quant_entry_failed'
        errors=rows[0].get('errors')
        if not isinstance(errors,list) or not errors or not isinstance(errors[-1],str):return 'quant_entry_failed'
        last=errors[-1]
        if last.startswith('model_call:'):
            reason=last.removeprefix('model_call:')
            if reason in _BLOCK_CODES:return reason
            if reason in {'SubscriptionInferenceError','TimeoutError','ConnectionError','HTTPError','URLError'}:
                return 'quant_model_failed'
        if last.startswith('invalid_response:'):return 'quant_validation_failed'
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        pass
    return 'quant_entry_failed'


def launch_until(args, *, deadline):
    seconds=(deadline-datetime.now(timezone.utc)).total_seconds()
    if seconds<=0:raise TimeoutError('analysis_deadline')
    # Do not leak holdings or CLI/provider diagnostics to the cron log. The
    # existing entry writes its private report and model-call receipts.
    process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,start_new_session=True)
    try:return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait()
        raise TimeoutError('analysis_deadline') from None


DEFAULT_MODEL='codex:gpt-6-astra'   # var/data/models.json ``quant.rotation_entry`` replaces it (see values/model_registry)


class ExistingQuantAnalyzer:
    def __init__(self, *, python, entry, root, launch=launch_until, model=DEFAULT_MODEL):
        self.python=str(python);self.entry=Path(entry);self.root=Path(root);self.launch=launch;self.model=str(model)

    def __call__(self, snapshot, deadline):
        requests=snapshot.get('analysis_requests')
        if not isinstance(requests,list) or len(requests)!=1 or not isinstance(requests[0],dict):
            raise ValueError('quant_request_scope')
        request=requests[0];key=request.get('key')
        if not isinstance(key,str) or not re.fullmatch(r'(KR|US):[A-Za-z0-9._-]+',key):
            raise ValueError('quant_request_scope')
        # A directory exists for at most one attempt of this day's experiment.
        # Existing Quant handles its own one permitted validation rewrite.
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        output=self.root/'quant';output.mkdir(mode=0o700)
        capture=self.root/'request-snapshot.json';atomic_private(capture,snapshot)
        experiment=request.get('rotation',{}).get('experiment_id','hyundai')
        purpose=f'hyundai-short-term-rotation:{deadline.date().isoformat()}:{experiment}'
        args=[self.python,str(self.entry),'--snapshot',str(capture),'--request-only',
              '--model',self.model,'--max-model-calls','2','--symbol',key,
              '--new-limit','0','--purpose',purpose,'--output',str(output)]
        try:
            code=self.launch(args,deadline=deadline)
            atomic_private(self.root/'execution.json',{'returncode':code,'deadline':deadline.isoformat()})
        except Exception as error:
            atomic_private(self.root/'execution.json',{'error':type(error).__name__,'deadline':deadline.isoformat()})
            raise
        if code!=0:raise RotationAnalysisError(failure_code(output,key=key,purpose=purpose))
        reports=[p for p in output.iterdir() if re.fullmatch(r'qd-[a-f0-9]{16}\.json',p.name)]
        if len(reports)!=1 or reports[0].is_symlink():raise ValueError('quant_report_missing_or_ambiguous')
        result=json.loads(reports[0].read_text())
        if result.get('report_id')!=reports[0].stem:raise ValueError('quant_report_identity')
        return result
