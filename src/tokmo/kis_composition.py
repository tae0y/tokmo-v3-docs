"""Small KIS composition independent of the legacy cadence import graph."""

from pathlib import Path


def build_kis_etf_client(*, project_root, state_root, max_requests):
    """Construct the user-authorized ETF reader; construction sends no request."""
    from tokmo.facts.kis_etf_live import (
        KisLiveTransport,
        build_kis_opener,
        load_kis_credentials,
    )
    from tokmo.programs.kis_etf import KisEtfClient
    from tokmo.records.kis_state import KisState
    from tokmo.runtime.wallclock import sleep_seconds, utc_now

    return KisEtfClient(
        credentials=load_kis_credentials(Path(project_root) / ".env.kis"),
        state=KisState(state_root),
        transport=KisLiveTransport(opener=build_kis_opener()),
        clock=utc_now,
        pacer=sleep_seconds,
        max_requests=max_requests,
    )


def refresh_kis_etf_inputs(*, root, at):
    """Optional configured read-only enrichment; failures never veto analysis."""
    from tokmo.facts.etf_issuer import fetch_portfolio
    from tokmo.facts.etf_issuer_live import IssuerLiveTransport
    from tokmo.programs.kis_etf_refresh import refresh_etf_inputs

    def fetch(symbol, *, at):
        transport = IssuerLiveTransport()
        fact, raw = fetch_portfolio(symbol, at=at, get=transport)
        fact["read_requests"] = transport.requests
        return fact, raw

    return refresh_etf_inputs(
        root, at=at, client_factory=build_kis_etf_client, portfolio_fetch=fetch
    )
