"""Period-only pension exports: semantic batch identity and overlap refusal.

No file, workbook, network or portfolio access. The store invokes admission
inside its existing BEGIN IMMEDIATE transaction, so competing writes cannot
race the period check. Legacy cumulative row identities remain unchanged.
"""

import hashlib
from dataclasses import replace

from tokmo.records.trade_ledger_canonical import canonical_payload_bytes

DELTA_SOURCE = "retirement_excel_delta_v1"
PENSION_SCOPE = "retirement_pension_1"
LEGACY_SOURCE = "retirement_excel_v1"


def identify_delta(events):
    """Identify a complete semantic batch, retaining identical occurrences."""
    normalized = tuple(replace(e, source_record_key="batch") for e in events)
    ordered = sorted(normalized, key=canonical_payload_bytes)
    digest = hashlib.sha256(
        b"pension-delta-v1\0" + b"\n".join(canonical_payload_bytes(e) for e in ordered)
    ).hexdigest()
    return tuple(
        replace(e, source_record_key=f"batch-{digest}-row-{i:06d}")
        for i, e in enumerate(ordered, 1)
    )


def legacy_overlaps_delta(connection, events):
    """Prevent cumulative re-exports from inserting a second copy of delta fills."""
    ranges = connection.execute(
        "SELECT min(trade_date),max(trade_date) FROM trade_ledger_events "
        "WHERE source=? AND account_scope=? GROUP BY substr(source_record_key,1,70)",
        (DELTA_SOURCE, PENSION_SCOPE),
    ).fetchall()
    return any(
        first <= e.trade_date.isoformat() <= last
        for first, last in ranges
        for e in events
    )


def delta_admission_reason(connection, events):
    """Return a sanitized refusal reason; caller owns transaction/rollback."""
    if not events or any(
        e.source != DELTA_SOURCE
        or e.account_scope != PENSION_SCOPE
        or e.supersedes_source_record_key is not None
        for e in events
    ):
        return "source_contract_violation"
    expected = identify_delta(events)
    if sorted(map(canonical_payload_bytes, events)) != sorted(
        map(canonical_payload_bytes, expected)
    ):
        return "source_contract_violation"
    prefix = expected[0].source_record_key[:70]
    low = min(e.trade_date.isoformat() for e in events)
    high = max(e.trade_date.isoformat() for e in events)
    # Bounds of the whole stored batch matter, including dates with no rows.
    ranges = connection.execute(
        "SELECT source, CASE WHEN source=? THEN substr(source_record_key,1,70) "
        "ELSE '' END AS batch, min(trade_date), max(trade_date), count(*) "
        "FROM trade_ledger_events WHERE account_scope=? AND source IN (?,?) "
        "GROUP BY source,batch",
        (DELTA_SOURCE, PENSION_SCOPE, LEGACY_SOURCE, DELTA_SOURCE),
    )
    for source, batch, first, last, count in ranges:
        if source == DELTA_SOURCE and batch == prefix:
            if count != len(events):
                return "source_contract_violation"
            continue  # the store still compares every canonical payload byte
        if first <= high and low <= last:
            return "batch_identity_conflict"
    return None
