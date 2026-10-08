"""Credential-redacted KIS ETF read vocabulary."""
from dataclasses import dataclass, field
import re


class KisError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,48}", code) else "provider_failure"
        super().__init__(self.code)


@dataclass(frozen=True)
class KisCredentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)


@dataclass(frozen=True)
class KisRequest:
    operation: str
    credentials: KisCredentials = field(repr=False)
    token: str | None = field(default=None, repr=False)
    symbol: str | None = None
    date_from: str | None = None
    date_to: str | None = None


@dataclass(frozen=True)
class KisResponse:
    status: int
    body: bytes = field(repr=False)
    continuation: str = ""
