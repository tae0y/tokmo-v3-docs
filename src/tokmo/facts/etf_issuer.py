"""Read public issuer portfolio disclosures; keep their dates and denominators.

These are the same read endpoints used by the issuers' product pages. No model
or account data is sent to them. Full rows stay local; consumers compact later.
"""

import json
import re
from datetime import datetime
from decimal import Decimal
from html import unescape
from urllib.parse import urlencode

# Product identities verified on official issuer pages, not portfolio weights.
PRODUCTS = {
    "069500": ("KODEX", "2ETF01"),
    "0080G0": ("KODEX", "2ETFR8"),
    "0204D0": ("KODEX", "2ETFV3"),
    "442580": ("PLUS", "006236"),
    "161510": ("PLUS", "006273"),
    "269540": ("PLUS", "006306"),
    "251600": ("PLUS", "006298"),
    "0138Y0": ("PLUS", "006396"),
    "0008T0": ("SOL", "211080"),
    "0167A0": ("SOL", "211106"),
    "0162Z0": ("RISE", "44K1"),
    "438080": ("ACE", "K55101DU8887"),
    "466940": ("TIGER", "KR7466940004"),
    "490090": ("TIGER", "KR7490090008"),
    "360750": ("TIGER", "KR7360750004"),
}


def number(value):
    value = str(value).replace(",", "").replace("%", "").strip()
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("nonfinite issuer number")
    return result


def dated(value):
    return (
        datetime.strptime(re.sub(r"[^0-9]", "", str(value))[:8], "%Y%m%d")
        .date()
        .isoformat()
    )


def cells(text):
    return [
        unescape(re.sub("<[^>]+>", "", x)).strip()
        for x in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", text, re.DOTALL)
    ]


def constituent(code, name, weight, amount=None):
    code, name = str(code or "").strip(), str(name or "").strip()
    if not name or len(name) > 200 or len(code) > 80:
        raise ValueError("issuer constituent identity")
    key = (
        "ISIN:" + code
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", code)
        else "SOURCE:" + code
    )
    if re.fullmatch(r"[0-9A-Z]{6}", code):
        key = "KR:" + code
    elif re.fullmatch(r"KR7[A-Z0-9]{6}[0-9]{3}", code):
        key = "KR:" + code[3:9]
    elif re.fullmatch(r"[A-Z.]+ US", code):
        key = "US:" + code[:-3]
    kind = (
        "cash"
        if re.search(r"현금|예금|CASH|KRD010", name + " " + code, re.IGNORECASE)
        else "bond"
        if code.startswith(("KR1", "KR3"))
        or re.search(r"국고|통안|채권0|TREASURY|T-NOTE|T-BILL", name, re.IGNORECASE)
        else "derivative"
        if re.search(r"선물|FUTURE|SWAP|스왑|옵션", name, re.IGNORECASE)
        else "fund"
        if re.search(
            r"ETF|TRUST|KODEX|TIGER|RISE|PLUS|ACE |SOL |iShares", name, re.IGNORECASE
        )
        else "stock"
    )
    return dict(
        code=code,
        key=key,
        name=name,
        asset_kind=kind,
        weight_pct=str(number(weight)) if weight not in (None, "", "-") else None,
        valuation_amount=str(number(amount)) if amount not in (None, "", "-") else None,
    )


