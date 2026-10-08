"""OpenDART fundamentals live transport (G1A, KR PAUSED).

This module provides the structural live transport for KR fundamentals.
KR is PAUSED — no network calls are made, no endpoints are invented. The
transport class follows the same import boundary as other live transport
adapters for architecture registration and composition wiring. When called,
it always returns a paused unavailable result because KR is unresolved in G1A.

This module does NOT import urllib.request/urllib.error because it never
opens a network connection. The architecture test's live-transport network
boundary contract is satisfied structurally — this module is registered in
LIVE_TRANSPORT_MODULES for composition wiring, and the architecture test
checks that only composition imports it. No urllib imports are needed.
"""

from tokmo.facts.opendart_fundamentals import (
    OPENDART_FUNDAMENTALS_PAUSED_REASON,
    OpenDartFundamentalsResult,
    build_paused_result,
)

OPENDART_FUNDAMENTALS_LIVE_STATUS = "PAUSED"


class OpenDartFundamentalsLiveTransport:
    """KR fundamentals live transport — PAUSED in G1A.

    This class exists for architecture test registration and composition
    wiring. It must never make a network call in G1A. When KR is unpaused
    in a future SDS delta, this class will implement the official
    fnlttSinglAcnt contract with a real HTTPS adapter.
    """

    def __init__(self, *, https_handler=None) -> None:
        self._status = OPENDART_FUNDAMENTALS_LIVE_STATUS

    @property
    def status(self) -> str:
        return self._status

    def __call__(self, *args, **kwargs) -> OpenDartFundamentalsResult:
        """Always returns a PAUSED result in G1A."""
        return build_paused_result()
