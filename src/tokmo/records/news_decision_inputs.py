"""Read-only source adapters. Availability is collection time, not publication."""
import hashlib
import json
from pathlib import Path
import sqlite3

from tokmo.records.news_decision_store import kst, kst_field, timestamp
from tokmo.values.sec_document import sec_document_url


def _open(path):
    connection = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _page(items, since, until, limit):
    if limit < 1:
        raise ValueError("limit must be positive")
    items = sorted((item for item in items if timestamp(since) < timestamp(item["observed_at"]) <= timestamp(until)),
                   key=lambda item: (timestamp(item["observed_at"]), item["id"]))
    if len(items) > limit:
        boundary = timestamp(items[limit - 1]["observed_at"])
        page = [item for item in items if timestamp(item["observed_at"]) <= boundary]
    else:
        page = items
    more = len(page) < len(items)
    return {"observations": page, "status": "TRUNCATED" if more else "OK", "has_more": more,
            "next_since": page[-1]["observed_at"] if more else kst(until)}


def read_news_observations(path, *, since, until, limit=200):
    try:
        connection = _open(path)
        try:
            # Revised documents have no historical body versions in this DB.
            # SQLite rounds fractional seconds. Include the boundary bucket;
            # _page compares exact instants and removes the true old rows.
            rows = connection.execute("SELECT * FROM news_documents WHERE julianday(last_seen_at)>=julianday(?) OR julianday(first_seen_at)>=julianday(?)", (timestamp(since).isoformat(), timestamp(since).isoformat())).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return {"observations": [], "status": "UNAVAILABLE", "has_more": False, "next_since": timestamp(since).isoformat()}
    items = []
    for row in map(dict, rows):
        revised = (row.get("revision_no") or 1) > 1
        available = max(timestamp(row[key]) for key in ("first_seen_at", "last_seen_at", "updated_at") if row.get(key)) if revised else timestamp(row["first_seen_at"])
        key = "news:" + row["news_document_id"] + ":" + str(row.get("revision_no") or 1)
        # Named time fields only are re-expressed in KST; title/text/url/ids stay as collected.
        items.append({"id": key, "observed_at": kst(available), "published_at": kst_field(row.get("published_at")),
                      "title": row["title"], "text": row.get("body_text") or row.get("summary_text") or "",
                      "source_kind": "NEWS", "publisher": row.get("publisher_name") or row["source_id"],
                      "url": row.get("canonical_url"), "source_id": row["source_id"],
                      "history_scope": "current_revision_only" if revised else "original_collection",
                      "first_seen_at": kst_field(row["first_seen_at"]), "time_interpretation": row.get("time_interpretation")})
    return _page(items, since, until, limit)


def read_disclosure_observations(path, *, since, until, limit=200):
    try:
        connection = _open(path)
        try:
            rows = connection.execute("SELECT * FROM disclosures").fetchall()
            revisions = [dict(row) for row in connection.execute("SELECT * FROM dart_revisions ORDER BY revision_id")]
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return {"observations": [], "status": "UNAVAILABLE", "has_more": False, "next_since": timestamp(since).isoformat()}
    items = []
    by_receipt = {}
    for revision in revisions:
        by_receipt.setdefault(revision["rcept_no"], []).append(revision)
    for row in map(dict, rows):
        changes = by_receipt.get(row["rcept_no"], [])
        first = changes[0].get("previous_canonical_json") if changes else row["canonical_json"]
        versions = [(row["first_observed_at"], first, "first")] if first else []
        versions.extend((change["observed_at"], change.get("observed_canonical_json"), str(change["revision_id"])) for change in changes)
        for observed, payload, version in versions:
            if not payload:
                continue  # Missing historical body cannot be reconstructed honestly.
            value = json.loads(payload)
            digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
            # Date-only publication_date stays a date; no time of day is invented for it.
            items.append({"id": "dart:" + row["rcept_no"] + ":" + version + ":" + digest,
                "observed_at": kst(observed), "published_at": None, "publication_date": row["rcept_dt"],
                "title": value.get("corp_name", row["corp_name"]) + " " + value.get("report_nm", row["report_nm"]),
                "text": json.dumps(value, ensure_ascii=False), "source_kind": "DISCLOSURE", "publisher": "DART",
                "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + row["rcept_no"],
                "content_scope": "listing_only", "body_verified": False, "stock_code": row.get("stock_code")})
    return _page(items, since, until, limit)


