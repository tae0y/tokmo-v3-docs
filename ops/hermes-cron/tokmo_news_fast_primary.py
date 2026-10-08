#!/opt/homebrew/bin/python3.12
"""Five-minute primary NEWS collection, using the existing source ledger."""
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # ops/hermes-cron/<this> -> project root
import sys

# Collection already runs from this canonical project. Reuse its full set of
# source-health/universe dependencies; Pulse and the decision worker stay on
# their separate deployed snapshot.
PROJECT = PROJECT_ROOT
ROOT = PROJECT_ROOT / 'var/data'
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'tools')]
# Connector subprocesses need the same source path as this wrapper.
os.environ['PYTHONPATH'] = str(PROJECT / 'src') + os.pathsep + os.environ.get('PYTHONPATH', '')
from tokmo.programs.news_decision_config import read_decision_config
from run_news_fast_primary import main
from run_news_disclosure_today import main as dart_today



if __name__ == '__main__':
    if not read_decision_config(ROOT).get('enabled'):
        raise SystemExit(0)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = dart_today(['--home', str(Path.home())]) if datetime.now(timezone.utc).minute % 5 == 3 else main(['--root', str(ROOT),
                     '--manifest', str(PROJECT_ROOT / '.secrets/config/tokmo-v3/news_poll_manifest_v1.json'),
                     '--credentials', str(PROJECT_ROOT / '.secrets/config/tokmo/news.env')])
    latest = ROOT / 'news/group_runtime/fast-primary-latest.json'
    temporary = latest.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(), exit_code=code,
                                         detail=captured.getvalue().strip()), ensure_ascii=False))
    temporary.replace(latest)
    if code:
        raise SystemExit('NEWS primary collection failed; check source ledger')
