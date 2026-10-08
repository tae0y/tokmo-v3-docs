"""Select a NEWS-only destination; the existing dialogue sender is unchanged."""
import json
from pathlib import Path
import re
import stat


class NewsRouteError(ValueError):
    """Local route validation failed before any transport was invoked."""


def read_group_identity(path):
    """Reuse the existing private Hermes env file; retain only its bot token."""
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("news group identity must be a private regular file")
    values = []
    for raw in path.read_text().splitlines():
        key, separator, value = raw.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == "TELEGRAM_BOT_TOKEN":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if len(values) != 1 or not values[0]:
        raise ValueError("news group identity missing or ambiguous")
    return values[0]


def build_news_sender(root, stock_sender, send_message):
    """No route file means the original sender. Route errors affect NEWS only.

    Read credentials only when sending. Group failures never fall back to the
    Stock bot. Qualify receipts by chat so message ids from two bots cannot
    collide in the existing news-delivery records.
    """
    route_path = Path(root) / "news_delivery_route.json"
    if not route_path.exists():
        return stock_sender

    def send(text, reply_markup=None):
        try:
            route = json.loads(route_path.read_text())
            if route.get("destination") != "TOKMO_GROUP":
                raise ValueError("unsupported news destination")
            chat_id = route.get("chat_id")
            if not isinstance(chat_id, str) or not re.fullmatch(r"-[1-9][0-9]{0,19}", chat_id):
                raise ValueError("news group chat id required")
            token = read_group_identity(route["credential_file"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise NewsRouteError('톡모그룹 전달 경로/인증 파일 확인 필요') from error
        message_id = send_message(body_text=text, bot_token=token, chat_id=chat_id,
                                  reply_markup=reply_markup)
        return f"telegram-group:{chat_id}:{message_id}"

    return send
