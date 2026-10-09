"""Resolve a delivered digest number to its frozen judgment and sources, read-only."""

import json
import sqlite3
from contextlib import closing
from datetime import date as Date
from pathlib import Path

from tokmo.programs.news_decision_outbox import _message_receipt
from tokmo.records.news_decision_store import KST, timestamp


def _connect(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_digest_item(
    state, ops, number, *, date=None, digest_id=None, reply_receipt=None
):
    """No selector means the most recently delivered digest, never an unsent proposal.

    A partial delivery can be queried, but remains explicitly partial. Dates refer to
    the date printed in the digest, not the arrival date after a delivery delay.
    """
    if type(number) is not int or number < 1:
        raise ValueError("number must be a positive integer")
    if date is not None:
        Date.fromisoformat(date)
    groups = {}
    with closing(_connect(state)) as connection:
        rows = connection.execute(
            "SELECT id, json_extract(state,'$.queued_digest_entries') AS entries, "
            "json_extract(state,'$.delivery_refs') AS refs, json_extract(state,'$.decision') AS decision, "
            "json_extract(state,'$.updated_at') AS updated_at FROM news_decision_events "
            "WHERE json_type(state,'$.queued_digest_entries')='object'"
        )
        for row in rows:
            refs = json.loads(row["refs"] or "{}")
            for key, item in json.loads(row["entries"]).items():
                printed_date = (
                    timestamp(item["digest_slot"]).astimezone(KST).date().isoformat()
                )
                if (digest_id and digest_id != key) or (date and date != printed_date):
                    continue
                group = groups.setdefault(
                    key, dict(id=key, date=printed_date, entries={}, refs=set())
                )
                if item["number"] in group["entries"]:
                    raise ValueError("conflicting digest number")
                group["entries"][item["number"]] = dict(
                    item,
                    current_decision=json.loads(row["decision"] or "null"),
                    current_updated_at=row["updated_at"],
                )
                group["refs"].update(refs.get(key, []))
    delivered = []
    with closing(_connect(ops)) as connection:
        for group in groups.values():
            receipts = []
            for key in sorted(group["refs"]):
                row = connection.execute(
                    "SELECT status,delivered_at,provider_message_id FROM alert_outbox WHERE alert_id=?",
                    (key,),
                ).fetchone()
                if (
                    row
                    and row["status"] == "SENT"
                    and row["delivered_at"]
                    and _message_receipt(row["provider_message_id"])
                ):
                    receipts.append(dict(row))
            if not receipts or (
                reply_receipt
                and not any(
                    row["provider_message_id"] == reply_receipt for row in receipts
                )
            ):
                continue
            group["last_delivery"] = max(
                timestamp(row["delivered_at"]) for row in receipts
            )
            group["delivery_status"] = (
                "SENT" if len(receipts) == len(group["refs"]) else "PARTIAL"
            )
            delivered.append(group)
    if not delivered:
        return dict(
            status="NOT_FOUND", message="조건에 맞는 실제 전달된 저녁 묶음이 없습니다."
        )
    delivered.sort(
        key=lambda group: (group["last_delivery"], group["id"]), reverse=True
    )
    if len(delivered) > 1 and (date or digest_id or reply_receipt):
        return dict(
            status="AMBIGUOUS",
            choices=[dict(digest_id=g["id"], date=g["date"]) for g in delivered],
        )
    group = delivered[0]
    item = group["entries"].get(number)
    meta = dict(
        digest_id=group["id"],
        digest_date=group["date"],
        delivery_status=group["delivery_status"],
    )
    if item is None:
        return dict(
            status="NUMBER_NOT_FOUND",
            **meta,
            available_numbers=sorted(group["entries"]),
        )
    result = dict(
        status="OK",
        **meta,
        number=number,
        title=item["title"],
        event_id=item["event_id"],
        summary=item.get("summary"),
        analyzed_at=item["analyzed_at"],
        decision_at_digest=item.get("decision"),
        sources_at_digest=item.get("evidence"),
        snapshot_available=item.get("decision") is not None
        and item.get("evidence") is not None,
    )
    if (
        item.get("current_updated_at")
        and timestamp(item["current_updated_at"]) > timestamp(item["analyzed_at"])
        and item.get("current_decision") != item.get("decision")
    ):
        result["later_analysis"] = dict(
            at=item["current_updated_at"], decision=item["current_decision"]
        )
    return result
