"""Bounded state transitions for the OpenDART all-disclosures pilot."""

import hashlib
from datetime import date, timedelta

from tokmo.facts.opendart_bulk import (
    BulkContractError,
    BulkRequest,
    BulkTransportFailure,
    parse_page,
)
from tokmo.values.news_collection_diagnostics import (
    failure_diagnostics,
    safe_cause,
    sanitize_diagnostics,
)

BASELINE_BEGIN = date(2026, 7, 1)
BASELINE_PAGE_CAP = 800
INCREMENTAL_PAGE_CAP = 40

# Date-partition operate: no fixed total-page cap. The safety bounds are the
# wall-clock deadline inside the scheduler cadence, the provider's own quota
# stop codes, and fail-closed transport handling.
TICK_DEADLINE = timedelta(minutes=20)
STALE_CLAIM_MINUTES = 60
RETENTION_MONTHS = 18
# An empty (013 / zero-row) answer for a recent day is reconfirmed on later
# ticks; it becomes a final `complete` only once the day is this many days old.
EMPTY_FINAL_AGE_DAYS = 3


def retention_floor(today: date) -> date:
    """First day of the calendar month RETENTION_MONTHS before `today`."""
    index = today.year * 12 + today.month - 1 - RETENTION_MONTHS
    return date(index // 12, index % 12 + 1, 1)


class PilotError(RuntimeError):
    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics


def pilot_failure_diagnostics(error, *, elapsed_ms, trace):
    diag = failure_diagnostics(error, elapsed_ms=elapsed_ms, trace=trace)
    if isinstance(error, PilotError):
        if error.diagnostics is not None:
            diag = sanitize_diagnostics(error.diagnostics)
            diag["elapsed_ms"] = elapsed_ms
        else:
            reason = error.args[0] if error.args and type(error.args[0]) is str else ""
            causes = {
                "http_5xx": "HTTP_5XX",
                "http_429": "HTTP_429",
                "timeout": "TIMEOUT",
                "connection_failure": "CONNECTION_ERROR",
                "auth_rejection": "HTTP_AUTH",
                "bad_request": "HTTP_4XX",
                "redirect": "HTTP_REDIRECT",
                "contract_failure": "CONTRACT_MISMATCH",
                "metadata_drift": "CONTRACT_MISMATCH",
            }
            diag["cause"] = causes.get(reason, safe_cause(reason.upper()))
            if diag["cause"] != "UNKNOWN":
                diag["stage"] = "FETCH"
    return diag


def _compact_date(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


def _page(store, run_id, request, api_key, transport, now):
    attempt = store.start_attempt(run_id, request.page_no, now)
    try:
        body = transport(request, api_key)
        page = parse_page(body, request.page_no)
    except BulkTransportFailure as error:
        store.close_attempt(attempt, error.code)
        store.finish(run_id, "failed", now)
        raise PilotError(error.code) from None
    except BulkContractError as error:
        reason = str(error)
        outcome = (
            reason.removeprefix("provider_")
            if reason.startswith("provider_")
            else "contract_failure"
        )
        store.close_attempt(attempt, outcome)
        store.finish(run_id, "failed", now)
        raise PilotError(str(error)) from None
    except Exception:
        store.close_attempt(attempt, "protocol_failure")
        store.finish(run_id, "failed", now)
        raise PilotError("protocol_failure") from None
    store.close_attempt(attempt, "success")
    return page


def _observe_page(store, run_id, page, now):
    """Observe every row on the page, then count the page as fetched.

    A conflict is a per-disclosure anomaly, not a transport or contract
    failure, so it must not abandon the rest of the page or the pages behind
    it. The query sorts ascending by date, so the newest disclosures land on
    the LAST pages — aborting here discarded exactly the rows the run existed
    to collect. Conflicts are counted durably and the callers settle the run as
    `incomplete`.
    """
    persisted = 0
    try:
        for disclosure in page.rows:
            store.observe(run_id, disclosure, now)
            persisted += 1
        store.page_fetched(run_id)
    except Exception as error:
        diag = failure_diagnostics(
            error,
            elapsed_ms=0,
            trace=hashlib.sha256(str(run_id).encode()).hexdigest()[:16],
            stage="PERSIST",
        )
        diag.update(received=len(page.rows), persisted=persisted, progress_scope="page")
        raise PilotError("storage_failure", diagnostics=diag) from error


def _is_safe_operating_growth(previous, page):
    """Accept append-only growth while reading the first page's bounded plan.

    OpenDART's ascending live list can gain newer rows/pages during a run.  The
    first page remains our bounded plan; the one-day overlap picks up rows that
    arrived behind that boundary on the next tick.  Shrinkage is not safe:
    it can move or remove rows inside the plan, so it remains fail-closed.
    """
    return (
        page.total_count >= previous.total_count
        and page.total_page >= previous.total_page
    )


def run_preflight(store, *, fixed_cutoff, api_key, transport, now):
    run = store.claim("preflight", "20260701", fixed_cutoff.strftime("%Y%m%d"), now)
    if run.status == "skipped_overlap":
        return run
    try:
        request = BulkRequest.create(BASELINE_BEGIN, fixed_cutoff, 1)
        page = _page(store, run.run_id, request, api_key, transport, now)
        store.set_plan(run.run_id, page.total_page, page.total_count)
        _observe_page(store, run.run_id, page, now)
        if store.receipt(run.run_id).conflict_count:
            # _observe_page no longer raises, so preflight has to apply the same
            # conflict gate the other lanes already do; otherwise a conflicting
            # probe would report `complete` and authorise a baseline.
            return store.finish(run.run_id, "incomplete", now)
        return store.finish(run.run_id, "complete", now)
    except Exception:
        store.finish(run.run_id, "failed", now)
        raise


def run_baseline(
    store, *, expected_preflight_id, expected_planned_pages, api_key, transport, now
):
    preflight = store.completed_preflight(expected_preflight_id)
    if preflight is None:
        raise PilotError("preflight_identity")
    planned, begin_text, end_text, first_total_count = preflight
    if planned != expected_planned_pages:
        raise PilotError("planned_pages")
    if planned > BASELINE_PAGE_CAP:
        raise PilotError("baseline_overflow")
    begin, end = _compact_date(begin_text), _compact_date(end_text)
    run = store.claim("backfill", begin_text, end_text, now)
    if run.status == "skipped_overlap":
        return run
    try:
        store.set_plan(run.run_id, planned, first_total_count)
        for page_no in range(1, planned + 1):
            page = _page(
                store,
                run.run_id,
                BulkRequest.create(begin, end, page_no),
                api_key,
                transport,
                now,
            )
            if page.total_page != planned or (
                page_no > 1 and page.total_count != first_count
            ):
                store.finish(run.run_id, "incomplete", now)
                raise PilotError("metadata_drift")
            if page_no == 1:
                first_count = page.total_count
                store.set_plan(run.run_id, planned, first_count)
            _observe_page(store, run.run_id, page, now)
        receipt = store.receipt(run.run_id)
        if receipt.fetched_pages != planned or receipt.conflict_count:
            return store.finish(run.run_id, "incomplete", now)
        return store.finish(run.run_id, "complete", now)
    except Exception:
        store.finish(run.run_id, "failed", now)
        raise


def run_incremental(store, *, today, api_key, transport, now):
    if store.latest_complete(("backfill",)) is None:
        raise PilotError("baseline_required")
    latest = store.latest_complete(("incremental", "backfill"))
    _, _, latest_end = latest
    begin = max(BASELINE_BEGIN, _compact_date(latest_end) - timedelta(days=1))
    run = store.claim_pilot_incremental(
        begin.strftime("%Y%m%d"), today.strftime("%Y%m%d"), now
    )
    if run is None:
        raise PilotError("pilot_cap")
    if run.status == "skipped_overlap":
        return run
    try:
        first = _page(
            store,
            run.run_id,
            BulkRequest.create(begin, today, 1),
            api_key,
            transport,
            now,
        )
        store.set_plan(run.run_id, first.total_page, first.total_count)
        _observe_page(store, run.run_id, first, now)
        if first.total_page > INCREMENTAL_PAGE_CAP:
            return store.finish(run.run_id, "overflow", now)
        for page_no in range(2, first.total_page + 1):
            page = _page(
                store,
                run.run_id,
                BulkRequest.create(begin, today, page_no),
                api_key,
                transport,
                now,
            )
            if (page.total_page, page.total_count) != (
                first.total_page,
                first.total_count,
            ):
                store.finish(run.run_id, "incomplete", now)
                raise PilotError("metadata_drift")
            _observe_page(store, run.run_id, page, now)
        receipt = store.receipt(run.run_id)
        if receipt.fetched_pages != max(1, first.total_page) or receipt.conflict_count:
            return store.finish(run.run_id, "incomplete", now)
        return store.finish(run.run_id, "complete", now)
    except Exception:
        store.finish(run.run_id, "failed", now)
        raise


def _collect_day(store, day, *, today, api_key, transport, now, clock, deadline):
    """Collect one single-day partition. Returns the tick outcome for the day.

    Deadline exhaustion is not a failure: the run settles as `partial`, the
    cursor is saved, and the next tick resumes — no day is ever skipped or
    force-completed because it was too large.
    """
    day_text = day.strftime("%Y%m%d")
    run = store.claim("partition", day_text, day_text, now)
    if run.status == "skipped_overlap":
        return "skipped_overlap"
    stored = store.partition(day_text)
    plan = None
    if stored is not None and stored[2] is not None:
        plan = (stored[2], stored[3])
    start = stored[1] if stored and plan and 1 < stored[1] <= plan[0] else 1
    store.set_partition(
        day_text,
        status="in_progress",
        next_page=start,
        plan=plan,
        run_id=run.run_id,
        now=now,
    )
    try:
        page = _page(
            store,
            run.run_id,
            BulkRequest.create(day, day, start),
            api_key,
            transport,
            now,
        )
        if start > 1 and (page.total_page, page.total_count) != plan:
            # The frozen plan no longer matches: a row may have been inserted
            # behind the cursor, so the offset is invalid. Recollect from page
            # one (idempotent) instead of silently skipping rows.
            start = 1
            page = _page(
                store,
                run.run_id,
                BulkRequest.create(day, day, 1),
                api_key,
                transport,
                now,
            )
        plan = (page.total_page, page.total_count)
        store.set_plan(run.run_id, page.total_page, page.total_count)
        _observe_page(store, run.run_id, page, now)
        if plan[0] == 0:
            final = day <= today - timedelta(days=EMPTY_FINAL_AGE_DAYS)
            store.set_partition(
                day_text,
                status="complete" if final else "open",
                next_page=1,
                plan=plan,
                run_id=run.run_id,
                now=now,
            )
            store.finish(run.run_id, "complete", now)
            return "complete" if final else "pending"
        previous = page
        page_no = start + 1
        while page_no <= plan[0]:
            if clock() >= deadline:
                store.set_partition(
                    day_text,
                    status="open",
                    next_page=page_no,
                    plan=plan,
                    run_id=run.run_id,
                    now=now,
                )
                store.finish(run.run_id, "partial", now)
                return "partial"
            page = _page(
                store,
                run.run_id,
                BulkRequest.create(day, day, page_no),
                api_key,
                transport,
                now,
            )
            if not _is_safe_operating_growth(previous, page):
                store.set_partition(
                    day_text,
                    status="failed",
                    next_page=1,
                    plan=None,
                    run_id=run.run_id,
                    now=now,
                )
                store.finish(run.run_id, "incomplete", now)
                raise PilotError("metadata_drift")
            if (page.total_page, page.total_count) != plan:
                # Append-only growth extends the frozen plan — there is no
                # fixed page cap; the deadline is the only bound.
                plan = (page.total_page, page.total_count)
                store.set_plan(run.run_id, page.total_page, page.total_count)
            _observe_page(store, run.run_id, page, now)
            previous = page
            page_no += 1
        if store.receipt(run.run_id).conflict_count:
            store.set_partition(
                day_text,
                status="failed",
                next_page=1,
                plan=None,
                run_id=run.run_id,
                now=now,
            )
            store.finish(run.run_id, "incomplete", now)
            return "conflict"
        # Today keeps growing, so its completion is never final: the partition
        # stays open and the next-day backlog lane owns the final state.
        final = day < today
        store.set_partition(
            day_text,
            status="complete" if final else "open",
            next_page=1,
            plan=plan,
            run_id=run.run_id,
            now=now,
        )
        store.finish(run.run_id, "complete", now)
        return "complete"
    except PilotError:
        # Transport failures may already be settled; storage failures are not.
        store.set_partition(
            day_text,
            status="failed",
            next_page=1,
            plan=None,
            run_id=run.run_id,
            now=now,
        )
        store.finish(run.run_id, "failed", now)
        raise
    except Exception:
        store.set_partition(
            day_text,
            status="failed",
            next_page=1,
            plan=None,
            run_id=run.run_id,
            now=now,
        )
        store.finish(run.run_id, "failed", now)
        raise


def run_operate(store, *, today, api_key, transport, now, deadline, clock):
    """One scheduler tick: today first, then oldest backlog until the deadline.

    Requires the explicit partition migration; today's freshness comes from
    the tick cadence itself, and one in-memory visited set is the only
    per-tick state.
    """
    if not store.partitions_migrated():
        raise PilotError("migration_required")
    store.recover_stale(now, now - timedelta(minutes=STALE_CLAIM_MINUTES))
    floor = retention_floor(today)
    store.seed_forward(today, floor, now)
    outcome = _collect_day(
        store,
        today,
        today=today,
        api_key=api_key,
        transport=transport,
        now=now,
        clock=clock,
        deadline=deadline,
    )
    if outcome == "skipped_overlap":
        return {"status": "skipped_overlap", "partitions": []}
    outcomes = [[today.strftime("%Y%m%d"), outcome]]
    visited = {today.strftime("%Y%m%d")}
    while clock() < deadline:
        day_text = store.next_backlog(
            today.strftime("%Y%m%d"), floor.strftime("%Y%m%d"), tuple(visited)
        )
        if day_text is None:
            break
        outcome = _collect_day(
            store,
            _compact_date(day_text),
            today=today,
            api_key=api_key,
            transport=transport,
            now=now,
            clock=clock,
            deadline=deadline,
        )
        if outcome == "skipped_overlap":
            break
        visited.add(day_text)
        outcomes.append([day_text, outcome])
    return {"status": "complete", "partitions": outcomes}
