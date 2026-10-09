"""Bounded, exact-identity transport for official membership sources."""

import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EndpointKindV1(str, Enum):
    SEC_SIC = "SEC_SIC"
    NASDAQ_LISTED = "NASDAQ_LISTED"
    OTHER_LISTED = "OTHER_LISTED"
    SEC_REGISTRY = "SEC_REGISTRY"
    SEC_SUBMISSIONS = "SEC_SUBMISSIONS"


_SPECS = {
    EndpointKindV1.SEC_SIC: (
        "www.sec.gov",
        "/search-filings/standard-industrial-classification-sic-code-list",
        4 * 1024 * 1024,
    ),
    EndpointKindV1.NASDAQ_LISTED: (
        "www.nasdaqtrader.com",
        "/dynamic/symdir/nasdaqlisted.txt",
        8 * 1024 * 1024,
    ),
    EndpointKindV1.OTHER_LISTED: (
        "www.nasdaqtrader.com",
        "/dynamic/symdir/otherlisted.txt",
        8 * 1024 * 1024,
    ),
    EndpointKindV1.SEC_REGISTRY: (
        "www.sec.gov",
        "/files/company_tickers_exchange.json",
        16 * 1024 * 1024,
    ),
}


@dataclass(frozen=True, slots=True)
class OfficialMembershipRequestV1:
    endpoint_kind: EndpointKindV1
    cik: str | None = None

    def __post_init__(self):
        if not isinstance(self.endpoint_kind, EndpointKindV1):
            raise ValueError("endpoint_kind")
        if self.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            if (
                type(self.cik) is not str
                or len(self.cik) != 10
                or not self.cik.isascii()
                or not self.cik.isdigit()
            ):
                raise ValueError("cik")
        elif self.cik is not None:
            raise ValueError("cik")

    @property
    def url(self):
        if self.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            return f"https://data.sec.gov/submissions/CIK{self.cik}.json"
        host, path, _ = _SPECS[self.endpoint_kind]
        return f"https://{host}{path}"

    @property
    def cap(self):
        if self.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            return 8 * 1024 * 1024
        return _SPECS[self.endpoint_kind][2]


@dataclass(frozen=True, slots=True)
class LiveOutcomeV1:
    ok: bool
    code: str
    body: bytes = b""
    observed_at: datetime | None = None
    status: int = 0


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect_rejected", headers, fp
        )


class OfficialMembershipLiveTransport:
    def __init__(self, *, clock):
        self._clock = clock

    def fetch(
        self, identity: OfficialMembershipRequestV1, *, sec_user_agent: str | None
    ) -> LiveOutcomeV1:
        if not isinstance(identity, OfficialMembershipRequestV1):
            return LiveOutcomeV1(False, "INVALID_REQUEST")
        is_sec = identity.endpoint_kind in {
            EndpointKindV1.SEC_SIC,
            EndpointKindV1.SEC_REGISTRY,
            EndpointKindV1.SEC_SUBMISSIONS,
        }
        if is_sec and (type(sec_user_agent) is not str or not sec_user_agent.strip()):
            return LiveOutcomeV1(False, "INVALID_CONFIG")
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": sec_user_agent
            if is_sec
            else "tokmo-v3-official-membership/1",
        }
        request = urllib.request.Request(identity.url, headers=headers, method="GET")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirect()
        )
        try:
            with opener.open(request, timeout=10.0) as response:
                encoding = response.headers.get("Content-Encoding")
                if encoding not in (None, "", "identity"):
                    return LiveOutcomeV1(False, "CONTENT_ENCODING")
                length_text = response.headers.get("Content-Length")
                if length_text is not None and (
                    not length_text.isascii() or not length_text.isdigit()
                ):
                    return LiveOutcomeV1(False, "CONTENT_LENGTH")
                if length_text is not None and int(length_text) > identity.cap:
                    return LiveOutcomeV1(False, "BODY_TOO_LARGE")
                if (
                    length_text is None
                    and response.headers.get("Transfer-Encoding", "").lower()
                    != "chunked"
                ):
                    return LiveOutcomeV1(False, "CONTENT_LENGTH")
                body = response.read(identity.cap + 1)
                if len(body) > identity.cap:
                    return LiveOutcomeV1(False, "BODY_TOO_LARGE")
                if length_text is not None and int(length_text) != len(body):
                    return LiveOutcomeV1(False, "CONTENT_LENGTH")
                return LiveOutcomeV1(
                    True, "OK", body, self._clock(), int(getattr(response, "status", 0))
                )
        except urllib.error.HTTPError as error:
            return LiveOutcomeV1(False, f"HTTP_{error.code}", status=error.code)
        except Exception:
            return LiveOutcomeV1(False, "TRANSPORT_FAILURE")
