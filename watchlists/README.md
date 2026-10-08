# Tokmo V3 Watchlists

This directory is the Git-tracked source of truth for the user's explicit current tracking intent.

- `KR.json` and `US.json` contain only user-approved symbols.
- Toss latest COMPLETE holdings remain the sole source of truth for actual ownership.
- Playbooks remain narrative/thesis/history documents and are not scanned into watchlists.
- The five-minute account cadence may union active watchlist symbols into quote/market-data targets, but must never persist them as holdings.
- The SEC collector may union active US watchlist symbols into its filing target universe and preserves per-symbol provenance.
- Removal is represented by `status: retired`; entries are not silently deleted.
- Quantity, cost basis, account identity, current price, and inferred holding state are forbidden here.

Validate with:

```bash
.venv/bin/python tools/validate_watchlists.py
```
