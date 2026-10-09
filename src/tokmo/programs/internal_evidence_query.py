"""Internal-first evidence query, Stage 1 (Plan/SDS 2026-09-04).

Bounded, read-only lookup over the local stores that already exist:

- ``news_raw``      canonical n5 NEWS documents (``news_documents``),
- ``news_episode``  linked episode metadata — explicitly SECONDARY,
- ``opendart``      the local OpenDART disclosure store, KR symbols only.

Query terms are expanded through the existing public NEWS alias catalog
plus caller-supplied ticker/official identity names; nothing is ever
added to the catalog. Matching reuses the boundary-safe discovery matcher
(``하닉`` attaches at a Korean token head only — never inside ``지하닉스``).

Every store degradation (absent, malformed, locked, schema mismatch,
not configured) is a typed limitation in the result; the caller is never
handed a single sufficiency/permission boolean, and nothing here blocks a
later external search. No file is created: SQLite opens with URI
``mode=ro`` only after ``is_file()``, never ``immutable`` (WAL content
must stay visible), and every failure to obtain a consistent read is a
limitation rather than a guess.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from tokmo.programs.news_topic_discovery import (
    _PUBLIC_ALIASES,
    _contains_term,
    _normalise,
    _tokens,
)

CONTRACT_ID = "internal-evidence-query-v1"
SUPPORTED_FAMILIES = ("news_raw", "news_episode", "opendart")
MAX_LIMIT = 200
MAX_SPAN_DAYS = 92
SUMMARY_EXCERPT_CHARS = 200
# Mechanical cursor batch sizes — implementation details, never a product
# cap: the whole validated window is always scanned and counted.
FETCH_BATCH_ROWS = 500
SYMBOL_BATCH = 100
_KR_SYMBOL = re.compile(r"[0-9]{6}")
_COMPACT_DAY = re.compile(r"[0-9]{8}")
_GOOGLE_HOSTS = ("news.google.com",)


class _MalformedStoreRow(Exception):
    """A stored row violates the seam's shape (typed STORE_MALFORMED)."""


@dataclass(frozen=True)
class EvidenceQueryV1:
    """Validated query: inclusive UTC calendar days, bounded span/limit."""

    date_from: date
    date_to: date
    terms: tuple = ()
    symbols: tuple = ()
    # ((symbol, (official name, ...)), ...) — caller-grounded identities.
    identities: tuple = ()
    families: tuple = SUPPORTED_FAMILIES
    limit: int = 20

    def __post_init__(self):
        if self.date_from > self.date_to:
            raise ValueError("DATE_RANGE_INVERTED")
        if (self.date_to - self.date_from).days > MAX_SPAN_DAYS:
            raise ValueError("DATE_SPAN_EXCEEDED")
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError("LIMIT_OUT_OF_RANGE")
        if not any(str(item).strip() for item in (*self.terms, *self.symbols)):
            raise ValueError("NO_QUERY_INPUT")
        if not self.families:
            raise ValueError("NO_FAMILY")


# ------------------------------------------------------------- expansion


def _catalog_keys(text):
    """Keys whose alias equals the input as a whole token sequence
    (``SK 하이닉스`` == ``SK하이닉스``); never a partial or head match."""
    tokens = _tokens(str(text))
    if not tokens:
        return []
    return sorted(
        key
        for key, aliases in _PUBLIC_ALIASES.items()
        if any(_tokens(alias) == tokens for alias in aliases)
    )


def expand_query_terms(*, terms=(), symbols=(), identities=()):
    """Terms/symbols/identity names → catalog entity keys + expanded terms.

    A literal input always stays a search term. An input equal to exactly
    one public alias pulls in that entity's whole alias set (the 000660 +
    SK하이닉스 pair merges into ``ent:sk-hynix``, never a new key). Symbols
    with neither a catalog match nor supplied identity names are reported
    as ungrounded and searched literally only — no alias is invented. A
    supplied name that equals aliases of several entities stays ambiguous
    (fail closed): it never grounds its symbol and never expands."""
    identity_names = {
        str(symbol): tuple(str(name) for name in names) for symbol, names in identities
    }
    keys, expanded, ungrounded, ambiguous = set(), set(), [], []
    inputs = [(str(term), None) for term in terms]
    inputs += [(str(symbol), str(symbol)) for symbol in symbols]
    inputs += [
        (name, symbol) for symbol, names in identity_names.items() for name in names
    ]
    grounded_symbols = set()
    for text, symbol in inputs:
        normalized = _normalise(text)
        if not normalized:
            continue
        expanded.add(normalized)
        matched = _catalog_keys(text)
        if len(matched) == 1:
            keys.add(matched[0])
            expanded.update(_PUBLIC_ALIASES[matched[0]])
            if symbol is not None:
                grounded_symbols.add(symbol)
        elif len(matched) > 1:
            ambiguous.append(text)
        elif symbol is not None and symbol != text:
            # A caller-supplied official name outside the catalog grounds
            # its symbol as a literal term (AVGO=Broadcom).
            grounded_symbols.add(symbol)
    for symbol in symbols:
        symbol = str(symbol)
        if symbol.strip() and symbol not in grounded_symbols:
            ungrounded.append(symbol)
    return {
        "entity_keys": sorted(keys),
        "expanded_terms": sorted(expanded),
        "ungrounded_inputs": sorted(dict.fromkeys(ungrounded)),
        "ambiguous_inputs": sorted(dict.fromkeys(ambiguous)),
    }


