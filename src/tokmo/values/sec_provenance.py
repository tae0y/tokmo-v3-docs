"""SEC holdings-targeted provenance lattice.

Shared between ``facts.sec_targeted`` (pure merge logic) and
``records.sec_targeted_store`` (persistence) so that records never
imports facts.  Values only — no behaviour, no I/O.
"""

TARGET_PROVENANCE = "HOLDING"
WATCHLIST_PROVENANCE = "WATCHLIST"
COMBINED_PROVENANCE = "HOLDING + WATCHLIST"
VALID_TARGET_PROVENANCE = frozenset(
    {TARGET_PROVENANCE, WATCHLIST_PROVENANCE, COMBINED_PROVENANCE}
)
