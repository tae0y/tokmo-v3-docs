"""Read only the two named public-quote artifacts; never enumerate token state."""
import json
from pathlib import Path
import re

from tokmo.records.atomic_json import read_atomic_json


def read_kis_fact(root, operation, symbol):
    if operation not in {'components', 'nav', 'nav_daily'} or not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{6}', symbol):
        raise ValueError('etf_identity')
    path = Path(root) / 'kis' / f'{operation}-{symbol}.json'
    try:
        value = json.loads(read_atomic_json(path, max_bytes=2097152))
        if not isinstance(value, dict):
            raise ValueError('fact_shape')
        return value, 'OK'
    except FileNotFoundError:
        return None, 'MISSING'
    except (OSError, ValueError, TypeError):
        return None, 'INVALID'


def read_kis_integration(root):
    path = Path(root) / 'kis/integration.json'
    try:
        return json.loads(read_atomic_json(path, max_bytes=4096))
    except FileNotFoundError:
        return None