def parse_portfolio(issuer, payload, *, symbol, received_at, url):
    """Parse a complete disclosure, without normalizing a partial basket to 100%."""
    rows = []
    declared = None
    date = None
    basis = "ISSUER_DISCLOSED_WEIGHT"
    if issuer == "PLUS":
        d = json.loads(payload)
        raw = d["content"]
        declared = d["totalElements"]
        if not d.get("last"):
            raise ValueError("issuer pagination incomplete")
        dates = {dated(x["wkdate"]) for x in raw}
        date = dates.pop() if len(dates) == 1 else None
        rows = [
            constituent(x.get("jmCd") or x.get("krJmCd"), x["jmNm"], x["ratio"])
            for x in raw
        ]
    elif issuer == "SOL":
        raw = json.loads(payload)
        dates = {dated(x["WORK_DT"]) for x in raw}
        if any(x["ETF_CD6"] != symbol for x in raw):
            raise ValueError("issuer symbol mismatch")
        date = dates.pop() if len(dates) == 1 else None
        declared = len(raw)
        rows = [
            constituent(x["STOCK_CODE"], x["SEC_NM"], x["WT_DISP"], x["PRICE"])
            for x in raw
        ]
    elif issuer == "KODEX":
        d = json.loads(payload)["pdf"]
        raw = d["list"]
        declared = int(d["totalCnt"])
        date = dated(d["gijunYMD"])
        rows = [
            constituent(x["itmNo"], x["secNm"], x["ratio"], x["evalA"]) for x in raw
        ]
        # The displayed ratio excludes cash. Evaluation amounts include the
        # cash row and share the same creation-unit denominator.
        total = sum((number(x["valuation_amount"]) for x in rows), Decimal(0))
        if total <= 0:
            raise ValueError("issuer valuation denominator")
        for row in rows:
            row["issuer_weight_pct"] = row["weight_pct"]
            row["weight_pct"] = str(
                (number(row["valuation_amount"]) / total * 100).quantize(
                    Decimal(".000001")
                )
            )
        basis = "ISSUER_CREATION_BASKET_VALUE_INCLUDING_CASH"
    elif issuer == "ACE":
        d = json.loads(payload)
        raw = d["pdfList"]
        date = dated(d["std_DT"])
        declared = len(raw)
        if any(dated(x["std_DT"]) != date for x in raw):
            raise ValueError("mixed issuer dates")
        # Amounts can use different units; never derive weights from their sum.
        rows = [
            constituent(x["jm_KSC_CD"], x["sec_NM"], x["wg"], x["val_AM"]) for x in raw
        ]
        if len(raw) < int(d["page"]["totalElements"]):
            raise ValueError("issuer pagination incomplete")
    elif issuer == "RISE":
        date = dated(re.search(r'id="datepicker_pdf"[^>]*value="([^"]+)"', payload)[1])
        table = re.search(
            r'<tbody[^>]*data-class="tab3PdfList"[^>]*>(.*?)</tbody>',
            payload,
            re.DOTALL,
        )[1]
        for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.DOTALL):
            x = cells(tr)
            if len(x) == 6 and x[0].isdigit():
                rows.append(constituent(x[2], x[1], x[4], x[5]))
        declared = len(rows)
    elif issuer == "TIGER":
        # The page explicitly supplies the portfolio date; the second part is
        # its all-rows response for that date (not the quote return date).
        page, table = payload.split("\n<!-- TOKMO_ROWS -->\n", 1)
        date = dated(re.search(r'name="fixDate"[^>]*value="([^"]+)"', page)[1])
        counts = set(re.findall(r'data-tot-cnt="(\d+)"', table))
        declared = int(counts.pop()) if len(counts) == 1 else None
        for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.DOTALL):
            x = cells(tr)
            if len(x) >= 5:
                rows.append(constituent(x[0], x[1], x[4], x[3]))
    else:
        raise ValueError("unsupported issuer")
    if not date or not rows or len(rows) > 2000 or declared != len(rows):
        raise ValueError("incomplete issuer portfolio")
    if date > received_at.date().isoformat():
        raise ValueError("future issuer date")
    total = sum(
        (number(x["weight_pct"]) for x in rows if x["weight_pct"] is not None),
        Decimal(0),
    )
    # Keep small rounding / expense residuals explicit; no forced rescaling.
    usable = Decimal(95) <= total <= Decimal(105) and all(
        x["weight_pct"] is not None for x in rows
    )
    return dict(
        schema="etf-issuer-v1",
        source=issuer,
        symbol=symbol,
        source_date=date,
        received_at=received_at.isoformat(),
        source_url=url,
        coverage="COMPLETE_DISCLOSURE",
        weight_basis=basis,
        whole_fund_weights_usable=usable,
        returned_component_count=len(rows),
        weight_sum_pct=str(total),
        unallocated_weight_pct=str(Decimal(100) - total),
        rows=rows,
    )


def fetch_portfolio(symbol, *, at, get):
    issuer, product = PRODUCTS[symbol]
    day = at.strftime("%Y%m%d")
    if issuer == "PLUS":
        url = "https://www.plusetf.co.kr/api/v1/product/pdf/list?" + urlencode(
            dict(n=product, d=day, page=0, pageSize=2000)
        )
    elif issuer == "SOL":
        url = "https://www.soletf.com/api/fund/pdfList?" + urlencode(
            dict(fund_cd=product, work_dt=day)
        )
    elif issuer == "KODEX":
        url = (
            f"https://www.samsungfund.com/api/v1/kodex/product-pdf/{product}.do?gijunYMD="
            + at.strftime("%Y.%m.%d")
        )
    elif issuer == "ACE":
        url = f"https://papi.aceetf.co.kr/api/funds/{product}/pdf?" + urlencode(
            dict(std_dt=day, size=2000, page=0)
        )
    elif issuer == "RISE":
        url = f"https://www.riseetf.co.kr/prod/finderDetail/{product}"
    else:
        url = (
            "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/pdf.ajax?ksdFund="
            + product
        )
    payload = get(url)
    if issuer == "TIGER":
        date = re.search(r'name="fixDate"[^>]*value="([^"]+)"', payload)[1]
        more = (
            "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/pdfListAjax.ajax?"
            + urlencode(
                dict(
                    ksdFund=product,
                    fixDate=date,
                    pageIndex=1,
                    firstIndex=0,
                    listCnt=2000,
                    prfPrd="Week01",
                )
            )
        )
        payload += "\n<!-- TOKMO_ROWS -->\n" + get(more)
    return parse_portfolio(
        issuer, payload, symbol=symbol, received_at=at, url=url
    ), payload
