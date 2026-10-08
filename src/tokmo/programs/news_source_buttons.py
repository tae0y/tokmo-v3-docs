"""News-only URL presentation; no fetching, callback, or transport ownership."""
import json
import re
from urllib.parse import urlsplit

MARKER = 'news_source_buttons:'
_SOURCE = re.compile(r'(?m)^(출처: ([^\n]+))\n(https?://[^\s]+)[ \t]*$')


def numbered_buttons(buttons, number):
    """Label each article's sources; preserve URLs and the caller's metadata."""
    aliases = {'연합뉴스': '연합', 'DART': 'DART공시', 'OpenDART': 'DART공시'}
    result = []
    for index, button in enumerate(buttons, 1):
        publisher = button['text'].split(' · ', 1)[-1]
        label = f'{number}-{index}' if len(buttons) > 1 else str(number)
        result.append(dict(button, text=f'{label} · {aliases.get(publisher, publisher)}'))
    return result


def source_buttons(body, *, number=1):
    """Move rendered citation URL lines, retaining publisher/time text verbatim."""
    buttons = []
    seen = set()
    def replace(match):
        url = match[3]
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid:
            return match[0]
        if url not in seen:
            seen.add(url)
            publisher = match[2].split(' · ', 1)[0][:28]
            buttons.append({'text': f'원문 {len(buttons)+1} · {publisher}', 'url': url})
        return match[1]
    text = _SOURCE.sub(replace, body)
    return text, numbered_buttons(buttons, number)


def source_markup(outbox, existing):
    buttons = [button for part in outbox.body_evidence if part.startswith(MARKER)
               for button in json.loads(part[len(MARKER):])]
    if not buttons:
        return existing
    return dict(existing or {}, inline_keyboard=list((existing or {}).get('inline_keyboard', []))
                + [buttons[index:index+2] for index in range(0, len(buttons), 2)])