def read_sec_observations(path, *, since, until, limit=200):
    """Existing SEC targeted evidence, read-only. Accession identity never implies issuer CIK.

    Only collection/storage availability advances the cursor. A filing date is not an exact
    publication time, and a submissions metadata row is not the filing's verified body.
    Provenance widening (HOLDING -> combined) does not create a new news observation.
    """
    unavailable = {"observations": [], "status": "UNAVAILABLE", "has_more": False, "next_since": kst(since),
                   "coverage": "targeted_universe_only"}
    try:
        connection = _open(path)
        try:
            connection.execute("BEGIN")
            metadata = dict(connection.execute("SELECT key,value FROM storage_metadata"))
            if metadata.get("sec_targeted_schema_version") != "2" or metadata.get("sec_targeted_adapter") != "tokmo-sec-targeted-next":
                return dict(unavailable, reason="SEC_SCHEMA_UNSUPPORTED")
            rows = connection.execute("""SELECT f.*,c.accession_number AS conflicting FROM filings f
                LEFT JOIN conflicts c ON c.accession_number=f.accession_number
                WHERE max(julianday(f.observed_at),julianday(f.stored_at))>=julianday(?)
                  AND max(julianday(f.observed_at),julianday(f.stored_at))<=julianday(?)
                ORDER BY max(julianday(f.observed_at),julianday(f.stored_at)),f.accession_number""",
                (kst(since), kst(until))).fetchall()
            # SQLite rounds Julian days below millisecond precision; include the boundary
            # and let _page compare exact instants so microsecond-neighbor rows are not lost.
            invalid_times = connection.execute("SELECT count(*) FROM filings WHERE julianday(observed_at) IS NULL OR julianday(stored_at) IS NULL").fetchone()[0]
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return dict(unavailable, reason="SEC_STORE_UNAVAILABLE")
    if invalid_times:
        return dict(unavailable, reason="SEC_TIME_INVALID")
    items = []
    try:
        for row in map(dict, rows):
            available = max(timestamp(row["observed_at"]), timestamp(row["stored_at"]))
            if not timestamp(since) < available <= timestamp(until):
                continue
            if row["conflicting"] is not None:
                return dict(unavailable, reason="SEC_FILING_CONFLICT")
            accession, cik, document = row["accession_number"], row["cik"], row["primary_document"]
            if row["source"] != "sec_submissions":
                return dict(unavailable, reason="SEC_IDENTITY_INVALID")
            # Form, issuer, date and document path are metadata facts; do not infer a filing's claims.
            content = {key: row[key] for key in ("accession_number", "cik", "company_name", "form", "filing_date", "report_date", "primary_document")}
            items.append({"id": "sec:" + accession + ":metadata", "observed_at": kst(available), "published_at": None,
                "publication_date": row["filing_date"], "report_date": row["report_date"],
                "title": row["company_name"] + " " + row["form"] + " 공시", "text": json.dumps(content, ensure_ascii=False),
                "source_kind": "DISCLOSURE", "source_system": "SEC", "publisher": "SEC EDGAR",
                "url": sec_document_url(cik, accession, document), "primary_document": document,
                "content_scope": "listing_only", "body_verified": False, "cik": cik, "accession_number": accession,
                "form": row["form"], "stock_code": row["ticker"]})
    except (ValueError, TypeError, KeyError, AttributeError):
        return dict(unavailable, reason="SEC_ROW_INVALID")
    return dict(_page(items, since, until, limit), coverage="targeted_universe_only")


def _read_holdings_context(path, now):
    try:
        value = json.loads(Path(path).read_text())
        captured = timestamp(value["captured_at"])
        if captured > timestamp(now):
            return {"portfolio_status": "UNAVAILABLE_AT_THIS_TIME"}
        stale = (timestamp(now) - captured).total_seconds() > 86400
        holdings = [dict({key: holding.get(key) for key in ("symbol", "quantity", "avg_price", "market_raw", "currency")},
                        account='account-' + str(index),
                        scope='retirement' if str(account.get('account_scope', '')).startswith('pension-excel-') else 'personal')
                    for index, account in enumerate(value["accounts"], 1) for holding in account["holdings"]]
        return {"portfolio_status": "STALE" if stale else "OK", "observed_at": kst(captured), "holdings": holdings,
                "cash_status": "NOT_PROVIDED", "user_intent": "NOT_PROVIDED"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"portfolio_status": "UNAVAILABLE"}


def read_portfolio_context(path, now, *, followup_path=None):
    value=_read_holdings_context(path,now)
    if followup_path is not None:
        from tokmo.records.investment_followup import read_news_context
        try:
            value['investment_followup']=read_news_context(followup_path,at=now)
            if value['investment_followup']['choices']:value['user_intent']='RECORDED_IN_INVESTMENT_FOLLOWUP'
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
            value['investment_followup']={'status':'UNAVAILABLE','choices':[],'delivered_reviews':[]}
    return value
