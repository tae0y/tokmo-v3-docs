"""Non-network request value for the bounded EP-1 OpenDART tracer."""

from dataclasses import dataclass, field

from tokmo.facts.opendart import REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True)
class OpenDartTraceJsonRequest:
    endpoint: str
    corp_code: str
    business_year: str
    report_code: str = "11011"
    fs_div: str | None = None
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    api_key: str = field(default="", repr=False)
