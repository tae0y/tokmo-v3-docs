#!/usr/bin/env python3
"""Print one PAPER result summary for cron stdout delivery; silent when nothing new."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from tokmo.programs.opportunity_result_notice import drain
from tokmo.programs.opportunity_rotation import Paper
from tokmo.values.opportunity_rotation import KST

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / 'var/data/opportunity-rotation'


def main(argv=None):
    parser = argparse.ArgumentParser(description='PAPER 결과 알림 — 저장된 판단만 사용, 발송은 cron 몫')
    parser.add_argument('--state-root', type=Path, default=DATA / 'paper')
    parser.add_argument('--ledger', type=Path, default=DATA / 'result-notice/ledger.json')
    args = parser.parse_args(argv)
    try:
        text = drain(Paper(args.state_root), args.ledger, now=datetime.now(KST).isoformat())
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps(dict(status='FAILED', error=str(exc)), ensure_ascii=False), file=sys.stderr)
        return 2
    if text:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
