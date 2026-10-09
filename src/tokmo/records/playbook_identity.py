"""Canonical instrument identity read from the Git-tracked playbook tree.

The playbook document is already the canonical, Toss-evidenced identity for
every instrument the portfolio holds: its front matter carries
``instrument_code`` and ``instrument_name``, and its path
``<root>/<scope>/<market>/<symbol>.md`` is the same path the trade-reason
append writes to. Reading the name here therefore adds no provider call and
introduces no second source of truth — the renderer's name and the document
it appends to are one and the same record.

Everything fails soft to ``None``: a missing document, an absent field, an
unreadable file, or an unmapped scope/market yields no name at all, and the
display contract then shows the canonical symbol alone.
"""

import re
from pathlib import Path

from tokmo.values.account_scope_patterns import (
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)

PLAYBOOK_MARKETS = frozenset({"KR", "US"})
# Symbols are canonical provider codes only: KR six-character codes and US
# tickers. Anything else (separators, traversal, empties) resolves to no path.
_SYMBOL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,31}")
_NAME_RE = re.compile(r'^instrument_name:\s*"([^"\n]+)"[ \t]*$', re.MULTILINE)
_FRONT_MATTER_OPEN = "---\n"

# Pure scope kinds (playbook subtrees) — not account identities. Account
# scopes map onto these through playbook_scope_dir(); the reverse direction
# (scope kind → document path) never fabricates an account-scope string.
PLAYBOOK_SCOPE_DIRS = ("personal", "retirement")


def playbook_scope_dir(account_scope) -> "str | None":
    """Account scope → playbook subtree; an unmapped scope has no subtree."""
    if type(account_scope) is not str:
        return None
    if ACCOUNT_SCOPE_RE.fullmatch(account_scope) is not None:
        return "personal"
    if PENSION_SCOPE_RE.fullmatch(account_scope) is not None:
        return "retirement"
    return None


def scope_dir_document_path(
    playbook_root, *, scope_dir: str, market: str, symbol: str
) -> "Path | None":
    """Scope kind → the one canonical document path, or None."""
    if playbook_root is None or scope_dir not in PLAYBOOK_SCOPE_DIRS:
        return None
    if market not in PLAYBOOK_MARKETS:
        return None
    if type(symbol) is not str or _SYMBOL_RE.fullmatch(symbol) is None:
        return None
    return Path(playbook_root) / scope_dir / market / f"{symbol}.md"


def instrument_document_path(
    playbook_root, *, account_scope: str, market: str, symbol: str
) -> "Path | None":
    """The one canonical document path, or None when it cannot be formed."""
    scope_dir = playbook_scope_dir(account_scope)
    if scope_dir is None:
        return None
    return scope_dir_document_path(
        playbook_root, scope_dir=scope_dir, market=market, symbol=symbol
    )


def _front_matter(text: str) -> str:
    if not text.startswith(_FRONT_MATTER_OPEN):
        return ""
    end = text.find("\n---", len(_FRONT_MATTER_OPEN))
    return text[len(_FRONT_MATTER_OPEN) : end] if end != -1 else ""


def read_instrument_name(
    playbook_root, *, account_scope: str, market: str, symbol: str
) -> "str | None":
    """The canonical ``instrument_name``; None whenever it is unavailable."""
    path = instrument_document_path(
        playbook_root, account_scope=account_scope, market=market, symbol=symbol
    )
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    match = _NAME_RE.search(_front_matter(text))
    if match is None:
        return None
    name = match.group(1).strip()
    return name or None
