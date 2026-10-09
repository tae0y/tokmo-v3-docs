"""Small opt-in selector shared by Pulse and the decision worker."""

import json
from datetime import datetime
from pathlib import Path


def read_decision_config(root):
    path = Path(root) / "news_decision.json"
    if not path.exists():
        return {"enabled": False}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("news_decision.json requires boolean enabled")
    return value


def delivery_window_open(config, now):
    """Optional one-time notification deadline; analysis remains enabled after expiry."""
    if "delivery_until" not in config:
        return True
    try:
        end = datetime.fromisoformat(config["delivery_until"].replace("Z", "+00:00"))
        return end.utcoffset() is not None and now < end
    except (ValueError, TypeError, AttributeError):
        return False
