"""Exact monetary-unit checks for cited disclosure bodies; no valuation or importance scoring.

This verifies amounts, not claim entailment, ownership arithmetic or company attribution.
Only KRW/USD spellings below are supported. Original text remains the source of record.
"""

import re
from decimal import Decimal

NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
SCALE = r"trillion|billion|million|thousand|조|억|만|천"
MONEY = re.compile(
    r"(?<![A-Za-z0-9.,])(?P<prefix>US\$|\$|₩|USD\s*|KRW\s*)?\s*"
    rf"(?P<number>{NUMBER})\s*(?P<scale>{SCALE})?\s*"
    r"(?P<currency>US dollars?\b|U\.S\. dollars?\b|dollars?\b|USD\b|KRW\b|달러|원)?",
    re.IGNORECASE,
)
CELL = re.compile(
    rf"\((?P<currency>원|달러|USD|KRW)\)\s*\n\s*(?P<number>{NUMBER})(?![\d,])",
    re.IGNORECASE,
)
MULTIPLIERS = {
    "trillion": 10**12,
    "billion": 10**9,
    "million": 10**6,
    "thousand": 1000,
    "조": 10**12,
    "억": 10**8,
    "만": 10000,
    "천": 1000,
}


def amounts(text):
    rows = []
    for pattern in (MONEY, CELL):
        for match in pattern.finditer(text):
            fields = match.groupdict()
            unit = fields.get("currency") or fields.get("prefix")
            if not unit:
                continue
            currency = "KRW" if unit.strip().upper() in {"원", "₩", "KRW"} else "USD"
            value = Decimal(fields["number"].replace(",", "")) * MULTIPLIERS.get(
                (fields.get("scale") or "").lower(), 1
            )
            rows.append(
                {
                    "currency": currency,
                    "value": format(value, "f"),
                    "quote": match[0].strip(),
                }
            )
    return rows


def source_amounts(text):
    """Keep declared units across flattened monetary tables, not mixed-unit columns.

    Original-document extraction puts headers and cells on separate lines. A
    single monetary unit applies to its section. A table with individually
    monetary column headers is also usable when no quantity/ratio columns occur.
    Numbers from mixed KRW/shares/% tables remain untyped until a column-aware
    source is available; their mere presence cannot prove a monetary claim.
    """
    rows = amounts(text)
    unit_header = re.compile(r"\(\s*단위\s*[:：]\s*([^\)]+)\)")
    scales = {
        "원": ("KRW", 1),
        "KRW": ("KRW", 1),
        "천원": ("KRW", 1000),
        "백만원": ("KRW", 10**6),
        "억원": ("KRW", 10**8),
        "달러": ("USD", 1),
        "USD": ("USD", 1),
    }
    sections = re.split(r"(?m)^\s*\d+\.\s+(?=\S)", text)
    for section in sections:
        # An explanatory footnote is not part of the table cells.
        section = re.split(r"(?m)^\s*(?:주\s*\d*\)|※|\[참고\])", section)[0]
        headers = list(unit_header.finditer(section))
        regions = []
        if headers:
            for index, header in enumerate(headers):
                unit = re.sub(r"\s+", "", header[1]).upper()
                if unit in scales:
                    regions.append(
                        (
                            section[
                                header.end() : headers[index + 1].start()
                                if index + 1 < len(headers)
                                else len(section)
                            ],
                            *scales[unit],
                        )
                    )
        else:
            # Several amount columns can share subheaders (e.g. common/preferred
            # stock); these are labels, not a quantity column.
            labels = re.findall(r"(?m)^([^\n]*\((원|달러|USD|KRW)\))\s*$", section)
            units = {unit for _, unit in labels}
            monetary = all(
                re.search(r"금액|가액|가격|대금|금\s*액|가\s*액", label)
                for label, _ in labels
            )
            if (
                len(labels) >= 2
                and len(units) == 1
                and monetary
                and not re.search(r"수\s*량|주식수|비\s*율|지분율", section)
            ):
                unit = next(iter(units))
                regions.append((section, *scales[unit]))
        for body, currency, multiplier in regions:
            if re.search(r"수\s*량|주식수|비\s*율|지분율", body):
                continue
            for line in body.splitlines():
                cell = line.strip()
                if re.fullmatch(NUMBER, cell):
                    # Compact numeric dates and source identifiers are not money.
                    if "," not in cell and re.fullmatch(r"(?:19|20)\d{6}", cell):
                        continue
                    value = Decimal(cell.replace(",", "")) * multiplier
                    rows.append(
                        dict(
                            currency=currency,
                            value=format(value, "f"),
                            quote=cell,
                            unit_basis="declared monetary table header",
                        )
                    )
    return rows


def document_amounts(evidence):
    result, seen = [], set()
    for item in evidence:
        if item.get("content_scope") != "original_document" or not item.get(
            "body_verified"
        ):
            continue
        for row in source_amounts(str(item.get("text") or "")):
            key = (item["id"], row["currency"], Decimal(row["value"]))
            if key in seen:
                continue
            seen.add(key)
            value = Decimal(row["value"])
            # Exact conversion only; no rounding, inferred price or computed valuation.
            display = (
                format(value / Decimal(10**8), "f").rstrip("0").rstrip(".")
                if value / Decimal(10**8) != (value / Decimal(10**8)).to_integral()
                else str(int(value / Decimal(10**8)))
            )
            display = (
                display + ("억 원" if row["currency"] == "KRW" else "억 달러")
                if value >= 10**8
                else row["value"] + ("원" if row["currency"] == "KRW" else "달러")
            )
            result.append(dict(row, evidence_id=item["id"], display=display))
    return result


def validate_document_amounts(decision, evidence, prior_bodies=()):
    cited = [item for item in evidence if item["id"] in decision["evidence_ids"]]
    if not any(item.get("content_scope") == "original_document" for item in cited):
        return
    available = {
        (row["currency"], Decimal(row["value"]))
        for item in cited
        for row in source_amounts(
            str(item.get("title") or "") + "\n" + str(item.get("text") or "")
        )
    }
    old_amounts = set()
    if decision["change"] == "CORRECTION" and decision.get("material_change"):
        stated_old = {
            (r["currency"], Decimal(r["value"]))
            for r in amounts(decision["material_change"]["prior_claim"])
        }
        known_old = {
            (r["currency"], Decimal(r["value"]))
            for body in prior_bodies
            for r in amounts(body)
        }
        old_amounts = stated_old & known_old
    # A correction may cite the wrong amount from an actual comparison body. Its new_fact
    # still must use a source amount. This does not prove linguistic entailment of the prose.
    assertions = {
        field: decision[field]
        for field in ("title", "facts", "interpretation", "why_now", "next_check")
    }
    if isinstance(decision.get("digest_summary"), str):
        assertions["digest_summary"] = decision["digest_summary"]
    if decision.get("material_change"):
        assertions["new_fact"] = decision["material_change"]["new_fact"]
    for field, text in assertions.items():
        for row in amounts(text):
            accepted = available if field == "new_fact" else available | old_amounts
            if (row["currency"], Decimal(row["value"])) not in accepted:
                raise ValueError(
                    "ungrounded document amount in " + field + ": " + row["quote"]
                )
