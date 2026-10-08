"""Read existing identities for order alerts, with no new provider or store."""
import json
from pathlib import Path

from tokmo.records.playbook_identity import read_instrument_name


def read_order_alert_name(playbook_root, *, account_scope, market, symbol,
                         capture_path=None):
    name = read_instrument_name(playbook_root, account_scope=account_scope,
                                market=market, symbol=symbol)
    if name and any("가" <= c <= "힣" for c in name):
        return name
    if capture_path is None:
        return None
    try:
        value = json.loads(Path(capture_path).read_text(encoding="utf-8"))
        names = {row["name"] for row in value.get("instruments", [])
                 if isinstance(row, dict) and row.get("market") == market
                 and row.get("symbol") == symbol
                 and isinstance(row.get("name"), str) and row["name"].strip()}
        if len(names) == 1:
            name = names.pop().strip()
            if any("가" <= c <= "힣" for c in name) and "\n" not in name and "\r" not in name:
                return name
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        pass
    return None