def classify_url_kind(url):
    """``GOOGLE_REDIRECT`` marks a Google News locator: discovery evidence
    only — the publisher original must be identified through publisher
    name/title/domain, never by treating the redirect as the original."""
    try:
        host = (urlsplit(str(url)).hostname or "").casefold()
    except ValueError:
        return "DIRECT"
    if any(host == item or host.endswith("." + item) for item in _GOOGLE_HOSTS):
        return "GOOGLE_REDIRECT"
    return "DIRECT"


# ------------------------------------------------------- store opening


def _failure_code(error):
    message = str(error).casefold()
    if "unable to open" in message:
        return "STORE_OPEN_FAILED"
    if "locked" in message or "busy" in message:
        return "STORE_BUSY_OR_LOCKED"
    if "no such table" in message or "no such column" in message:
        return "SCHEMA_MISMATCH"
    return "STORE_MALFORMED"


def _open_readonly(path):
    """(connection, None) or (None, limitation code). Never creates."""
    if path is None:
        return None, "STORE_NOT_CONFIGURED"
    path = Path(path)
    if not path.is_file():
        return None, "STORE_ABSENT"
    uri = f"file:{quote(str(path.absolute()), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.25, isolation_level=None)
    except sqlite3.Error as error:
        return None, _failure_code(error)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=250")
        if connection.execute("PRAGMA query_only").fetchone() != (1,):
            raise sqlite3.OperationalError("query_only unavailable")
    except sqlite3.Error as error:
        connection.close()
        return None, _failure_code(error)
    return connection, None


def _schema_ok(connection, expected_schema_id):
    """n5/episode stores carry ``schema_meta.schema_id``; the OpenDART
    store (``None``) is recognised by its ``disclosures`` table."""
    if expected_schema_id is None:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='disclosures'"
            ).fetchone()
            is not None
        )
    row = connection.execute(
        "SELECT schema_id FROM schema_meta WHERE singleton=1"
    ).fetchone()
    return row is not None and row[0] == expected_schema_id


def _run_family(store_path, expected_schema_id, reader):
    """Open → verify schema → read; every degradation is (None, code)."""
    connection, code = _open_readonly(store_path)
    if connection is None:
        return None, code
    try:
        try:
            if not _schema_ok(connection, expected_schema_id):
                return None, "SCHEMA_MISMATCH"
            return reader(connection), None
        except sqlite3.OperationalError as error:
            return None, _failure_code(error)
        except (sqlite3.DatabaseError, _MalformedStoreRow):
            return None, "STORE_MALFORMED"
    finally:
        connection.close()


# ------------------------------------------------------------- readers


def _day_start(day):
    return f"{day.isoformat()}T00:00:00"


def _match(text, expanded_terms):
    tokens = _tokens(text)
    return [term for term in expanded_terms if _contains_term(tokens, term)]


def _batches(cursor, size):
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield from rows


