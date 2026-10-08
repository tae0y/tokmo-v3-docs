"""Pure SEC filing address identity. Submitter accession prefix is not the issuer CIK."""
import re
from urllib.parse import quote


def sec_document_url(cik, accession, document):
    if (not isinstance(cik, str) or not re.fullmatch(r"[0-9]{10}", cik) or int(cik) == 0
            or not isinstance(accession, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
            or not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9_. /-]+", document)
            or any(part in {"", ".", ".."} for part in document.split("/"))):
        raise ValueError("document_identity_invalid")
    return ("https://www.sec.gov/Archives/edgar/data/" + str(int(cik)) + "/"
            + accession.replace("-", "") + "/" + quote(document, safe="/"))
