"""Weekly YouTube duplicate-review program seam (schema v11).

Once per canonical KST ISO week, every unordered pair of open hypotheses is
reconsidered for potential duplicates. The run is durable and crash-safe:

- the week receipt is reserved atomically (unique week identity), and a
  repeated invocation in the same week replays as a no-op;
- every pair adjudication is itself durably keyed by (week, pair), so an
  interrupted run resumes without duplicate findings;
- the receipt only becomes COMPLETE after the whole scan finished — an
  interrupted run never leaves a false COMPLETE;
- findings are conservative: only RELATED_BUT_DISTINCT edges and OPEN
  MERGE_REVIEW queue items are written; no sighting is ever fabricated
  without a source, no hypothesis is merged, deleted or mutated.

This module performs no network, model, Telegram, order or account effect.
It never calls ``merge_hypotheses``: merging stays manual-only.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class KstIsoWeekV1:
    """Canonical IANA Asia/Seoul ISO-week identity of one aware instant."""

    iso_year: int
    iso_week: int
    identity: str
    range: tuple

    def __post_init__(self):
        if type(self.iso_year) is not int or type(self.iso_week) is not int:
            raise ValueError("invalid ISO week fields")
        if not 1 <= self.iso_week <= 53:
            raise ValueError("invalid ISO week number")
        parts = self.identity.split("-W")
        if (
            len(parts) != 2
            or parts[0] != f"{self.iso_year:04d}"
            or parts[1] != f"{self.iso_week:02d}"
            or len(self.identity) != 8
        ):
            raise ValueError("week identity does not match fields")
        start, end = self.range
        for value in (start, end):
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError("week range must be timezone-aware")
        if end <= start or (end - start) > timedelta(days=7):
            raise ValueError("invalid week range")


def kst_iso_week(instant: datetime) -> KstIsoWeekV1:
    """Derive the canonical KST ISO-week identity from an aware instant.

    The instant is converted through ``ZoneInfo("Asia/Seoul")`` — never a
    fixed offset — so DST/historical offset changes and week/year boundaries
    follow the canonical IANA zone, not the host clock or naive UTC.
    """
    if (
        not isinstance(instant, datetime)
        or instant.tzinfo is None
        or instant.utcoffset() is None
    ):
        raise ValueError("instant must be timezone-aware")
    local = instant.astimezone(KST)
    iso_year, iso_week, iso_day = local.isocalendar()
    # Monday 00:00 KST of this ISO week: subtract (iso_day - 1) days.
    start = (local - timedelta(days=iso_day - 1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )
    start = datetime(
        start.year,
        start.month,
        start.day,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    end = start + timedelta(days=7) - timedelta(microseconds=1)
    return KstIsoWeekV1(
        iso_year=iso_year,
        iso_week=iso_week,
        identity=f"{iso_year:04d}-W{iso_week:02d}",
        range=(start, end),
    )


@dataclass(frozen=True)
class WeeklyReviewOutcomeV1:
    status: str  # COMPLETE | DUPLICATE
    week_identity: str
    open_count: int
    pairs_scanned: int
    new_related_edges: int
    new_merge_reviews: int
    same_hypothesis_findings: int
    hypothesis_ids: tuple = ()
    pair_keys: tuple = ()
    pair_findings: tuple = ()


def _pair_key(left_id: str, right_id: str) -> tuple:
    return (left_id, right_id) if left_id < right_id else (right_id, left_id)


def _profile_for(store, hypothesis_id):
    from tokmo.values.ops_kernel import SemanticProfileV1

    row = store.read_hypothesis(hypothesis_id)
    return row, SemanticProfileV1.from_hypothesis(
        row,
        index_keys=store.read_hypothesis_index(hypothesis_id),
    )


def run_weekly_review(store, *, now: datetime) -> WeeklyReviewOutcomeV1:
    """Evaluate every unordered pair of open hypotheses for this KST week.

    Crash-safe by construction: the week receipt is reserved atomically, each
    pair adjudication is durably keyed by (week, pair) so a retry resumes
    instead of duplicating work, and the receipt only becomes COMPLETE after
    the whole scan finished. SAME_HYPOTHESIS_NEW_SIGHTING is recorded as a
    review finding only — no sighting is fabricated without a source. This
    function performs no network, model, delivery, order or merge effect;
    ``merge_hypotheses`` is never called here.
    """
    from tokmo.programs.youtube_semantic import classify_semantic_profiles
    from tokmo.values.ops_kernel import (
        HypothesisState,
        ProducerRole,
        SemanticClassification,
    )

    week = kst_iso_week(now)
    reservation = store.reserve_weekly_review(
        ProducerRole.ANALYSIS,
        week,
        now=now,
    )
    if reservation["status"] == "DUPLICATE":
        summary = store.read_weekly_review_summary(week.identity)
        run = summary["run"]
        # minor 6: the DUPLICATE replay reports the pair-key set frozen in
        # the completed receipt (one per adjudicated pair), never the
        # current open-hypothesis set which may have grown since.
        return WeeklyReviewOutcomeV1(
            status="DUPLICATE",
            week_identity=week.identity,
            open_count=run.open_count,
            pairs_scanned=run.pairs_scanned,
            new_related_edges=0,
            new_merge_reviews=0,
            same_hypothesis_findings=0,
            hypothesis_ids=tuple(
                sorted(
                    {
                        identifier
                        for row in summary["pairs"]
                        for identifier in (row.hypothesis_a, row.hypothesis_b)
                    }
                )
            ),
            pair_keys=tuple(
                (row.hypothesis_a, row.hypothesis_b) for row in summary["pairs"]
            ),
            pair_findings=tuple(
                (row.hypothesis_a, row.hypothesis_b, row.classification)
                for row in summary["pairs"]
            ),
        )
    # RESUME or CREATED: (re)derive the open set and scan every pair once
    open_rows = [
        row
        for row in store.read_open_hypotheses()
        if row.state in (HypothesisState.CANDIDATE, HypothesisState.WATCHING)
    ]
    hypothesis_ids = [row.hypothesis_id for row in open_rows]
    profiles = {
        hypothesis_id: _profile_for(store, hypothesis_id)
        for hypothesis_id in hypothesis_ids
    }
    pairs = [
        _pair_key(hypothesis_ids[i], hypothesis_ids[j])
        for i in range(len(hypothesis_ids))
        for j in range(i + 1, len(hypothesis_ids))
    ]
    new_edges = new_reviews = same_findings = 0
    findings = []
    for pair in pairs:
        left_row, left_profile = profiles[pair[0]]
        right_row, right_profile = profiles[pair[1]]
        decision = classify_semantic_profiles(left_profile, right_profile)
        adjudicated = store.record_weekly_pair_adjudication(
            ProducerRole.ANALYSIS,
            week.identity,
            pair,
            decision,
            now=now,
        )
        if adjudicated == "created":
            findings.append((pair, decision.classification.value))
            if decision.classification is (
                SemanticClassification.SAME_HYPOTHESIS_NEW_SIGHTING
            ):
                same_findings += 1
        # relation/review creation is idempotent under its own durable
        # identity, so it runs even for a pair adjudicated before a crash —
        # a resume cannot lose the finding.
        if decision.classification is (SemanticClassification.RELATED_BUT_DISTINCT):
            if (
                store.record_hypothesis_relation(
                    ProducerRole.ANALYSIS,
                    pair[0],
                    pair[1],
                    decision,
                    now=now,
                    origin="WEEKLY_REVIEW",
                )
                == "created"
            ):
                new_edges += 1
        elif decision.classification is SemanticClassification.MERGE_REVIEW:
            if (
                store.queue_merge_review(
                    ProducerRole.ANALYSIS,
                    pair[0],
                    pair[1],
                    decision,
                    now=now,
                    origin="WEEKLY_REVIEW",
                )
                == "created"
            ):
                new_reviews += 1
    # minor 1: the COMPLETE receipt summarizes the WHOLE durable week —
    # findings persisted before an interruption (pair adjudications,
    # relations, reviews already written by earlier attempts of this same
    # week) are recomputed from the durable rows, not only from what this
    # invocation created.
    durable = store.read_weekly_review_summary(week.identity)
    # Fable 2nd repair M-1: the receipt counts THIS week's findings, derived
    # exactly from the durable weekly_review_pairs rows for this week —
    # never from store-wide relation/review tables, which also contain
    # pre-existing admission-time rows that would inflate the counters.
    store.complete_weekly_review(
        ProducerRole.ANALYSIS,
        week,
        now=now,
        open_count=len(hypothesis_ids),
        pairs_scanned=len(durable["pairs"]),
        new_related_edges=sum(
            1
            for row in durable["pairs"]
            if row.classification == "RELATED_BUT_DISTINCT"
        ),
        new_merge_reviews=sum(
            1 for row in durable["pairs"] if row.classification == "MERGE_REVIEW"
        ),
        same_hypothesis_findings=sum(
            1
            for row in durable["pairs"]
            if row.classification == "SAME_HYPOTHESIS_NEW_SIGHTING"
        ),
    )
    # M-1: the returned outcome mirrors the durable receipt exactly —
    # counters derive from this week's persisted pair rows, so a resumed
    # run reports the whole week, not only rows this invocation created.
    summary = store.read_weekly_review_summary(week.identity)
    run = summary["run"]
    return WeeklyReviewOutcomeV1(
        status="COMPLETE",
        week_identity=week.identity,
        open_count=run.open_count,
        pairs_scanned=run.pairs_scanned,
        new_related_edges=run.new_related_edges,
        new_merge_reviews=run.new_merge_reviews,
        same_hypothesis_findings=run.same_hypothesis_findings,
        hypothesis_ids=tuple(hypothesis_ids),
        pair_keys=tuple(pairs),
        pair_findings=tuple(findings),
    )


def main(argv=None) -> int:
    """CLI seam: run one weekly review against a store path argument.

    No network/model/delivery capability exists on this path. The store is
    opened from the single positional path argument. The run instant MUST
    be supplied explicitly as an aware ISO-8601 ``--now``: without it the
    CLI refuses (``refusal=observed_now_required``) rather than reading an
    ambient host clock — program source never calls a direct clock. The
    supplied instant is converted through canonical KST exactly like the
    program seam.
    """
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="tokmo-youtube-weekly-review",
        description="Weekly YouTube duplicate review (no-network seam)",
    )
    parser.add_argument("store", help="path to the ops kernel sqlite store")
    parser.add_argument(
        "--now",
        default=None,
        help="aware ISO-8601 run instant (required in "
        "practice; the CLI refuses rather than reading "
        "an ambient clock)",
    )
    args = parser.parse_args(argv)
    # The repository forbids direct ambient clock reads in program source:
    # the run instant arrives as an explicit aware ISO-8601 argument. Without
    # --now the CLI refuses rather than guessing a host-local datetime.
    if args.now is None:
        print(
            "tokmo-youtube-weekly-review refusal=observed_now_required",
            file=__import__("sys").stderr,
        )
        return 1
    try:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    except ValueError:
        print(
            "tokmo-youtube-weekly-review refusal=invalid_now",
            file=__import__("sys").stderr,
        )
        return 1
    if now.tzinfo is None or now.utcoffset() is None:
        print(
            "tokmo-youtube-weekly-review refusal=now_must_be_aware",
            file=__import__("sys").stderr,
        )
        return 1
    from tokmo.records.ops_kernel import OpsKernelStore

    store = OpsKernelStore(Path(args.store))
    try:
        outcome = run_weekly_review(store, now=now)
    finally:
        store.close()
    print(
        f"weekly_review status={outcome.status} week={outcome.week_identity} "
        f"open={outcome.open_count} pairs={outcome.pairs_scanned} "
        f"related_edges={outcome.new_related_edges} "
        f"merge_reviews={outcome.new_merge_reviews} "
        f"same_hypothesis_findings={outcome.same_hypothesis_findings}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
