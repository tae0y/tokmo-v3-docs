"""Two KIS quote contracts plus token authentication; no account/order routes."""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import json
import re
from types import MappingProxyType

from tokmo.values.kis_etf import KisCredentials, KisRequest, KisResponse, KisError

PATHS = MappingProxyType({
    "auth": "/oauth2/tokenP",
    "components": "/uapi/etfetn/v1/quotations/inquire-component-stock-price",
    "nav": "/uapi/etfetn/v1/quotations/nav-comparison-time-trend",
    "nav_daily": "/uapi/etfetn/v1/quotations/nav-comparison-daily-trend",
})
TR_IDS = MappingProxyType({"components": "FHKST121600C0", "nav": "FHPST02440100", "nav_daily": "FHPST02440200"})


def valid_secret(value):
    return isinstance(value, str) and 1 <= len(value) <= 8192 and all(33 <= ord(c) <= 126 for c in value)


def validate_query(operation, symbol):
    if operation not in TR_IDS or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol):
        raise KisError("invalid_query")


def build_request(request):
    if type(request) is not KisRequest or type(request.credentials) is not KisCredentials:
        raise KisError("invalid_request")
    credentials = request.credentials
    if not valid_secret(credentials.app_key) or not valid_secret(credentials.app_secret):
        raise KisError("invalid_credentials")
    if request.operation == "auth":
        if request.symbol is not None or request.token is not None:
            raise KisError("auth_shape")
        return "POST", PATHS["auth"], {}, {"grant_type": "client_credentials", "appkey": credentials.app_key,
                                           "appsecret": credentials.app_secret}
    validate_query(request.operation, request.symbol)
    if not valid_secret(request.token):
        raise KisError("invalid_token")
    params = {"FID_COND_MRKT_DIV_CODE": "J" if request.operation == "components" else "E",
              "FID_INPUT_ISCD": request.symbol}
    params.update({"FID_COND_SCR_DIV_CODE": "11216"} if request.operation == "components" else {"FID_HOUR_CLS_CODE": "60"})
    if request.operation == "nav_daily":
        for value in (request.date_from, request.date_to):
            try:
                datetime.strptime(value, '%Y%m%d')
            except (TypeError, ValueError):
                raise KisError('query_date') from None
        params = dict(FID_COND_MRKT_DIV_CODE='J', FID_INPUT_ISCD=request.symbol,
                      FID_INPUT_DATE_1=request.date_from, FID_INPUT_DATE_2=request.date_to)
    return "GET", PATHS[request.operation], params, None


def decode_response(response, *, auth=False):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise KisError("duplicate_json_key")
            result[key] = value
        return result
    if type(response) is not KisResponse or type(response.body) is not bytes or len(response.body) > 1048576:
        raise KisError("response_shape")
    try:
        data = json.loads(response.body, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise KisError("response_json") from None
    if type(data) is not dict:
        raise KisError("response_shape")
    if response.status != 200 or (not auth and data.get("rt_cd") != "0"):
        # Provider prose can contain private request information; never return it.
        code = data.get("msg_cd") or data.get("error_code") or "provider_failure"
        raise KisError(code)
    return data


def _number(value, *, positive=False, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value, re.ASCII):
        raise KisError("numeric_contract")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise KisError("numeric_contract") from None
    if not number.is_finite() or (positive and number <= 0):
        raise KisError("numeric_contract")
    return str(number)


def _text(value, *, maximum=160):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise KisError("text_contract")
    return value


def parse_etf_response(operation, symbol, response, *, received_at):
    validate_query(operation, symbol)
    if not isinstance(received_at, datetime) or received_at.utcoffset() is None:
        raise KisError("received_at")
    data = decode_response(response)
    rows = data.get("output2" if operation == "components" else "output")
    if type(rows) is not list or len(rows) > 2000 or any(type(row) is not dict for row in rows):
        raise KisError("rows_contract")
    fact = {"schema": "kis-etf-v1", "source": "KIS", "operation": operation, "symbol": symbol,
            "market": "KR", "currency": "KRW", "received_at": received_at.isoformat(),
            "source_date": None, "freshness": "UNVERIFIED", "status": "OK" if rows else "NO_DATA",
            "more_available": response.continuation in {"F", "M"}, "rows": []}
    if operation == "components":
        metadata = data.get("output1")
        if type(metadata) is not dict:
            raise KisError("metadata_contract")
        reported = _number(metadata.get("etf_cnfg_issu_cnt"), optional=True)
        if reported is not None and (Decimal(reported) < 0 or Decimal(reported) != Decimal(reported).to_integral_value()):
            raise KisError("component_count")
        partial = fact["more_available"] or (reported is not None and Decimal(reported) > len(rows))
        fact.update(coverage="PARTIAL" if partial else "UNVERIFIED", reported_component_count=reported,
                    returned_component_count=len(rows), weight_basis="PROVIDER_UNVERIFIED",
                    provider_summary={k: _number(metadata.get(k), optional=True) for k in
                        ("nav", "prdy_clpr_nav", "stck_prpr", "etf_ntas_ttam", "etf_cnfg_issu_avls")})
        for row in rows:
            fact["rows"].append({"component_code": _text(row.get("stck_shrn_iscd"), maximum=40),
                "component_name": _text(row.get("hts_kor_isnm")), "component_market": None,
                "provider_weight_pct": _number(row.get("etf_cnfg_issu_rlim"), optional=True),
                "valuation_amount": _number(row.get("etf_vltn_amt"), optional=True)})
        weights = [row["provider_weight_pct"] for row in fact["rows"]]
        fact["returned_weight_sum_pct"] = (str(sum((Decimal(w) for w in weights), Decimal(0)))
                                             if weights and all(w is not None for w in weights) else None)
    elif operation == 'nav_daily':
        for row in rows:
            try:
                source_date = datetime.strptime(row['stck_bsop_date'], '%Y%m%d').date().isoformat()
            except (KeyError, TypeError, ValueError):
                raise KisError('source_date') from None
            nav = _number(row.get('nav'), positive=True)
            price = _number(row.get('stck_clpr'), positive=True)
            fact['rows'].append(dict(source_date=source_date, nav=nav, price=price,
                provider_gap_pct=_number(row.get('dprt'), optional=True),
                computed_gap_pct=str(((Decimal(price)/Decimal(nav)-1)*100).quantize(Decimal('0.01')))))
        fact['source_date'] = max((r['source_date'] for r in fact['rows']), default=None)
        fact['freshness'] = 'SOURCE_DATED'
    else:
        for row in rows:
            hour = row.get("bsop_hour")
            if not isinstance(hour, str) or not re.fullmatch(r"\d{6}", hour, re.ASCII):
                raise KisError("source_time")
            try:
                source_time = time(int(hour[:2]), int(hour[2:4]), int(hour[4:])).isoformat()
            except ValueError:
                raise KisError("source_time") from None
            nav = _number(row.get("nav"), positive=True)
            price = _number(row.get("stck_prpr"), positive=True)
            gap = ((Decimal(price) - Decimal(nav)) / Decimal(nav) * 100).quantize(Decimal("0.01"))
            fact["rows"].append({"source_time": source_time, "nav": nav, "price": price,
                                 "provider_gap_pct": _number(row.get("dprt"), optional=True),
                                 "computed_gap_pct": str(gap)})
    return fact