def _read_news_raw(connection, query, expanded_terms, row_acceptor=None):
    """Scan the WHOLE validated window newest-first (mechanical batches),
    count every match, and keep only the newest ``limit`` rows."""
    lower = _day_start(query.date_from)
    upper = _day_start(query.date_to + timedelta(days=1))
    cursor = connection.execute(
        "SELECT news_document_id,source_id,canonical_url,publisher_name,"
        "publisher_site_url,title,summary_text,evidence_role,content_mode,"
        "published_at,first_seen_at,last_seen_at,revision_no"
        " FROM news_documents"
        " WHERE substr(COALESCE(published_at,first_seen_at),1,19)>=?"
        " AND substr(COALESCE(published_at,first_seen_at),1,19)<?"
        " ORDER BY COALESCE(published_at,first_seen_at) DESC,"
        " news_document_id ASC",
        (lower, upper),
    )
    scanned = matched_count = 0
    returned = []
    for row in _batches(cursor, FETCH_BATCH_ROWS):
        scanned += 1
        (
            doc_id,
            source_id,
            url,
            publisher,
            site,
            title,
            summary,
            role,
            mode,
            published,
            first_seen,
            last_seen,
            revision,
        ) = row
        matched = _match(f"{title} {summary or ''}", expanded_terms)
        if not matched:
            continue
        # Optional per-row acceptance predicate (M1 seam): when supplied, a
        # row enters the result (and the cap arithmetic) only if the caller
        # accepts it — the exact-instrument acceptance therefore applies
        # BEFORE the result limit, so loose discovery matches can never
        # crowd out a valid older same-entity article. Default None keeps
        # the generic Stage-1 semantics for every non-Quant caller.
        if row_acceptor is not None and not row_acceptor(f"{title} {summary or ''}"):
            continue
        matched_count += 1
        if len(returned) >= query.limit:
            continue
        returned.append(
            {
                "document_id": doc_id,
                "title": title,
                "summary_excerpt": (
                    " ".join(str(summary).split())[:SUMMARY_EXCERPT_CHARS]
                    if summary
                    else None
                ),
                "url": url,
                "url_kind": classify_url_kind(url),
                "publisher_name": publisher,
                "publisher_site_url": site,
                "source_id": source_id,
                "evidence_role": role,
                "content_mode": mode,
                "published_at": published,
                "first_seen_at": first_seen,
                "last_seen_at": last_seen,
                "revision_no": revision,
                "matched_terms": matched,
                "provenance": {
                    "store": "news_raw",
                    "table": "news_documents",
                    "document_id": doc_id,
                },
            }
        )
    return {
        "documents": returned,
        "counts": {
            "scanned": scanned,
            "matched": matched_count,
            "returned": len(returned),
            "truncated": matched_count - len(returned),
        },
    }


def _read_news_episode(connection, query, document_ids, entity_keys):
    episodes = {}
    if document_ids:
        marks = ",".join("?" for _ in document_ids)
        for row in connection.execute(
            "SELECT e.episode_id,e.title,e.domain,e.state,e.event_class,"
            "e.revision,e.first_seen_at,e.updated_at,"
            "COALESCE(ev.news_document_id,ev.story_key),ev.claim_status,"
            "ev.source_kind FROM episode_evidence ev"
            " JOIN episodes e ON e.episode_id=ev.episode_id"
            f" WHERE ev.news_document_id IN ({marks})"
            f" OR ev.story_key IN ({marks})"
            " ORDER BY e.episode_id,ev.story_key,ev.claim_status",
            (*document_ids, *document_ids),
        ):
            item = episodes.setdefault(
                row[0],
                {
                    "episode_id": row[0],
                    "title": row[1],
                    "domain": row[2],
                    "state": row[3],
                    "event_class": row[4],
                    "revision": row[5],
                    "first_seen_at": row[6],
                    "updated_at": row[7],
                    "document_ids": [],
                    "claim_statuses": [],
                    "source_kinds": [],
                },
            )
            for field, value in (
                ("document_ids", row[8]),
                ("claim_statuses", row[9]),
                ("source_kinds", row[10]),
            ):
                if value not in item[field]:
                    item[field].append(value)
    candidates = []
    if entity_keys:
        marks = ",".join("?" for _ in entity_keys)
        lower = _day_start(query.date_from)
        upper = _day_start(query.date_to + timedelta(days=1))
        rows = connection.execute(
            "SELECT candidate_id,run_id,lane,entity_key,phrase_text,"
            "distinct_story_count,distinct_source_count,state,episode_id,"
            "created_at FROM discovery_candidates"
            f" WHERE entity_key IN ({marks})"
            " AND substr(created_at,1,19)>=? AND substr(created_at,1,19)<?"
            " ORDER BY created_at DESC,candidate_id ASC",
            (*entity_keys, lower, upper),
        ).fetchmany(query.limit + 1)
        candidates = [
            dict(
                zip(
                    (
                        "candidate_id",
                        "run_id",
                        "lane",
                        "entity_key",
                        "phrase_text",
                        "distinct_story_count",
                        "distinct_source_count",
                        "state",
                        "episode_id",
                        "created_at",
                    ),
                    row,
                )
            )
            for row in rows[: query.limit]
        ]
        candidates_truncated = len(rows) > query.limit
    else:
        candidates_truncated = False
    for item in episodes.values():
        for field in ("document_ids", "claim_statuses", "source_kinds"):
            item[field].sort()
    return {
        "linked_episodes": [episodes[key] for key in sorted(episodes)],
        "discovery_candidates": candidates,
        "candidates_truncated": candidates_truncated,
    }


