#!/usr/bin/env python3
"""Deterministically build an official SIC artifact from staged private bytes."""

import argparse
import json
import re
from hashlib import sha256
from html import unescape
from pathlib import Path


def _cell_text(cell: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", cell)).split())


def _parse_sic_html(raw: bytes) -> tuple[tuple[str, ...], int]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("html_decode") from error
    tables = re.findall(
        r"<table\b[^>]*>.*?</table\s*>", text, flags=re.IGNORECASE | re.DOTALL
    )
    authoritative = []
    for table in tables:
        if len(re.findall(r"<table\b", table, flags=re.IGNORECASE)) != 1:
            continue
        rows = re.findall(
            r"<tr\b[^>]*>.*?</tr\s*>", table, flags=re.IGNORECASE | re.DOTALL
        )
        tr_open_count = len(re.findall(r"<tr\b", table, flags=re.IGNORECASE))
        if not rows or len(rows) != tr_open_count:
            continue
        headings = tuple(
            _cell_text(cell)
            for cell in re.findall(
                r"<t[hd]\b[^>]*>(.*?)</t[hd]\s*>",
                rows[0],
                flags=re.IGNORECASE | re.DOTALL,
            )
        )
        if headings == ("SIC Code", "Office", "Industry Title"):
            header_td_count = len(re.findall(r"<td\b", rows[0], flags=re.IGNORECASE))
            total_td_count = len(re.findall(r"<td\b", table, flags=re.IGNORECASE))
            if total_td_count != header_td_count + 3 * (tr_open_count - 1):
                raise ValueError("sic_rows")
            authoritative.append((rows, tr_open_count - 1))
    if len(authoritative) != 1:
        raise ValueError("sic_authority_table")
    rows, source_row_count = authoritative[0]
    codes = []
    for row in rows[1:]:
        cells = tuple(
            _cell_text(cell)
            for cell in re.findall(
                r"<td\b[^>]*>(.*?)</td\s*>", row, flags=re.IGNORECASE | re.DOTALL
            )
        )
        if len(cells) != 3 or not cells[1] or not cells[2]:
            raise ValueError("sic_rows")
        if not re.fullmatch(r"[0-9]{3,4}", cells[0]):
            raise ValueError("sic_code")
        codes.append(cells[0].zfill(4))
    if not codes:
        raise ValueError("sic_rows")
    if len(set(codes)) != len(codes):
        raise ValueError("sic_duplicate")
    if len(codes) != source_row_count:
        raise ValueError("sic_rows")
    return tuple(sorted(codes)), source_row_count


def parse_sic_html(raw: bytes) -> tuple[str, ...]:
    return _parse_sic_html(raw)[0]


def build_from_capture_store(store, capture_id: str) -> tuple[bytes, bytes]:
    capture = store.read(capture_id)
    raw = capture.body
    if (
        capture.capture_id != capture_id
        or capture.status != 200
        or capture.body_byte_count != len(raw)
        or capture.body_sha256 != sha256(raw).hexdigest()
    ):
        raise ValueError("capture_binding")
    codes, source_row_count = _parse_sic_html(raw)
    artifact = (
        json.dumps(
            {"schema_version": "sec-sic-html-v1", "codes": codes},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    manifest = (
        json.dumps(
            {
                "classification": "OFFICIAL_CAPTURE",
                "source": "SEC_SIC_DOCUMENTATION",
                "capture_instant": capture.captured_at.isoformat(),
                "extraction_contract_version": "sec-sic-html-v1",
                "official_source_sha256": sha256(raw).hexdigest(),
                "artifact_sha256": sha256(artifact).hexdigest(),
                "row_count": len(codes),
                "source_row_count": source_row_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    return artifact, manifest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_store", type=Path)
    parser.add_argument("capture_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    artifact, manifest = build_from_capture_store(
        OfficialMembershipCaptureStore(args.capture_store), args.capture_id
    )
    args.output.write_bytes(artifact)
    args.output.with_suffix(".manifest.json").write_bytes(manifest)


if __name__ == "__main__":
    main()
