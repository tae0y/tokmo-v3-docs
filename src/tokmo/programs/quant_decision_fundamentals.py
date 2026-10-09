"""Small point-in-time financial projections and transparent peer comparisons.

Reads official facts supplied by adapters. No industry inference, model
forecast, fiscal-year mixing or silently reconstructed quarterly totals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from tokmo.values.quant_decisions import number

KR_ACCOUNTS = {
    "revenue": {"매출액", "수익(매출액)", "영업수익"},
    "operating_profit": {"영업이익", "영업이익(손실)"},
    "net_income": {"당기순이익", "당기순이익(손실)"},
    "equity": {"자본총계"},
    "assets": {"자산총계"},
    "liabilities": {"부채총계"},
}


def dart_metrics(payload, *, corp_code, year, as_of):
    if payload.get("status") != "000":
        return {}, []
    rows = [
        r
        for r in payload.get("list", [])
        if r.get("corp_code") == corp_code
        and r.get("bsns_year") == str(year)
        and r.get("reprt_code") == "11011"
    ]
    scope = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
    rows = [r for r in rows if r.get("fs_div") == scope]
    metrics, evidence = {}, []
    for key, names in KR_ACCOUNTS.items():
        found = []
        for row in rows:
            receipt = row.get("rcept_no", "")
            if (
                row.get("account_nm") not in names
                or len(receipt) != 14
                or not receipt.isdigit()
            ):
                continue
            # Date-only filing availability is conservatively the end of the
            # filing day. A date is not proof of an intraday release time.
            at = datetime.strptime(receipt[:8], "%Y%m%d").replace(
                tzinfo=ZoneInfo("Asia/Seoul")
            ) + timedelta(days=1)
            if at > as_of or row.get("currency") not in {"KRW", "USD"}:
                continue
            try:
                value = number(row["thstrm_amount"].replace(",", "").strip())
                prior = (
                    number(row["frmtrm_amount"].replace(",", "").strip())
                    if row.get("frmtrm_amount", "").strip() not in {"", "-"}
                    else None
                )
            except (ValueError, KeyError):
                continue
            found.append((at, receipt, value, prior, row["currency"]))
        if not found:
            continue
        latest = max(r[0] for r in found)
        values = {r[2:] for r in found if r[0] == latest}
        if len(values) != 1:
            continue
        value, prior, currency = values.pop()
        receipt = next(r[1] for r in found if r[0] == latest)
        source = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}"
        metrics[key] = {
            "value": str(value),
            "unit": currency,
            "period": str(year),
            "period_type": "annual",
            "statement_scope": scope,
            "available_at": latest.isoformat(),
            "source": source,
        }
        if (
            prior is not None
            and prior > 0
            and key in {"revenue", "operating_profit", "net_income"}
        ):
            metrics[key + "_growth"] = dict(
                metrics[key], value=str(value / prior - 1), unit="ratio"
            )
        evidence.append(
            {
                "id": "dart-" + receipt + "-" + key,
                "kind": "fundamental",
                "text": f"{year}년 {scope} {key}: {value} {currency}; 전기 {prior}",
                "available_at": latest.isoformat(),
                "source": source,
            }
        )
    return metrics, evidence


SEC_CONCEPTS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "operating_profit": ("OperatingIncomeLoss",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
}


def sec_metrics(payload, *, as_of):
    metrics, evidence = {}, []
    facts = payload.get("facts", {}).get("us-gaap", {})
    for key, concepts in SEC_CONCEPTS.items():
        candidates = []
        for concept in concepts:
            rows = facts.get(concept, {}).get("units", {}).get("USD", [])
            for row in rows:
                if row.get("form") not in {"10-K", "10-K/A"}:
                    continue
                try:
                    at = datetime.fromisoformat(row["filed"]).replace(
                        tzinfo=UTC
                    ) + timedelta(days=1)
                    end = datetime.fromisoformat(row["end"])
                    if at > as_of:
                        continue
                    if key in {"revenue", "net_income", "operating_profit"}:
                        duration = (end - datetime.fromisoformat(row["start"])).days
                        if not 330 <= duration <= 380:
                            continue
                    candidates.append((end, at, concept, row, number(row["val"])))
                except (KeyError, ValueError):
                    continue
        if not candidates:
            continue
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        end, at, concept, row, value = candidates[0]
        # Conflicting facts in the same latest filing/period remain absent.
        if (
            len(
                {
                    c[4]
                    for c in candidates
                    if c[0] == end and c[1] == at and c[2] == concept
                }
            )
            > 1
        ):
            continue
        cik = str(payload.get("cik", "")).zfill(10)
        source = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        metrics[key] = {
            "value": str(value),
            "unit": "USD",
            "period": end.date().isoformat(),
            "period_type": "annual",
            "available_at": at.isoformat(),
            "source": source,
            "concept": concept,
            "filing": row["accn"],
        }
        prior = next((c for c in candidates if 330 <= (end - c[0]).days <= 400), None)
        if (
            prior
            and prior[4] > 0
            and key in {"revenue", "net_income", "operating_profit"}
        ):
            metrics[key + "_growth"] = dict(
                metrics[key], value=str(value / prior[4] - 1), unit="ratio"
            )
        evidence.append(
            {
                "id": "sec-" + row["accn"] + "-" + key,
                "kind": "fundamental",
                "text": f"{end.date()} 연간 {key}: {value} USD",
                "available_at": at.isoformat(),
                "source": source,
            }
        )
    # Discontinued concepts may only have old observations (e.g. Liabilities).
    # Keep one latest annual period; do not mix old balances with new profit.
    latest_period = max((v["period"] for v in metrics.values()), default=None)
    metrics = {k: v for k, v in metrics.items() if v["period"] == latest_period}
    evidence = [e for e in evidence if any(e["id"].endswith("-" + k) for k in metrics)]
    if "liabilities" not in metrics and "assets" in metrics and "equity" in metrics:
        a, b = metrics["assets"], metrics["equity"]
        # Parent-only equity can exclude noncontrolling interests; only total
        # consolidated equity supports deriving total liabilities.
        if (
            b["concept"]
            == "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
        ):
            metrics["liabilities"] = dict(
                a,
                value=str(D(a["value"]) - D(b["value"])),
                concept="Assets minus total consolidated equity",
            )
    return metrics, evidence


def valuation_metrics(instrument):
    metrics = instrument.metrics

    def get(key):
        row = metrics.get(key)
        if not row:
            return None
        try:
            return number(row["value"])
        except (ValueError, KeyError):
            return None

    results = {}
    for key in ("revenue_growth", "operating_profit_growth", "net_income_growth"):
        value = get(key)
        if value is not None:
            results[key] = value
    for name, numerator, denominator in [
        ("net_margin", "net_income", "revenue"),
        ("roe", "net_income", "equity"),
        ("liabilities_to_assets", "liabilities", "assets"),
    ]:
        a, b = get(numerator), get(denominator)
        if (
            a is not None
            and b is not None
            and b > 0
            and metrics[numerator]["period"] == metrics[denominator]["period"]
            and metrics[numerator]["unit"] == metrics[denominator]["unit"]
        ):
            results[name] = a / b
    shares = get("shares_outstanding")
    income = get("net_income")
    equity = get("equity")
    if shares and shares > 0 and instrument.price:
        cap = shares * instrument.price
        if income is not None and metrics["net_income"]["unit"] == instrument.currency:
            results["earnings_yield"] = income / cap
        if equity is not None and metrics["equity"]["unit"] == instrument.currency:
            results["book_to_market"] = equity / cap
    return results


def peer_scores(snapshot):
    raw = {
        i.key: valuation_metrics(i) for i in snapshot.instruments if i.kind == "stock"
    }
    by_key = {i.key: i for i in snapshot.instruments}
    output = {}
    for key, metrics in raw.items():
        item = by_key[key]
        ranks = {}
        for name, value in metrics.items():
            peers = [
                values[name]
                for k, values in raw.items()
                if name in values
                and by_key[k].market == item.market
                and item.sector
                and by_key[k].sector == item.sector
            ]
            if len(peers) < 3:
                continue
            lower = sum(v < value for v in peers)
            ties = sum(v == value for v in peers)
            pct = D(str(lower + (ties - 1) / 2)) / D(len(peers) - 1)
            if name == "liabilities_to_assets":
                pct = 1 - pct
            ranks[name] = {
                "percentile": str(pct),
                "sample_size": len(peers),
                "sector": item.sector,
            }
        output[key] = {
            "metrics": {k: str(v) for k, v in metrics.items()},
            "peer_ranks": ranks,
            "score": float(sum(D(v["percentile"]) for v in ranks.values()) / len(ranks))
            if ranks
            else None,
            "basis": "연결된 같은 시장·확인된 동종 업종 내 상대비교. 연간 실적 기준이며 미래 실적 전망이 아닙니다.",
        }
    return output
