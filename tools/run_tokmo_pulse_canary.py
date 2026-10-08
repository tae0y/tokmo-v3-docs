#!/usr/bin/env python3
"""CLI entrypoint for the G2.5 bounded live canary (LIVE mode only).

Usage (live mode — NOT for tests):
    python tools/run_tokmo_pulse_canary.py \\
        --mode bounded-live-canary \\
        --market KR --symbol 005930 \\
        --canonical-root /path/to/canonical/root \\
        --project-root /path/to/tokmo-v3 \\
        --max-ticks 5 --max-provider-starts 10 \\
        --base-commit <exact-HEAD-sha>

B-1: The campaign evidence root is derived from Path.home() + an immutable
relative constant inside pulse_canary.py. It is NOT passed via --root.
Fixture mode is handled by tools/run_tokmo_pulse_canary_fixture.py.

This tool lives outside src/tokmo/ so it is not subject to the architecture
import seal. It hosts build_canary_live_deps (B1) which composes the real
Toss production transports — TossAuthLiveTransport, TossPricesLiveTransport,
query_current_prices — without going through build_production_cadence_deps.
No HTTP/auth implementation is duplicated.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

# Ensure src/ is on the import path when run from the project root.
project_root = Path(__file__).resolve().parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))


def build_canary_live_deps(*, project_root: Path, clock: Callable,
                           https_handler_factory=None):
    """Compose canary deps from the REAL Toss production adapters.

    B1: This directly reuses TossAuthLiveTransport, TossPricesLiveTransport,
    and query_current_prices — the same code paths as production — without
    going through build_production_cadence_deps (which requires wire claims).
    No HTTP/auth implementation is duplicated. The transports accept an
    https_handler injection point so tests can inject a fake handler.

    The resulting CanaryDepsV1 has:
    - token_fetch: calls TossAuthLiveTransport + parse_token_response
    - quotes_fetch(market, symbols, *, bearer): calls TossPricesLiveTransport
      + query_current_prices, passing the amortized bearer explicitly (B3).
    - sleep_until: real wall-clock sleep (B2).
    """
    import time

    from tokmo.facts.toss_auth_live import TossAuthLiveTransport
    from tokmo.facts.toss_prices_live import TossPricesLiveTransport
    from tokmo.facts.toss_prices import query_current_prices
    from tokmo.facts.toss import (
        build_auth_request, parse_token_response,
        _TossTransportSuccess,
    )
    from tokmo.runtime.config import load_toss_config
    from tokmo.composition import TOSS_ENV_NAME
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.programs.pulse_canary import CanaryDepsV1, _real_sleep_until

    project_root = Path(project_root)
    config = load_toss_config(project_root / TOSS_ENV_NAME)

    def _handler():
        return (None if https_handler_factory is None
                else https_handler_factory())

    auth_transport = TossAuthLiveTransport(https_handler=_handler())
    prices_transport = TossPricesLiveTransport(https_handler=_handler())

    def token_fetch() -> str:
        request = build_auth_request(
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        reply = auth_transport(request)
        if not isinstance(reply, _TossTransportSuccess):
            raise RuntimeError("token_transport_failed")
        token = parse_token_response(reply.body, observed_at=clock())
        token_value = getattr(token, "value", None)
        if token_value is None or not isinstance(token_value, str):
            raise RuntimeError("token_parse_failed")
        return token_value

    def quotes_fetch(market, symbols, *, bearer):
        outcome = query_current_prices(
            symbols=tuple(symbols), token=bearer,
            transport=prices_transport, clock=clock,
        )
        facts = getattr(outcome, "facts", ())
        code = getattr(outcome, "code", "error")
        return PulseQuoteBatchOutcomeV1(code, facts, ())

    return CanaryDepsV1(
        clock=clock,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=_real_sleep_until,
    )


from tokmo.programs.pulse_canary import run_canary_cli

if __name__ == "__main__":
    sys.exit(run_canary_cli(
        sys.argv[1:],
        live_deps_factory=build_canary_live_deps,
    ))