def _read_opendart(connection, query, kr_symbols):
    """Deduplicated KR symbols are queried in bounded SQL-variable batches
    (never a 'too many SQL variables' error mislabeled as corruption);
    rows are merged and ordered deterministically."""
    window = (query.date_from.strftime("%Y%m%d"), query.date_to.strftime("%Y%m%d"))
    rows = []
    for start in range(0, len(kr_symbols), SYMBOL_BATCH):
        batch = kr_symbols[start : start + SYMBOL_BATCH]
        marks = ",".join("?" for _ in batch)
        rows.extend(
            connection.execute(
                "SELECT rcept_no,corp_code,corp_name,stock_code,report_nm,"
                "flr_nm,rcept_dt,first_observed_at,first_run_id FROM disclosures"
                f" WHERE stock_code IN ({marks}) AND rcept_dt>=? AND rcept_dt<=?",
                (*batch, *window),
            ).fetchall()
        )
    for row in rows:
        if not isinstance(row[6], str) or not _COMPACT_DAY.fullmatch(row[6]):
            raise _MalformedStoreRow("rcept_dt")
    rows.sort(key=lambda row: (row[6], row[0]), reverse=True)
    returned = rows[: query.limit]
    disclosures = [
        {
            "rcept_no": row[0],
            "corp_code": row[1],
            "corp_name": row[2],
            "stock_code": row[3],
            "report_nm": row[4],
            "flr_nm": row[5],
            "rcept_dt": f"{row[6][:4]}-{row[6][4:6]}-{row[6][6:8]}",
            "first_observed_at": row[7],
            "provenance": {
                "store": "opendart",
                "table": "disclosures",
                "rcept_no": row[0],
                "first_run_id": row[8],
            },
        }
        for row in returned
    ]
    return {
        "disclosures": disclosures,
        "counts": {
            "matched": len(rows),
            "returned": len(returned),
            "truncated": len(rows) - len(returned),
        },
    }


# ------------------------------------------------------------ assembly


