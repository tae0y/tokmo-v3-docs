"""NEWS health notices use the existing private Stock bot credential file."""
import os
from pathlib import Path
import stat


def build_stock_health_sender(path, send_message):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()):
        raise ValueError('unsafe stock health credential file')
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in {'TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'} or key in values or not value:
            raise ValueError('invalid stock health credential file')
        values[key] = value
    if set(values) != {'TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'}:
        raise ValueError('incomplete stock health credential file')

    def send(body):
        receipt = send_message(body_text=body, bot_token=values['TELEGRAM_BOT_TOKEN'],
                               chat_id=values['TELEGRAM_CHAT_ID'])
        if receipt is None or not str(receipt).isdigit():
            raise ValueError('stock health delivery receipt missing')
        return str(receipt)
    return send
