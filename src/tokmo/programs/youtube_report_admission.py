"""Composed admission seam for one archived 12man report (EXT-YT-001).

One deterministic, clock-free program call admits every closed
``TRACKING_CANDIDATE`` block of one archived report against an explicit
ops-kernel store and returns a bounded machine-readable read-back for the
Telegram renderer. It composes the existing product seams only —
``provenance_from_archive_meta``, ``parse_report_tracking_blocks``,
``admit_report_candidates`` and the kernel read APIs — and owns no
semantic identity, relation, merge, or store-write logic of its own.

The report and meta are UNTRUSTED data (see ``youtube_tracking``); this
module adds no capability beyond composing those seams. The observation
instant comes from the archive metadata itself, never the current clock,
so an identical replay is byte-identical and durably side-effect free.
"""

from datetime import datetime

from tokmo.programs.youtube_tracking import (
    TrackingCandidateParseError,
    admit_report_candidates,
    parse_report_tracking_blocks,
    provenance_from_archive_meta,
)

READBACK_CONTRACT = "youtube-12man-admission-readback:v1"
# Bounded read-back: enough for the renderer's per-candidate status line,
# never an unbounded mirror of the report.
_MAX_READBACK_ENTRIES = 16
_MAX_STATEMENT_EXCERPT = 80

STATUS_NEW = "NEW"
STATUS_NEW_SIGHTING_OR_REVISION = "NEW_SIGHTING_OR_REVISION"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_FAILURE = "FAILURE"


def observed_at_from_archive_meta(meta) -> datetime:
    """Deterministic source-observation instant for one archive meta.

    Documented rule: the archiver's own ``fetched_at`` instant — when the
    transcript bytes were actually observed — wins; only when the archive
    recorded no ``fetched_at`` key at all does the publisher ``published``
    instant apply. A present-but-invalid value fails closed: it never
    silently falls back and never uses the current clock.
    """
    if not isinstance(meta, dict):
        raise TrackingCandidateParseError("archive meta must be a mapping")
    field = "fetched_at" if "fetched_at" in meta else "published"
    raw = meta.get(field)
    if type(raw) is not str or not raw.strip():
        raise TrackingCandidateParseError(f"archive meta {field} must be text")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise TrackingCandidateParseError(f"archive meta {field} not ISO8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TrackingCandidateParseError(
            f"archive meta {field} must be timezone-aware",
        )
    return parsed


def _candidate_status(outcome) -> str:
    """Closed renderer status for one single-candidate admission outcome."""
    if outcome.failures:
        return STATUS_FAILURE
    if outcome.created:
        return STATUS_NEW
    if outcome.revisions or outcome.semantic_sightings:
        return STATUS_NEW_SIGHTING_OR_REVISION
    if outcome.duplicates:
        return STATUS_DUPLICATE
    return STATUS_FAILURE


def _count_revision_sightings(store, provenance) -> int:
    """Durable proof that this exact source revision reached the store.

    Read-only scan over open hypotheses; the report contract must never
    claim registration the tracking store cannot show (delta contract §4).
    """
    count = 0
    for row in store.read_open_hypotheses():
        for sighting in store.read_hypothesis_sightings(row.hypothesis_id):
            if (
                sighting.source_ref == provenance.source_ref
                and sighting.revision == provenance.revision
            ):
                count += 1
    return count


def admit_archived_report(store, *, meta, report_text) -> dict:
    """Admit one archived report's blocks and return the bounded read-back.

    Provenance and the observation instant are validated strictly before
    the store is touched; malformed meta raises without any mutation.
    Candidates are admitted one at a time through the existing idempotent
    seam so each keeps its own closed status token, and a malformed block
    only quarantines itself (M2). The returned mapping carries no private
    paths, no store location, and no hypothesis identifiers.
    """
    provenance = provenance_from_archive_meta(meta)
    observed_at = observed_at_from_archive_meta(meta)
    parsed = parse_report_tracking_blocks(
        report_text,
        source_ref=provenance.source_ref,
        revision=provenance.revision,
        observed_at=observed_at,
    )
    totals = {
        "created": 0,
        "revisions": 0,
        "semantic_sightings": 0,
        "duplicates": 0,
        "admission_failures": 0,
        "related_edges": 0,
        "merge_reviews": 0,
    }
    entries = []
    for candidate in parsed.candidates:
        outcome = admit_report_candidates(store, (candidate,), now=observed_at)
        totals["created"] += outcome.created
        totals["revisions"] += outcome.revisions
        totals["semantic_sightings"] += outcome.semantic_sightings
        totals["duplicates"] += outcome.duplicates
        totals["admission_failures"] += outcome.failures
        totals["related_edges"] += outcome.related_edges
        totals["merge_reviews"] += outcome.merge_reviews
        entries.append(
            {
                "status": _candidate_status(outcome),
                "statement_excerpt": candidate.statement[:_MAX_STATEMENT_EXCERPT],
            }
        )
    if parsed.failures:
        admit_report_candidates(
            store,
            (),
            now=observed_at,
            failures=parsed.failures,
        )
    failure_rows = store.read_candidate_failures(
        source_ref=provenance.source_ref,
    )
    sightings_for_revision = _count_revision_sightings(store, provenance)
    return {
        "contract": READBACK_CONTRACT,
        "source_ref": provenance.source_ref,
        "revision": provenance.revision,
        "parsed": {
            "valid_candidates": len(parsed.candidates),
            "malformed_blocks": len(parsed.failures),
        },
        "admission": totals,
        "candidates": entries[:_MAX_READBACK_ENTRIES],
        "candidates_omitted": max(0, len(entries) - _MAX_READBACK_ENTRIES),
        "quarantined": [
            {"status": STATUS_FAILURE, "error_kind": failure.error_kind}
            for failure in parsed.failures[:_MAX_READBACK_ENTRIES]
        ],
        "read_back": {
            "sightings_for_revision": sightings_for_revision,
            "failure_records_for_revision": sum(
                1 for row in failure_rows if row.revision == provenance.revision
            ),
            "source_revision_registered": sightings_for_revision > 0,
        },
    }
