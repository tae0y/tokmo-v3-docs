"""N7: Sole pension-reminder live Telegram transport.

Registered in LIVE_TRANSPORT_MODULES. Uses the reviewed stdlib HTTPS boundary,
accepts sanitized reminder text plus injected sealed credentials/config, and
returns a nonempty provider message id. Distinct from the bounded shadow
lifecycle sender.

This module is reachable ONLY from the production pension reminder worker
behind production mode plus the separate reminder activation flag.
It is NEVER used by shadow lifecycle, replay, or fixtures.
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request


def send_pension_reminder(
    *,
    body_text: str,
    bot_token: str,
    chat_id: str,
) -> str:
    """Send one pension-reminder message via Telegram Bot API.

    Returns the provider message_id (Telegram message_id as string).

    Uses stdlib HTTPS only. No third-party HTTP library.
    Raises TimeoutError on network timeout, Exception on any other failure.
    """
    if not body_text or not bot_token or not chat_id:
        raise ValueError("missing required parameters")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": body_text,
        }
    ).encode("utf-8")
    context = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10, context=context) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                raise Exception(f"telegram error: {result.get('description')}")
            return str(result["result"]["message_id"])
    except TimeoutError:
        raise
    except Exception:
        raise
