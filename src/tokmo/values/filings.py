"""Filing types: SEC filings and OpenDART disclosures."""

from dataclasses import dataclass
from datetime import date, datetime

__all__ = ["SecFilingFact", "OpenDartDisclosureFact"]


@dataclass(frozen=True)
class SecFilingFact:
    source: str
    ticker: str
    cik: str
    company_name: str
    accession_number: str
    form: str
    filing_date: date
    report_date: date | None
    primary_document: str
    is_xbrl: bool | None
    observed_at: datetime

    @property
    def source_ref(self) -> str:
        return f"{self.source}:{self.cik}:{self.accession_number}"


@dataclass(frozen=True)
class OpenDartDisclosureFact:
    source: str
    stock_code: str
    corp_code: str
    corp_name: str
    corp_cls: str
    report_name: str
    receipt_number: str
    filer_name: str
    receipt_date: date
    remark: str | None
    observed_at: datetime

    @property
    def source_ref(self) -> str:
        return f"{self.source}:{self.corp_code}:{self.receipt_number}"
