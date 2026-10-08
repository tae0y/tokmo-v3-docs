"""One declared-UA, GET-only SEC primary-document read; no exhibits crawl or retry."""
import hashlib
from html.parser import HTMLParser
import re
import urllib.request
from defusedxml.ElementTree import fromstring

from tokmo.facts.sec_live import build_live_opener
from tokmo.values.sec_document import sec_document_url

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 128_000


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "ix:header", "ix:hidden"}:
            self.hidden.append(tag)
        if not self.hidden and tag in {"p", "div", "br", "tr", "td", "th", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden and tag in {"p", "div", "tr", "td", "th", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def document_text(raw):
    if len(raw) > MAX_BYTES or raw.startswith(b"%PDF") or b"\x00" in raw:
        raise ValueError("document_format_or_size")
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("document_encoding_unavailable") from None
    is_html = re.search(r"<html(?:\s|>)", source, re.IGNORECASE) is not None
    # Inline XBRL filings are XHTML, often with an XML declaration. Treat their visible
    # mixed text as HTML; XML-leaf extraction loses surrounding text and exposes ix:hidden.
    if not is_html and re.match(r"\s*<(?:\?xml|ownershipDocument\b|edgarSubmission\b)", source):
        try:
            root = fromstring(source)
        except Exception:
            raise ValueError("document_xml_unavailable") from None
        parts = []
        def leaves(node, path=()):
            if len(path) > 32:
                raise ValueError("document_xml_depth")
            path = path + (node.tag.rsplit("}", 1)[-1],)
            if not len(node) and node.text and node.text.strip():
                parts.append(".".join(path) + ": " + node.text.strip())
            for child in node:
                leaves(child, path)
        leaves(root)
        text = "\n".join(parts)
    else:
        parser = _Text()
        parser.feed(source)
        parser.close()
        text = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())
    if not text or len(text) > MAX_TEXT:
        raise ValueError("document_text_limit")
    return {"text": text, "source_document_sha256": hashlib.sha256(raw).hexdigest(),
            "document_scope": "primary_document_only", "attachments_verified": False}


class SecDocumentReader:
    def __init__(self, user_agent_loader, *, opener_factory=build_live_opener):
        self.user_agent_loader, self.opener_factory = user_agent_loader, opener_factory

    def __call__(self, listing):
        url = sec_document_url(listing.get("cik"), listing.get("accession_number"), listing.get("primary_document"))
        try:
            agent = self.user_agent_loader()
            if not isinstance(agent, str) or not agent.strip() or re.search(r"[\r\n]", agent):
                raise ValueError("bad agent")
        except Exception:
            raise ValueError("document_auth_unavailable") from None
        try:
            request = urllib.request.Request(url, method="GET", headers={"User-Agent": agent, "Accept-Encoding": "identity"})
            with self.opener_factory().open(request, timeout=10) as response:
                if response.status != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("bad response")
                raw = response.read(MAX_BYTES + 1)
        except Exception:
            raise ValueError("document_transport_unavailable") from None
        return document_text(raw)
