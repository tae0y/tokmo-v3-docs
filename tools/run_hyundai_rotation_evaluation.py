#!/usr/bin/env python3
"""Accumulate rotation outcomes from saved data; no models, broker or messages."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tokmo.programs.rotation_evaluation import collect


def main(argv=None):
    home=Path.home();parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path(__file__).resolve().parents[1]/'var/state/hyundai-rotation')
    parser.add_argument('--data-root',type=Path,default=Path(__file__).resolve().parents[1]/'var/data')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    result=collect(state=args.state_dir,data_root=args.data_root,
                   output=args.output or args.state_dir/'evaluation',now=datetime.now(timezone.utc))
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status'] in {'OK','BUSY'} else 2


if __name__=='__main__':raise SystemExit(main())
