#!/usr/bin/env python3
"""Validate and render Git-tracked narrative playbooks (stdlib only)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

CURRENT_START = "<!-- HERMES_CURRENT_START -->"
CURRENT_END = "<!-- HERMES_CURRENT_END -->"
ANCHORS_START = "<!-- HERMES_ANCHORS_START -->"
ANCHORS_END = "<!-- HERMES_ANCHORS_END -->"
# Three layers, per the 2026-07-29 design as clarified on 2026-08-07:
#   1층 HERMES_CURRENT — Hermes' own judgement. Replace-in-place, Git history.
#   2층 USER_THESIS    — the user's words verbatim (typos included).
#                        Replace-in-place, Git history.
#   3층 매매이력        — one entry per executed trade. Append-only.
# The anchor index existed to stop an early thesis from being buried under a
# growing timeline; layer 2 now holds the thesis outright, so a migrated
# document does not need it.
USER_THESIS_START = "<!-- USER_THESIS_START -->"
USER_THESIS_END = "<!-- USER_THESIS_END -->"
# Layer 3 carries trade rationale only. v2_strategy_playbook survives solely so
# a pre-conversion document (none since 2026-08-07) stays parseable; a converted
# document may not reintroduce it.
TIMELINE_SOURCE_KINDS = frozenset({"user_trade_reason", "v2_trade_history_reason"})
THESIS_SOURCE_KINDS = frozenset({"v2_strategy_playbook"})
RAW_START = '<!-- USER_RAW_START entry_id="{entry_id}" -->'
RAW_END = '<!-- USER_RAW_END entry_id="{entry_id}" -->'
RESERVED = ("<!-- USER_RAW_START", "<!-- USER_RAW_END")


class TimelineEntry(NamedTuple):
    entry_id: str
    heading_date: str
    heading: str
    source_kind: str
    source_ref: str
    payload: str
    event_date: str = "unknown"
    recorded_date: str = "unknown"
    source_snapshot_date: str = "unknown"
    source_snapshot_basis: str = "unknown"
    legacy_trade_key: str | None = None
    legacy_code: str | None = None
    legacy_name: str | None = None
    imported_at: str = "unknown"


def canonical_payload(payload: str) -> str:
    return payload.replace("\r\n", "\n").replace("\r", "\n")


def render_raw_block(entry_id: str, payload: str) -> str:
    payload = canonical_payload(payload)
    if any(marker in payload for marker in RESERVED):
        raise ValueError("reserved marker in narrative payload")
    return f"{RAW_START.format(entry_id=entry_id)}\n{payload}\n{RAW_END.format(entry_id=entry_id)}"


def extract_raw_block(text: str, entry_id: str) -> str:
    start = RAW_START.format(entry_id=entry_id) + "\n"
    end = "\n" + RAW_END.format(entry_id=entry_id)
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"raw markers must occur exactly once: {entry_id}")
    return text.split(start, 1)[1].split(end, 1)[0]


def _entry_markdown(entry: TimelineEntry) -> str:
    fields = [
        ("entry_id", entry.entry_id),
        ("source_kind", entry.source_kind),
        ("source_ref", entry.source_ref),
        ("event_date", entry.event_date),
        ("recorded_date", entry.recorded_date),
        ("source_snapshot_date", entry.source_snapshot_date),
        ("source_snapshot_basis", entry.source_snapshot_basis),
    ]
    if entry.legacy_trade_key:
        fields.extend(
            (
                ("legacy_trade_key", entry.legacy_trade_key),
                ("v3_trade_event_id", "unlinked"),
            )
        )
    if entry.legacy_code:
        fields.extend(
            (
                ("legacy_instrument_code", entry.legacy_code),
                ("legacy_instrument_name", entry.legacy_name or "unknown"),
            )
        )
    fields.append(("imported_at", entry.imported_at))
    metadata = "\n".join(f"- `{key}`: `{value}`" for key, value in fields)
    return (
        f'<a id="entry-{entry.entry_id}"></a>\n'
        f"### {entry.heading_date} — {entry.heading}\n\n{metadata}\n\n"
        f"{render_raw_block(entry.entry_id, entry.payload)}"
    )


def render_instrument_document(
    *,
    scope: str,
    market: str,
    code: str,
    name: str,
    entries: list[TimelineEntry],
    current_understanding: str | None = None,
) -> str:
    anchors = (
        "\n".join(
            f"- **검토할 원문:** [`{entry.entry_id}`](#entry-{entry.entry_id})"
            for entry in entries
        )
        or "- 아직 원문 기록이 없다."
    )
    timeline = "\n\n".join(_entry_markdown(entry) for entry in entries)
    return f'''---
schema_version: 1
document_kind: instrument
playbook_id: "{scope}:{market}:{code}"
portfolio_scope: {scope}
market: {market}
instrument_code: "{code}"
instrument_name: "{name}"
summary_version: 0
summary_as_of: "unreviewed"
source_cutoff: "unreviewed"
---

# {name}

{CURRENT_START}
## 헤르의 현재 이해

> 이 구역은 사용자의 원문이 아니라 헤르의 현재 해석이다.

**마이그레이션 메모:** 초기 V2 자료는 화자 구분이 어렵고 사용자·AI 표현이 혼재할 수 있다.

{current_understanding or "현재 이해를 아직 작성하지 않았다."}
{CURRENT_END}

{ANCHORS_START}
## 핵심 투자 이유와 근거

{anchors}
{ANCHORS_END}

## 사용자 원문 타임라인

{timeline}
'''


def validate_tree(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.exists():
        return errors
    seen_entries: set[str] = set()
    for path in sorted(root.rglob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root)
        thesis = text.count(USER_THESIS_START)
        if thesis > 1 or thesis != text.count(USER_THESIS_END):
            errors.append(f"{rel}: unbalanced {USER_THESIS_START}")
        required = [CURRENT_START, CURRENT_END]
        if thesis:
            # Migrated: the anchor index is superseded by layer 2, but if a
            # document keeps it, it must still be well-formed.
            for marker in (ANCHORS_START, ANCHORS_END):
                if text.count(marker) > 1:
                    errors.append(f"{rel}: marker count {marker}")
            if text.count(ANCHORS_START) != text.count(ANCHORS_END):
                errors.append(f"{rel}: unbalanced {ANCHORS_START}")
        else:
            required += [ANCHORS_START, ANCHORS_END]
        for marker in required:
            if text.count(marker) != 1:
                errors.append(f"{rel}: marker count {marker}")
        if thesis:
            # Once a document carries the user's own thesis section, its
            # timeline must hold trade rationale only — a thesis in both places
            # would leave two competing "current" answers.
            stale = [
                kind
                for kind in re.findall(r"`source_kind`: `([a-z0-9_]+)`", text)
                if kind in THESIS_SOURCE_KINDS
            ]
            if stale:
                errors.append(
                    f"{rel}: thesis section present but timeline still "
                    f"holds {sorted(set(stale))}"
                )
        for kind in re.findall(r"`source_kind`: `([a-z0-9_]+)`", text):
            if kind not in TIMELINE_SOURCE_KINDS | THESIS_SOURCE_KINDS:
                errors.append(f"{rel}: unknown source_kind {kind}")
        ids = re.findall(r'<a id="entry-([^"]+)"></a>', text)
        for entry_id in ids:
            if entry_id in seen_entries:
                errors.append(f"{rel}: duplicate entry {entry_id}")
            seen_entries.add(entry_id)
            try:
                extract_raw_block(text, entry_id)
            except ValueError as exc:
                errors.append(f"{rel}: {exc}")
        for anchor in re.findall(r"\]\(#entry-([^\)]+)\)", text):
            if anchor not in ids:
                errors.append(f"{rel}: missing anchor {anchor}")
        if (
            rel.parts
            and rel.parts[0] in {"personal", "retirement"}
            and len(rel.parts) == 3
        ):
            scope, market, filename = rel.parts
            code = filename.removesuffix(".md")
            expected = f'playbook_id: "{scope}:{market}:{code}"'
            if expected not in text:
                errors.append(f"{rel}: identity/path mismatch")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    errors = validate_tree(args.root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2
    print(f"playbook validation passed: {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
