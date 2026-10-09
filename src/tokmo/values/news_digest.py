"""Morning digest eligibility and composition (2026-08-22 repair + M4).

A non-urgent digest item must carry a meaningful event statement from
stored evidence or be omitted. The observed bug body
(`밤사이 최신 material 상태 통합: 📰 could 뉴스 동향 … 판정: 긴급 알림 아님 …`)
must never release again: verdict/target boilerplate, the legacy
integration prefix, and internal JSON all fail closed here.

M4: one morning release cycle folds every eligible overnight episode into
at most ONE Telegram-visible digest body — ``compose_morning_digest`` is
the pure bounded composer the release seam calls.
"""

DIGEST_STATEMENT_PREFIX = "사건: "

FORBIDDEN_DIGEST_MARKERS = ("밤사이 최신 material 상태 통합:", "판정:", "대상:")

# Bounded combined digest: at most this many episode items and roughly this
# many characters per released digest body (Telegram hard cap is 4096).
MORNING_DIGEST_MAX_ITEMS = 8
MORNING_DIGEST_MAX_CHARS = 3200
MORNING_DIGEST_HEADING = "🌅 아침 뉴스 다이제스트"


def morning_digest_eligible(body) -> bool:
    if type(body) is not str:
        return False
    text = body.strip()
    if not text or text.startswith("{"):
        return False
    if any(marker in text for marker in FORBIDDEN_DIGEST_MARKERS):
        return False
    return any(
        line.strip().startswith(DIGEST_STATEMENT_PREFIX)
        and line.strip()[len(DIGEST_STATEMENT_PREFIX) :].strip()
        for line in text.splitlines()
    )


def _digest_item(body: str) -> str:
    """One bounded item: the held body's heading plus its event statement.
    Evidence counts stay out of the combined digest — the statement is the
    meaningful part; full held bytes remain durable in ``alert_revisions``."""
    lines = [line.strip() for line in body.strip().splitlines() if line.strip()]
    statement = next(line for line in lines if line.startswith(DIGEST_STATEMENT_PREFIX))
    heading = lines[0] if lines and lines[0] != statement else ""
    return f"{heading}\n{statement}" if heading else statement


def compose_morning_digest(bodies) -> "tuple[str, int]":
    """Fold ordered eligible held bodies into one bounded digest.

    Returns ``(digest_body, used)`` where ``used`` is how many leading
    bodies were folded; the release seam suppresses the overflow with
    audit provenance (one morning digest per cycle, never one per tick).
    Every input must already be ``morning_digest_eligible`` — anything
    else is a caller bug and fails closed with ``ValueError``.
    """
    bodies = list(bodies)
    if not bodies or any(not morning_digest_eligible(body) for body in bodies):
        raise ValueError("compose_morning_digest requires eligible bodies")
    if len(bodies) == 1:
        # A single overnight episode keeps its held bytes untouched.
        return bodies[0], 1
    items = []
    length = len(MORNING_DIGEST_HEADING) + 8
    for body in bodies[:MORNING_DIGEST_MAX_ITEMS]:
        item = _digest_item(body)
        if items and length + len(item) > MORNING_DIGEST_MAX_CHARS:
            break
        items.append(item)
        length += len(item) + 2
    heading = f"{MORNING_DIGEST_HEADING} · {len(items)}건"
    return "\n\n".join([heading, *items]), len(items)