def run_internal_evidence_query(
    query, *, news_db=None, episode_db=None, opendart_db=None, news_row_acceptor=None
):
    """JSON-safe, deterministic result. Never raises on store state;
    argument validation lives in :class:`EvidenceQueryV1`.

    ``news_row_acceptor`` (optional, single ``text`` argument over the
    row's title + summary) is the narrow Quant seam for exact
    instrument/entity acceptance BEFORE the ``news_raw`` result limit
    (loose discovery matches may not crowd out a valid same-entity
    article); the default keeps generic Stage-1 semantics for every
    other caller."""
    limitations = []

    def limit(family, code, detail=""):
        limitations.append({"family": family, "code": code, "detail": str(detail)})

    expansion = expand_query_terms(
        terms=query.terms, symbols=query.symbols, identities=query.identities
    )
    for symbol in expansion["ungrounded_inputs"]:
        limit("expansion", "IDENTITY_UNGROUNDED", symbol)
    for text in expansion["ambiguous_inputs"]:
        limit("expansion", "ALIAS_AMBIGUOUS", text)
    families = {}
    requested = tuple(dict.fromkeys(str(item) for item in query.families))
    for family in requested:
        if family not in SUPPORTED_FAMILIES:
            limit(family, "UNSUPPORTED_FAMILY", family)

    document_ids = ()
    if "news_raw" in requested:
        payload, code = _run_family(
            news_db,
            "external-news-n5",
            lambda connection: _read_news_raw(
                connection, query, expansion["expanded_terms"], news_row_acceptor
            ),
        )
        if payload is None:
            limit("news_raw", code)
            families["news_raw"] = {
                "store_health": code,
                "documents": [],
                "counts": {"scanned": 0, "matched": 0, "returned": 0, "truncated": 0},
            }
        else:
            if payload["counts"]["truncated"]:
                limit(
                    "news_raw", "RESULT_CAP_TRUNCATED", payload["counts"]["truncated"]
                )
            if any(
                doc["url_kind"] == "GOOGLE_REDIRECT" for doc in payload["documents"]
            ):
                limit(
                    "news_raw",
                    "GOOGLE_REDIRECT_DISCOVERY_ONLY",
                    sum(
                        doc["url_kind"] == "GOOGLE_REDIRECT"
                        for doc in payload["documents"]
                    ),
                )
            families["news_raw"] = {"store_health": "OK", **payload}
            document_ids = tuple(doc["document_id"] for doc in payload["documents"])

    if "news_episode" in requested:
        base = {"role": "SECONDARY", "linked_episodes": [], "discovery_candidates": []}
        if not document_ids and not expansion["entity_keys"]:
            limit("news_episode", "NO_LINK_INPUTS")
            families["news_episode"] = {"store_health": "NO_LINK_INPUTS", **base}
        else:
            payload, code = _run_family(
                episode_db,
                "news-episode-v1",
                lambda connection: _read_news_episode(
                    connection, query, document_ids, expansion["entity_keys"]
                ),
            )
            if payload is None:
                limit("news_episode", code)
                families["news_episode"] = {"store_health": code, **base}
            else:
                if payload.pop("candidates_truncated"):
                    limit(
                        "news_episode", "RESULT_CAP_TRUNCATED", "discovery_candidates"
                    )
                families["news_episode"] = {
                    "store_health": "OK",
                    "role": "SECONDARY",
                    **payload,
                }

    if "opendart" in requested:
        distinct = tuple(dict.fromkeys(str(symbol) for symbol in query.symbols))
        kr_symbols = tuple(
            symbol for symbol in distinct if _KR_SYMBOL.fullmatch(symbol)
        )
        for symbol in distinct:
            if symbol not in kr_symbols:
                limit("opendart", "NON_KR_SYMBOL_SKIPPED", symbol)
        empty = {
            "disclosures": [],
            "counts": {"matched": 0, "returned": 0, "truncated": 0},
        }
        if not kr_symbols:
            limit("opendart", "NO_KR_SYMBOL")
            families["opendart"] = {"store_health": "NO_KR_SYMBOL", **empty}
        else:
            payload, code = _run_family(
                opendart_db,
                None,
                lambda connection: _read_opendart(connection, query, kr_symbols),
            )
            if payload is None:
                limit("opendart", code)
                families["opendart"] = {"store_health": code, **empty}
            else:
                if payload["counts"]["truncated"]:
                    limit(
                        "opendart",
                        "RESULT_CAP_TRUNCATED",
                        payload["counts"]["truncated"],
                    )
                families["opendart"] = {"store_health": "OK", **payload}

    limitations.sort(key=lambda item: (item["family"], item["code"], item["detail"]))
    return {
        "contract": CONTRACT_ID,
        "query": {
            "date_from": query.date_from.isoformat(),
            "date_to": query.date_to.isoformat(),
            "terms": [str(item) for item in query.terms],
            "symbols": [str(item) for item in query.symbols],
            "identities": {
                str(symbol): [str(name) for name in names]
                for symbol, names in query.identities
            },
            "families": list(requested),
            "limit": query.limit,
        },
        "expansion": expansion,
        "families": {key: families[key] for key in sorted(families)},
        "limitations": limitations,
        "residual_gaps": _residual_gaps(requested, families, expansion, document_ids),
    }


def _residual_gaps(requested, families, expansion, document_ids):
    gaps = []
    for family in SUPPORTED_FAMILIES:
        if family not in requested:
            gaps.append(f"{family}: not queried")
        elif families[family]["store_health"] != "OK":
            gaps.append(
                f"{family}: no consistent local read "
                f"({families[family]['store_health']})"
            )
    episode = families.get("news_episode")
    if episode and episode["store_health"] == "OK" and document_ids:
        linked = {
            doc for item in episode["linked_episodes"] for doc in item["document_ids"]
        }
        unlinked = len([doc for doc in document_ids if doc not in linked])
        if unlinked:
            gaps.append(
                f"news_episode: {unlinked} returned raw documents "
                "have no linked episode metadata"
            )
    if expansion["ungrounded_inputs"]:
        gaps.append(
            "expansion: ungrounded symbols searched literally only: "
            + ",".join(expansion["ungrounded_inputs"])
        )
    gaps.append(
        "external: no external search performed; Stage 1.5 "
        "targeted discovery remains available to the caller"
    )
    return gaps
