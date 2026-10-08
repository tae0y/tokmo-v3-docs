#!/usr/bin/env python3
"""Validate the canonical Tokmo V3 watchlist registries."""

from pathlib import Path

from tokmo.watchlists import load_watchlists


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_watchlists(root / "watchlists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
