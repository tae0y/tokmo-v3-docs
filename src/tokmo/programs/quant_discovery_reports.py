"""Deterministic public-card report assembly for Quant discovery."""

from tokmo.values.quant_discovery import QuantRunReportV1


def assemble_report(*, identity, cards, unavailable_codes, operation_counts):
    if any(card.market is not identity.market for card in cards):
        raise ValueError("cross-market report")
    return QuantRunReportV1(
        identity=identity,
        cards=tuple(sorted(cards, key=lambda card: card.symbol)),
        unavailable_codes=tuple(unavailable_codes),
        operation_counts=tuple(operation_counts),
    )


def assemble_market_report(**kwargs):
    return assemble_report(**kwargs)


def assemble_personal_peer_report(**kwargs):
    return assemble_report(**kwargs)


def assemble_pension_report(**kwargs):
    return assemble_report(**kwargs)
