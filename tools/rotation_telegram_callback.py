#!/usr/bin/env python3
"""Owned callback CLI. Input is supplied only by the existing Hermes receiver.

No getUpdates polling, model call or order submission. Owner identity comes
from the gateway configuration, never from the callback JSON or analysis.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))

from run_hyundai_rotation_analysis import select_policy
from tokmo.programs.rotation_analysis_delivery import resolve_callback, ApprovalError
from tokmo.records.rotation_analysis import RotationAnalysisStore


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path(__file__).resolve().parents[1]/'var/state/hyundai-rotation')
    parser.add_argument('--check',action='store_true',help='check owner, imports and policy without reading a callback or writing state')
    args=parser.parse_args(argv)
    try:
        owner=os.environ.get('TOKMO_ROTATION_OWNER_ID','')
        if not re.fullmatch('[1-9][0-9]*',owner):raise ApprovalError('owner_not_configured')
        if args.check:
            policy=select_policy(args.state_dir)
            print(json.dumps({'status':'READY','experiment_id':policy.experiment_id,'owner_configured':True}));return 0
        value=json.loads(sys.stdin.read(4097))
        if not isinstance(value,dict) or set(value)!={'user_id','chat_id','message_id','data'}:
            raise ApprovalError('invalid_callback_payload')
        if any(not isinstance(v,str) for v in value.values()):raise ApprovalError('invalid_callback_payload')
        if value['user_id']!=owner:raise ApprovalError('unauthorized_owner')
        policy=select_policy(args.state_dir)
        status=resolve_callback(store=RotationAnalysisStore(args.state_dir/'decisions'),
            proposal_root=args.state_dir/'proposals',policy=policy,owner_id=owner,
            now=datetime.now(timezone.utc),**value)
        print(json.dumps({'status':status},ensure_ascii=False));return 0
    except Exception as error:
        reason=str(error) if isinstance(error,ApprovalError) else type(error).__name__
        print(json.dumps({'status':'ERROR','reason':reason},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
