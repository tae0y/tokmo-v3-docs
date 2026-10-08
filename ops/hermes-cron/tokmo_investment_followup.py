#!/usr/bin/env python3
"""Installed Tokmo entry: no args = scheduled review; args = conversation CLI."""
from pathlib import Path
import json
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # ops/hermes-cron/<this> -> project root


home = Path.home()
runtime = PROJECT_ROOT
project = PROJECT_ROOT
root = PROJECT_ROOT / 'var/data'
python = project / '.venv/bin/python'
if not sys.argv[1:]:
    args = [str(runtime/'tools/run_investment_followup_cron.py'), '--root', str(root), '--project-root', str(project)]
else:
    config = json.loads((root/'investment_followup.json').read_text())
    args = [str(runtime/'tools/run_investment_followup.py'), '--project-root', str(project),
            '--data-root', str(root), '--state', str(root/'private/investment-followup.sqlite3'),
            '--model', config.get('analysis_model','glm-5.3'),
            '--screen-model', config.get('screen_model','glm-5.3-flash'),
            *(['--first-pass-model', str(config['first_pass_model'])] if config.get('first_pass_model') else []),
            '--retry-attempt', str(config.get('retry_attempt','1'))]
    latest = root/'ops/quant-inputs/latest.json'
    supplements = list(config.get('supplements',[]))
    if latest.is_file() and str(latest) not in supplements:
        supplements.insert(0,str(latest))
    for path in supplements:
        args.extend(['--supplement',str(path)])
    # Historical pension comparisons are explicit caller arguments only.
    args.extend(sys.argv[1:])
env = dict(os.environ)
env['PYTHONPATH'] = str(runtime/'src')+os.pathsep+str(runtime/'tools')
env['PYTHONDONTWRITEBYTECODE'] = '1'
os.execve(str(python), [str(python), '-B', *args], env)
