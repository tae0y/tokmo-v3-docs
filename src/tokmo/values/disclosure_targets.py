"""Typed recurring disclosure targets."""

import re
from dataclasses import dataclass
from enum import StrEnum


class DisclosureProvider(StrEnum):
    SEC = "SEC"
    OPENDART = "OPENDART"


@dataclass(frozen=True)
class SecDisclosureTarget:
    provider: DisclosureProvider
    symbol: str
    cik: str | None = None


@dataclass(frozen=True)
class DartDisclosureTarget:
    provider: DisclosureProvider
    symbol: str
    corp_code: str | None = None


def parse_disclosure_targets(
    payload,
) -> tuple[SecDisclosureTarget | DartDisclosureTarget, ...]:
    try:
        if (
            type(payload) is not dict
            or set(payload) != {"version", "targets"}
            or payload["version"] != "disclosure30-v1"
            or type(payload["targets"]) is not list
            or not 1 <= len(payload["targets"]) <= 50
        ):
            raise ValueError
        results = []
        identities = set()
        for raw in payload["targets"]:
            if type(raw) is not dict or "provider" not in raw or "symbol" not in raw:
                raise ValueError
            provider = DisclosureProvider(raw["provider"])
            symbol = raw["symbol"]
            if provider is DisclosureProvider.SEC:
                if set(raw) - {"provider", "symbol", "cik"}:
                    raise ValueError
                if (
                    type(symbol) is not str
                    or re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol) is None
                ):
                    raise ValueError
                cik = raw.get("cik")
                if cik is not None and (
                    type(cik) is not str or re.fullmatch(r"\d{10}", cik) is None
                ):
                    raise ValueError
                target = SecDisclosureTarget(provider, symbol, cik)
            else:
                if set(raw) - {"provider", "symbol", "corp_code"}:
                    raise ValueError
                if type(symbol) is not str or re.fullmatch(r"\d{6}", symbol) is None:
                    raise ValueError
                corp_code = raw.get("corp_code")
                if corp_code is not None and (
                    type(corp_code) is not str
                    or re.fullmatch(r"\d{8}", corp_code) is None
                ):
                    raise ValueError
                target = DartDisclosureTarget(provider, symbol, corp_code)
            identity = (provider.value, symbol)
            if identity in identities:
                raise ValueError
            identities.add(identity)
            results.append(target)
        return tuple(results)
    except (KeyError, TypeError, ValueError):
        raise ValueError("disclosure_target_contract") from None
