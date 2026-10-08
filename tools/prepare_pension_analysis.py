#!/usr/bin/env python3
"""Import pension analysis evidence. No broker, official-ledger, or send writes.

Use --state in a temporary directory for rehearsal. Source workbooks are retained
privately beside this manifest, so clearing an attachment cache loses no evidence.
"""
import argparse
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tokmo.programs.pension_analysis_sources import (
    add_import, analysis_view, compare_statement, new_manifest, read_workbook,
    seed_statement, statement_import, transaction_import,
)
from tokmo.records.quant_decision_reports import atomic_private
from tokmo.values.quant_decisions import instant


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['seed', 'statement', 'transactions', 'resolve', 'preview'])
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--account')
    p.add_argument('--annotation', type=Path)
    p.add_argument('--workbook', type=Path)
    p.add_argument('--identities', type=Path, help='Exact product name to {kind:etf/other,symbol} JSON')
    p.add_argument('--from-date', help='The export query start, not the first nonempty transaction date')
    p.add_argument('--through', help='Statement trade cutoff, or export query end date')
    p.add_argument('--at', help='Evidence received time, with timezone; default now')
    p.add_argument('--label', default='증권사 계좌현황')
    p.add_argument('--row-id')
    p.add_argument('--resolution', choices=['execution', 'replace', 'ignore'])
    p.add_argument('--side', choices=['BUY', 'SELL'])
    p.add_argument('--replaces')
    p.add_argument('--reason')
    a = p.parse_args(argv)
    now = instant(a.at) if a.at else datetime.now(ZoneInfo('Asia/Seoul'))
    if a.command == 'preview':
        value = json.loads(a.state.read_text())
        print(json.dumps(analysis_view(value, as_of=now, through=a.through), ensure_ascii=False, indent=2))
        return 0
    if a.command == 'seed' and not a.annotation:
        p.error('seed에는 --annotation이 필요합니다')
    if a.command in {'statement', 'transactions'} and not (a.workbook and a.identities and a.through):
        p.error('엑셀·종목 식별표·거래 반영/조회 종료일이 필요합니다')
    if a.command == 'transactions' and not a.from_date:
        p.error('거래내역에는 --from-date 조회 시작일이 필요합니다')
    if a.command == 'resolve' and not (a.row_id and a.resolution and a.reason):
        p.error('정정에는 행 식별자·처리 방법·근거가 필요합니다')
    if a.command == 'resolve' and a.resolution != 'ignore' and not a.side:
        p.error('체결로 처리할 정정의 매수/매도 방향이 필요합니다')
    if a.resolution == 'replace' and not a.replaces:
        p.error('정정으로 대체할 원거래 행 식별자가 필요합니다')
    a.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with a.state.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        value = json.loads(a.state.read_text()) if a.state.exists() else new_manifest(a.account)
        if a.account and a.account != value['account']:
            raise ValueError('연금 계좌 식별자가 기존 자료와 다릅니다')
        result = {'command': a.command, 'official_stores_changed': False}
        if a.command == 'seed':
            value = add_import(value, seed_statement(a.annotation, as_of=now), kind='statement')
        elif a.command == 'resolve':
            row = next((r for e in value['exports'] for r in e['rows'] if r['id'] == a.row_id), None)
            if not row or row.get('side') in {'BUY', 'SELL'}:
                raise ValueError('미해결 거래구분 행을 지정해 주세요')
            value['resolutions'].append(dict(row_id=a.row_id, action=a.resolution, side=a.side,
                                              replaces=a.replaces, reason=a.reason, recorded_at=now.isoformat()))
        else:
            raw, rows = read_workbook(a.workbook)
            identities = json.loads(a.identities.read_text())
            if a.command == 'statement':
                entry = statement_import(raw, rows, identities, through=a.through, available_at=now, label=a.label)
                result['comparison'] = compare_statement(value, entry, as_of=now) if value['statements'] else {'status': 'FIRST_BASELINE'}
                entry['comparison'] = result['comparison']
            else:
                entry = transaction_import(raw, rows, identities, complete_from=a.from_date,
                                           complete_through=a.through, available_at=now)
            directory = a.state.parent / (a.state.stem + '-sources')
            directory.mkdir(mode=0o700, exist_ok=True)
            source = directory / (sha256(raw).hexdigest() + '.xlsx')
            if source.exists():
                if source.read_bytes() != raw:
                    raise ValueError('보관 원본 해시가 다릅니다')
            else:
                fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            entry['source_path'] = str(source.resolve())
            value = add_import(value, entry, kind=a.command)
        view=analysis_view(value, as_of=now) if value['statements'] else None
        atomic_private(a.state, value)
        result.update(statements=len(value['statements']), exports=len(value['exports']), view=view)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
