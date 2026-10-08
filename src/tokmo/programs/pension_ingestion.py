"""Pension ingestion program: candidate → approve → activate → canonical snapshot.

N4: calls authorize_pension_import from holdings_persistence.py.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from tokmo.records.holdings_persistence import authorize_pension_import
from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
from tokmo.records.pension_revisions import PensionRegistryStore
from tokmo.values.holdings import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    PENSION_SOURCE,
    TossHoldingFact,
)
from tokmo.values.pension_holdings import PensionHoldingsRevisionV1


class PensionIngestionError(Exception):
    pass


def write_canonical_pension_snapshot(
    revision: PensionHoldingsRevisionV1,
    snapshot_store: HoldingsSnapshotStore,
    *,
    clock,
) -> str:
    """Write a canonical pension snapshot to holdings_snapshots.

    Calls authorize_pension_import (N4: no bypass).
    Returns the snapshot_id.
    """
    snapshot_id = uuid4().hex
    permit = authorize_pension_import(
        snapshot_id=snapshot_id,
        program_name="tokmo-v3-pension-import-v1",
        clock=clock,
    )
    from tokmo.records.holdings_persistence import DenialToken
    if isinstance(permit, DenialToken):
        raise PensionIngestionError(permit.reason)

    accounts = []
    if revision.holding_count > 0:
        from tokmo.values.holdings import TossHoldingFact
        toss_holdings = tuple(
            TossHoldingFact(
                symbol=h.symbol,
                quantity=h.quantity,
                avg_price=h.avg_price,
                avg_price_field="average_cost",
                currency=h.currency,
                market_raw=h.market_raw,
                market_field=h.market_field,
                name=None,
                asset_type=None,
            )
            for h in revision.holdings
        )
        accounts.append(HoldingsSnapshotAccount(
            account_ordinal=1,
            holdings=toss_holdings,
        ))
    else:
        # LIQUIDATED: single empty account
        accounts.append(HoldingsSnapshotAccount(
            account_ordinal=1,
            holdings=(),
        ))

    from tokmo.values.holdings import CompleteHoldingsSnapshot
    from tokmo.records.holdings_snapshots import (
        snapshot_content_hash, canonical_snapshot_bytes,
    )
    snapshot = CompleteHoldingsSnapshot(
        snapshot_id=snapshot_id,
        source=PENSION_SOURCE,
        captured_at=revision.source_as_of,
        discovered_account_count=len(accounts),
        accounts=tuple(accounts),
        content_hash=snapshot_content_hash(
            _build_snapshot_for_hash(snapshot_id, PENSION_SOURCE,
                                     revision.source_as_of, tuple(accounts))
        ),
    )
    receipt = snapshot_store.append(snapshot, permit)
    if receipt.status != "SAVED":
        raise PensionIngestionError(f"snapshot append failed: {receipt.status}")
    return snapshot_id


def _build_snapshot_for_hash(snapshot_id, source, captured_at, accounts):
    """Build a CompleteHoldingsSnapshot for content hash computation."""
    from tokmo.values.holdings import CompleteHoldingsSnapshot
    # We need the content hash but can't build it from the final object
    # (chicken-and-egg). Build with a dummy hash first, compute, then return.
    return CompleteHoldingsSnapshot(
        snapshot_id=snapshot_id,
        source=source,
        captured_at=captured_at,
        discovered_account_count=len(accounts),
        accounts=accounts,
        content_hash="dummy",
    )
