"""One bounded official document GET, for an already selected discovery candidate.

No arbitrary URLs, attachment crawl, disk extraction, retries or holdings filter.
Source contract: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003
"""

import hashlib
import re
import urllib.request
from io import BytesIO
from urllib.parse import urlencode
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring

from tokmo.facts.opendart import REQUEST_TIMEOUT_SECONDS
from tokmo.facts.opendart_live import build_live_opener

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 128_000


def parse_document(raw, receipt):
    """Preserve the entire bounded document text. Ambiguous/missing main XML is unavailable."""
    if len(raw) > MAX_BYTES:
        raise ValueError("document_size_limit")
    if not raw.startswith(b"PK"):
        root = fromstring(raw)
        status = root.findtext("status")
        raise ValueError(
            "document_unavailable_" + status
            if status and re.fullmatch(r"[0-9]{3}", status)
            else "document_invalid_response"
        )
    with ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 32 or sum(item.file_size for item in entries) > MAX_BYTES:
            raise ValueError("document_size_limit")
        xmls = [
            item
            for item in entries
            if not item.is_dir() and item.filename.lower().endswith(".xml")
        ]
        main = [item for item in xmls if item.filename == receipt + ".xml"]
        if len(main) != 1:
            if len(xmls) != 1:
                raise ValueError("document_main_ambiguous")
            main = xmls
        with archive.open(main[0]) as source:
            xml = source.read(MAX_BYTES + 1)
        if len(xml) > MAX_BYTES:
            raise ValueError("document_size_limit")
    root = fromstring(xml)
    # Keep labels and table cells in source order; do not invent a summary or silently clip.
    text = "\n".join(part.strip() for part in root.itertext() if part.strip())
    if not text or len(text) > MAX_TEXT:
        raise ValueError("document_text_limit")
    return {"text": text, "source_document_sha256": hashlib.sha256(xml).hexdigest()}


class OpenDartDocumentReader:
    def __init__(self, credential_loader, *, opener_factory=build_live_opener):
        self.credential_loader, self.opener_factory = credential_loader, opener_factory

    def __call__(self, receipt):
        if not isinstance(receipt, str) or not re.fullmatch(r"[0-9]{14}", receipt):
            raise ValueError("invalid_document_receipt")
        try:
            key = self.credential_loader()
        except Exception:
            raise ValueError("document_auth_unavailable") from None
        try:
            query = urlencode({"rcept_no": receipt, "crtfc_key": key})
            wire = urllib.request.Request(
                "https://opendart.fss.or.kr/api/document.xml?" + query,
                method="GET",
                headers={"Accept-Encoding": "identity"},
            )
            with self.opener_factory().open(
                wire, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                raw = response.read(MAX_BYTES + 1)
        except Exception:
            # Provider exceptions can carry the query credential; never propagate them.
            raise ValueError("document_transport_unavailable") from None
        try:
            return parse_document(raw, receipt)
        except Exception as exc:
            safe = str(exc)
            if not re.fullmatch(r"document_[a-z_0-9]+", safe):
                safe = "document_parse_unavailable"
            raise ValueError(safe) from None
