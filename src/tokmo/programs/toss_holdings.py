"""Pure Toss holdings summaries and snapshot comparison.

Everything here is a function of already-produced facts or already-stored
records: no transport, no clock, no filesystem. Two rules shape the output.

Aggregate-only for humans: the counts and the provider field *names* are the
only things a person or a relay ever sees; positions stay in the private
store and in the private diff file the caller writes.

Observation, not causality: a comparison reports what differs between two
captures. It never pairs a decrease in one symbol with an increase in
another, and it never claims that equal ordinals in two captures denote the
same account — so an account-count change is surfaced rather than smoothed
over.
"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime

from tokmo.values import (
    TOSS_SOURCE,
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossAccountHoldingsFact,
)

DIFF_FIELDS = ("quantity", "avg_price")


def assemble_complete_snapshot(
    facts: tuple,
    *,
    snapshot_id: str,
    content_hash_fn: Callable[[CompleteHoldingsSnapshot], str],
) -> CompleteHoldingsSnapshot:
    """Assemble a complete multi-account snapshot from per-account facts.

    Fail-closed validation runs before ``content_hash_fn`` is ever called:

    * *facts* must be a non-empty tuple of ``TossAccountHoldingsFact``.
    * Every fact must carry exactly ``TOSS_SOURCE`` as its source.
    * Every fact must share one aware UTC ``captured_at``.
    * Every fact must carry the same ``account_count`` (N), and
      ``N == len(facts)``.
    * Ordinals must be exactly ``1..N`` in capture order.

    ``content_hash_fn`` is injected so programs never import records.
    The caller (composition) passes ``snapshot_content_hash`` from the
    adapter; the function itself only rearranges typed facts into the
    bundle structure and lets the injected hasher compute the digest.
    """
    # --- fail-closed validation before any hasher call ---

    if type(facts) is not tuple:
        raise ValueError("holdings_facts_not_tuple")
    if not facts:
        raise ValueError("holdings_facts_empty")
    for fact in facts:
        if type(fact) is not TossAccountHoldingsFact:
            raise ValueError("holdings_fact_type_mismatch")

    first = facts[0]
    captured_at = first.captured_at
    account_count = first.account_count
    source = first.source

    if source != TOSS_SOURCE:
        raise ValueError("holdings_fact_source_mismatch")
    if len(facts) != account_count:
        raise ValueError("holdings_fact_count_neq_len")
    for index, fact in enumerate(facts):
        if fact.source != source:
            raise ValueError("holdings_fact_source_mismatch")
        if fact.captured_at != captured_at:
            raise ValueError("holdings_fact_timestamp_mismatch")
        if fact.account_count != account_count:
            raise ValueError("holdings_fact_count_conflict")
        if fact.account_ordinal != index + 1:
            raise ValueError("holdings_fact_ordinal_out_of_sequence")

    # --- assembly (all evidence verified, hasher safe to call) ---

    accounts = tuple(
        HoldingsSnapshotAccount(
            account_ordinal=fact.account_ordinal,
            holdings=fact.holdings,
        )
        for fact in facts
    )
    provisional = CompleteHoldingsSnapshot(
        snapshot_id=snapshot_id,
        source=source,
        captured_at=captured_at,
        discovered_account_count=account_count,
        accounts=accounts,
        content_hash="0" * 64,
    )
    return CompleteHoldingsSnapshot(
        snapshot_id=snapshot_id,
        source=source,
        captured_at=captured_at,
        discovered_account_count=account_count,
        accounts=accounts,
        content_hash=content_hash_fn(provisional),
    )


def aggregate(facts) -> dict:
    items = [holding for fact in facts for holding in fact.holdings]
    return {
        "accounts": len(facts),
        "holdings_items": len(items),
        "currencies_observed": len({item.currency for item in items if item.currency}),
        "markets_observed": len({item.market_raw for item in items if item.market_raw}),
    }


def field_presence(facts) -> dict:
    """Sanitized schema evidence: which provider names were observed, and how
    many items each account returned. Names and counts only, never values."""
    items = [holding for fact in facts for holding in fact.holdings]
    return {
        "avg_price_fields": sorted({item.avg_price_field for item in items}),
        "market_fields": sorted(
            {item.market_field for item in items if item.market_field}
        ),
        "item_counts": [len(fact.holdings) for fact in facts],
    }


def format_masked_result(result) -> str:
    """The one line eligible for a report or a relay."""
    return (
        f"toss-holdings result={result.code} started={result.started}"
        f" accounts={result.account_count} holdings_items={result.holdings_items}"
        f" currencies={result.currencies_observed}"
        f" markets_observed={result.markets_observed}"
        f" saved={result.saved} duplicate={result.duplicate}"
        f" conflict={result.conflict}"
        f" avg_price_field={_names(result.avg_price_fields)}"
        f" market_field={_names(result.market_fields)}"
    )


def captures(records) -> list[tuple[str, list[dict]]]:
    """Toss snapshot records grouped by capture instant, oldest first."""
    grouped: dict[str, list[dict]] = {}
    for record in records:
        if record.get("source") != TOSS_SOURCE:
            continue
        grouped.setdefault(record["captured_at"], []).append(record)
    return sorted(grouped.items(), key=lambda item: datetime.fromisoformat(item[0]))


def summarise_capture(records) -> dict:
    ordered = captures(records)
    current = ordered[-1][1] if ordered else []
    items = [item for record in current for item in record["holdings"]]
    return {
        "captures": len(ordered),
        "captured_at": ordered[-1][0] if ordered else None,
        "accounts": len(current),
        "holdings_items": len(items),
        "currencies_observed": len(
            {item["currency"] for item in items if item["currency"]}
        ),
        "markets_observed": len(
            {item["market_raw"] for item in items if item["market_raw"]}
        ),
    }


def format_capture_summary(summary: dict) -> str:
    return (
        f"toss-report captures={summary['captures']}"
        f" accounts={summary['accounts']}"
        f" holdings_items={summary['holdings_items']}"
        f" currencies={summary['currencies_observed']}"
        f" markets_observed={summary['markets_observed']}"
        f" captured_at={summary['captured_at']}"
    )


def diff_latest(records) -> dict:
    """Compare the newest capture against the one before it, as observations.

    The detailed lists name symbols and amounts, so the caller keeps this
    inside the private leaf; only ``counts`` is fit for a human surface.
    """
    ordered = captures(records)
    current_at, current = ordered[-1] if ordered else (None, [])
    previous_at, previous = ordered[-2] if len(ordered) > 1 else (None, [])
    now_positions = _positions(current)
    then_positions = _positions(previous)

    changed, added, removed = [], [], []
    for key in sorted(now_positions):
        after = now_positions[key]
        before = then_positions.get(key)
        if before is None:
            if previous_at is not None:
                added.append(_entry(key, after))
            continue
        if any(before[field] != after[field] for field in DIFF_FIELDS):
            changed.append(
                {
                    "account_ordinal": key[0],
                    "symbol": key[1],
                    **{f"{field}_from": before[field] for field in DIFF_FIELDS},
                    **{f"{field}_to": after[field] for field in DIFF_FIELDS},
                }
            )
    for key in sorted(then_positions):
        if key not in now_positions:
            removed.append(_entry(key, then_positions[key]))

    return {
        "current_captured_at": current_at,
        "previous_captured_at": previous_at,
        "account_count_changed": _counts_differ(current, previous),
        "changed": changed,
        "added": added,
        "removed": removed,
        "counts": {
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
            "current_items": len(now_positions),
            "previous_items": len(then_positions),
        },
    }


def format_report(diff: dict) -> str:
    counts = diff["counts"]
    return (
        f"toss-report-diff changed={counts['changed']} added={counts['added']}"
        f" removed={counts['removed']} current_items={counts['current_items']}"
        f" previous_items={counts['previous_items']}"
        f" account_count_changed={str(diff['account_count_changed']).lower()}"
        f" captured_at={diff['current_captured_at']}"
        f" previous_captured_at={diff['previous_captured_at']}"
    )


def _positions(records) -> dict[tuple[int, str], dict]:
    return {
        (record["account_ordinal"], item["symbol"]): item
        for record in records
        for item in record["holdings"]
    }


def _entry(key: tuple[int, str], item: dict) -> dict:
    return {
        "account_ordinal": key[0],
        "symbol": key[1],
        **{field: item[field] for field in DIFF_FIELDS},
    }


def _counts_differ(current, previous) -> bool:
    if not previous:
        return False
    return {record["account_count"] for record in current} != {
        record["account_count"] for record in previous
    }


def diff_latest_typed(
    previous: CompleteHoldingsSnapshot | None,
    current: CompleteHoldingsSnapshot | None,
) -> dict:
    """Compare two typed snapshots as capture-level multisets.

    This performs an ordinal-free multiset comparison over canonical
    observational signatures ``(symbol, quantity, avg_price)``.  Exact
    multiplicities are cancelled between previous and current captures;
    leftover signatures in *current* are reported as **added** and
    leftovers in *previous* as **removed**.

    No durable item or account identity is assumed across captures:

    * ``account_ordinal`` is capture-local and never appears in diff
      entries or the ``counts`` block.  Swapping ordinals between two
      captures with identical holdings therefore produces a zero diff.
    * Because there is no durable identity to pair old and new items,
      a quantity/avg change is never labelled ``changed`` — the old
      signature is *removed* and the new signature is *added*;
      ``changed`` is always ``[]``.
    * ``account_count_changed`` is an aggregate signal
      (``discovered_account_count`` differs) surfaced rather than
      smoothed over, never a per-account A→B linkage claim.
    """
    if current is None:
        return {
            "current_captured_at": None,
            "previous_captured_at": None,
            "account_count_changed": False,
            "changed": [],
            "added": [],
            "removed": [],
            "counts": {
                "changed": 0,
                "added": 0,
                "removed": 0,
                "current_items": 0,
                "previous_items": 0,
            },
        }

    now_sig = _typed_signatures(current)
    then_sig = _typed_signatures(previous) if previous else Counter()

    # Cancel exact multiplicities; the remainder is added/removed.
    net_added = now_sig - then_sig  # surplus in current
    net_removed = then_sig - now_sig  # surplus in previous

    added = [_sig_entry(sig) for sig in sorted(net_added.elements())]
    removed = [_sig_entry(sig) for sig in sorted(net_removed.elements())]

    return {
        "current_captured_at": current.captured_at.isoformat(),
        "previous_captured_at": previous.captured_at.isoformat()
        if previous is not None
        else None,
        "account_count_changed": _typed_counts_differ(previous, current),
        "changed": [],
        "added": added,
        "removed": removed,
        "counts": {
            "changed": 0,
            "added": len(added),
            "removed": len(removed),
            "current_items": sum(now_sig.values()),
            "previous_items": sum(then_sig.values()),
        },
    }


def summarise_typed_snapshot(snapshot) -> dict:
    """Build a capture summary dict from a typed CompleteHoldingsSnapshot.

    Mirrors ``summarise_capture`` but works on typed values, not JSON
    records. Returns the same shape that ``format_capture_summary`` expects.
    """
    items = [item for account in snapshot.accounts for item in account.holdings]
    return {
        "captures": 1,
        "captured_at": snapshot.captured_at.isoformat(),
        "accounts": snapshot.discovered_account_count,
        "holdings_items": len(items),
        "currencies_observed": len({item.currency for item in items if item.currency}),
        "markets_observed": len({item.market_raw for item in items if item.market_raw}),
    }


def _typed_signatures(
    snapshot: CompleteHoldingsSnapshot,
) -> Counter:
    """Build an ordinal-free multiset of canonical observational signatures.

    Each holding across all accounts contributes one ``(symbol, quantity,
    avg_price)`` tuple to the Counter.  Duplicate symbols across accounts
    are preserved as multiplicities, never overwritten.
    """
    return Counter(
        (item.symbol, item.quantity, item.avg_price)
        for account in snapshot.accounts
        for item in account.holdings
    )


def _sig_entry(sig: tuple) -> dict:
    """Build a diff entry dict from a canonical signature tuple.

    Entries contain ``symbol``, ``quantity``, ``avg_price`` only — never
    ``account_ordinal``.
    """
    return {
        "symbol": sig[0],
        "quantity": sig[1],
        "avg_price": sig[2],
    }


def _typed_counts_differ(
    previous: CompleteHoldingsSnapshot | None,
    current: CompleteHoldingsSnapshot,
) -> bool:
    if previous is None:
        return False
    return previous.discovered_account_count != current.discovered_account_count


def _names(names) -> str:
    return "|".join(names) if names else "none"
