"""Small decision-engine extension of the existing NEWS episode database.

Opened explicitly by the new worker/replay, never by the legacy pulse. Existing
tables and receipts are left intact; additive DDL participates in its checksum.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tokmo.records.news_episode_store import NewsEpisodeStore, _actual_ddl_digest

KST = ZoneInfo("Asia/Seoul")
# Named time keys of an observation payload; compared as instants, never as text.
TIME_KEYS = frozenset({"observed_at", "published_at", "first_seen_at"})
BUDGETED_STAGES = (
    "analyze",
    "analyze_first",
)  # the daily token budget counts and gates these stages only
# The one key rows stored before 2026-09-07 lack; its absence in a stored row is not a contradiction.
LEGACY_OPTIONAL_KEYS = frozenset({"time_interpretation"})


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def timestamp(value):
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(UTC)


def kst(value):
    """Storage/model/display form of a NEWS-owned instant: ISO 8601 with the +09:00 offset.
    Rows written before this contract stay in their UTC text; readers compare instants."""
    return timestamp(value).astimezone(KST).isoformat()


def kst_field(value):
    """KST form of an optional source time; an unparsable value is kept as-is, never invented."""
    if value in (None, ""):
        return None
    try:
        return kst(value)
    except (ValueError, TypeError, AttributeError):
        return value


def kst_projection(value, keys=TIME_KEYS):
    """Shallow copy with the named time keys re-expressed in KST (only keys that are present)."""
    return {
        key: kst_field(item) if key in keys else item for key, item in value.items()
    }


def same_content(old, new, *, ignore=()):
    """Stored payload vs re-read payload: exact key set and values, except that named time keys
    are equal as instants and a legacy-optional key may be absent from the stored row."""
    for key in set(old) | set(new):
        if key in ignore:
            continue
        if key not in old or key not in new:
            if key in LEGACY_OPTIONAL_KEYS and key not in old:
                continue
            return False
        before, after = old[key], new[key]
        if before == after:
            continue
        try:
            if (
                key in TIME_KEYS
                and before is not None
                and after is not None
                and timestamp(before) == timestamp(after)
            ):
                continue
        except (ValueError, TypeError, AttributeError):
            pass
        return False
    return True


class NewsDecisionStore:
    def __init__(self, path):
        self.base = NewsEpisodeStore(path)
        self.connection = self.base.connection
        with self.connection:
            for statement in (
                "CREATE TABLE IF NOT EXISTS news_decision_inputs (id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'READY', error TEXT)",
                "CREATE TABLE IF NOT EXISTS news_decision_events (id TEXT PRIMARY KEY, state TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS news_decision_calls (id TEXT PRIMARY KEY, stage TEXT NOT NULL, started_at TEXT NOT NULL, status TEXT NOT NULL, reservation INTEGER NOT NULL, tokens INTEGER NOT NULL, request TEXT NOT NULL, response TEXT, error TEXT)",
                "CREATE TABLE IF NOT EXISTS news_decision_cursors (source TEXT PRIMARY KEY, observed_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS news_decision_input_closures (input_id TEXT PRIMARY KEY, closed_at TEXT NOT NULL, reason TEXT NOT NULL, details TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS news_decision_control (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS news_decision_deferred (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, state TEXT NOT NULL, task TEXT NOT NULL)",
            ):
                self.connection.execute(statement)
            self.connection.execute(
                "UPDATE schema_meta SET ddl_sha256=? WHERE singleton=1",
                (_actual_ddl_digest(self.connection),),
            )

    def close(self):
        self.connection.close()

    def control(self, key):
        row = self.connection.execute(
            "SELECT value FROM news_decision_control WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def controls(self, prefix):
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT value FROM news_decision_control WHERE key LIKE ? ORDER BY key",
                (prefix + "%",),
            )
        ]

    def set_control(self, key, value):
        with self.connection:
            if value is None:
                self.connection.execute(
                    "DELETE FROM news_decision_control WHERE key=?", (key,)
                )
            else:
                self.connection.execute(
                    "INSERT INTO news_decision_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, encoded(value)),
                )

    def defer_candidate(self, candidate, now):
        """Persist screening outcome and input transition together, before restart."""
        import hashlib

        key = hashlib.sha256(
            encoded(sorted(candidate["evidence_ids"])).encode()
        ).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO news_decision_deferred VALUES(?,?,'WAITING',?) ON CONFLICT(id) DO UPDATE SET state='WAITING',task=excluded.task",
                (key, kst(now), encoded(candidate)),
            )
            self.connection.executemany(
                "UPDATE news_decision_inputs SET state='WAITING',error=NULL WHERE id=? AND state='READY'",
                [(i,) for i in candidate["evidence_ids"]],
            )

    def deferred_candidates(self, limit=20):
        return [
            dict(json.loads(row[1]), deferred_id=row[0], deferred_at=row[2])
            for row in self.connection.execute(
                "SELECT id,task,created_at FROM news_decision_deferred WHERE state='WAITING' ORDER BY julianday(created_at) DESC,id LIMIT ?",
                (limit,),
            )
        ]

    def finish_deferred(self, task, state, error=None):
        with self.connection:
            self.connection.execute(
                "UPDATE news_decision_deferred SET state=? WHERE id=?",
                (state, task["deferred_id"]),
            )
            self.connection.executemany(
                "UPDATE news_decision_inputs SET state=?,error=? WHERE id=? AND state='WAITING'",
                [(state, error, i) for i in task["evidence_ids"]],
            )

    def ingest(self, observations, now):
        with self.connection:
            for observation in observations:
                if timestamp(observation["observed_at"]) > timestamp(now):
                    raise ValueError("future observation")
                # New stored payload: every named time key in KST; text/url/ids/date-only values as collected.
                value = kst_projection(
                    dict(observation, observed_at=kst(observation["observed_at"]))
                )
                row = self.connection.execute(
                    "SELECT payload FROM news_decision_inputs WHERE id=?",
                    (value["id"],),
                ).fetchone()
                if row is not None and row[0] != encoded(value):
                    # A re-seen current revision moves observed_at; the exemption comes from the stored
                    # row's own provenance, never from what the re-read claims. Anything else is a reused id.
                    old = json.loads(row[0])
                    ignore = (
                        ("observed_at",)
                        if old.get("history_scope") == "current_revision_only"
                        else ()
                    )
                    if not same_content(old, value, ignore=ignore):
                        raise ValueError("observation id reused with different content")
                    continue
                self.connection.execute(
                    "INSERT OR IGNORE INTO news_decision_inputs(id,observed_at,payload) VALUES(?,?,?)",
                    (value["id"], value["observed_at"], encoded(value)),
                )

    def pending(self, limit=20):
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT payload FROM news_decision_inputs WHERE state='READY' ORDER BY julianday(observed_at),id LIMIT ?",
                (limit,),
            )
        ]

    def observation(self, key):
        """Original availability/provenance of an already ingested source version."""
        row = self.connection.execute(
            "SELECT payload FROM news_decision_inputs WHERE id=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def mark(self, ids, state, error=None):
        with self.connection:
            self.connection.executemany(
                "UPDATE news_decision_inputs SET state=?,error=? WHERE id=?",
                [(state, error, key) for key in ids],
            )

    def errors(self):
        return self.connection.execute(
            "SELECT id,error FROM news_decision_inputs WHERE state='ERROR' ORDER BY julianday(observed_at),id"
        ).fetchall()

    def error_details(self):
        return [
            dict(id=row[0], error=row[1], observed_at=row[2])
            for row in self.connection.execute(
                "SELECT id,error,observed_at FROM news_decision_inputs WHERE state='ERROR' ORDER BY julianday(observed_at),id"
            )
        ]

    def retry_errors(self, ids=None):
        with self.connection:
            # Failed calls remain evidence; a subsequent explicit retry receives
            # a different now and hence a fresh call identity.
            if ids is None:
                self.connection.execute(
                    "UPDATE news_decision_inputs SET state='READY',error=NULL WHERE state='ERROR'"
                )
                return
            states = dict(
                self.connection.execute(
                    "SELECT id,state FROM news_decision_inputs WHERE id IN (%s)"
                    % ",".join("?" * len(ids)),
                    list(ids),
                )
            )
            wrong = [key for key in ids if states.get(key) != "ERROR"]
            if wrong or not ids:
                raise ValueError("not an ERROR input: " + ", ".join(wrong))
            self.connection.executemany(
                "UPDATE news_decision_inputs SET state='READY',error=NULL WHERE id=? AND state='ERROR'",
                [(key,) for key in ids],
            )

    @staticmethod
    def duplicate_basis(payload):
        """Exact source version, including all unknown fields and source times.

        A collector's new observation/revision id is not a new published fact.
        URL/title similarity alone never proves that an input was processed.
        """
        if not payload.get("url") or not payload.get("text"):
            return None
        value = kst_projection(
            {k: v for k, v in payload.items() if k not in {"id", "observed_at"}}
        )
        return hashlib.sha256(encoded(value).encode()).hexdigest()

    def duplicate_error_candidates(self, now, *, state="ERROR"):
        failures = []
        for key, at, raw, error in self.connection.execute(
            "SELECT id,observed_at,payload,error FROM news_decision_inputs WHERE state=?",
            (state,),
        ):
            basis = self.duplicate_basis(json.loads(raw))
            if basis and timestamp(at) <= timestamp(now):
                failures.append(
                    dict(input_id=key, fingerprint=basis, previous_error=error)
                )
        if not failures:
            return []
        needed = {row["fingerprint"] for row in failures}
        completed = {}
        for key, at, raw in self.connection.execute(
            "SELECT id,observed_at,payload FROM news_decision_inputs WHERE state='DONE' ORDER BY julianday(observed_at),id"
        ):
            if timestamp(at) > timestamp(now):
                continue
            basis = self.duplicate_basis(json.loads(raw))
            if basis in needed:
                completed[basis] = key
        return [
            dict(row, matched_input_id=completed[row["fingerprint"]])
            for row in failures
            if row["fingerprint"] in completed
        ]

    def close_duplicate_errors(self, now):
        """Close only proven duplicates; retain original input/error and a durable receipt.

        CLOSED is intentionally different from DONE: this particular failed
        revision was not newly analyzed. No READY inputs or events are touched.
        """
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.duplicate_error_candidates(now)
            for row in rows:
                self.connection.execute(
                    "INSERT INTO news_decision_input_closures VALUES(?,?,?,?)",
                    (row["input_id"], kst(now), "DUPLICATE_OF_PROCESSED", encoded(row)),
                )
                changed = self.connection.execute(
                    "UPDATE news_decision_inputs SET state='CLOSED' WHERE id=? AND state='ERROR'",
                    (row["input_id"],),
                )
                if changed.rowcount != 1:
                    raise ValueError("duplicate closure input changed concurrently")
        return dict(
            closed=len(rows),
            reason="DUPLICATE_OF_PROCESSED",
            input_ids=[r["input_id"] for r in rows],
            model_calls=0,
        )

    STALE_ERROR_HOURS = 72

    def close_stale_errors(self, now, *, hours=STALE_ERROR_HOURS):
        """Close ERROR inputs observed more than ``hours`` ago without re-analysis (the same 72h
        rule as stale event rechecks). The original input, its error and a closure receipt stay;
        an explicit ``retry_errors`` on a CLOSED id is still possible. READY/WAITING rows are untouched."""
        limit = kst(timestamp(now) - timedelta(hours=hours))
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                "SELECT id,error,observed_at FROM news_decision_inputs WHERE state='ERROR' AND julianday(observed_at)<julianday(?) ORDER BY julianday(observed_at),id",
                (limit,),
            ).fetchall()
            for key, error, observed_at in rows:
                self.connection.execute(
                    "INSERT OR IGNORE INTO news_decision_input_closures VALUES(?,?,?,?)",
                    (
                        key,
                        kst(now),
                        "STALE_ERROR",
                        encoded(
                            dict(
                                input_id=key,
                                previous_error=error,
                                observed_at=observed_at,
                                hours=hours,
                            )
                        ),
                    ),
                )
                self.connection.execute(
                    "UPDATE news_decision_inputs SET state='CLOSED' WHERE id=? AND state='ERROR'",
                    (key,),
                )
        return dict(
            closed=len(rows),
            reason="STALE_ERROR",
            input_ids=[r[0] for r in rows],
            model_calls=0,
        )

    def close_duplicate_deferred(self, now):
        """Only whole, exactly processed candidate groups can bypass recovery inference."""
        if not self.deferred_candidates(limit=1):
            return 0
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            duplicates = {
                r["input_id"]: r
                for r in self.duplicate_error_candidates(now, state="WAITING")
            }
            closed = 0
            for task in self.deferred_candidates(limit=-1):
                if (
                    self.control("accepted_inputs:" + task["deferred_id"])
                    or not set(task["evidence_ids"]) <= duplicates.keys()
                ):
                    continue
                for key in task["evidence_ids"]:
                    self.connection.execute(
                        "INSERT INTO news_decision_input_closures VALUES(?,?,?,?)",
                        (
                            key,
                            kst(now),
                            "DUPLICATE_OF_PROCESSED",
                            encoded(duplicates[key]),
                        ),
                    )
                    self.connection.execute(
                        "UPDATE news_decision_inputs SET state='CLOSED' WHERE id=? AND state='WAITING'",
                        (key,),
                    )
                    closed += 1
                self.connection.execute(
                    "UPDATE news_decision_deferred SET state='CLOSED' WHERE id=?",
                    (task["deferred_id"],),
                )
        return closed

    def events(self):
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT state FROM news_decision_events ORDER BY id"
            )
        ]

    def save_event(self, event, *, accepted_inputs=None):
        self.save_events([event], accepted_inputs=accepted_inputs)

    def save_events(self, events, *, cursor=None, accepted_inputs=None):
        with self.connection:
            self.connection.executemany(
                "INSERT INTO news_decision_events(id,state) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                [(event["id"], encoded(event)) for event in events],
            )
            if accepted_inputs:
                import hashlib

                assert len(events) == 1
                event = events[0]
                key = (
                    "accepted_inputs:"
                    + hashlib.sha256(
                        encoded(sorted(accepted_inputs)).encode()
                    ).hexdigest()
                )
                self.connection.execute(
                    "INSERT INTO news_decision_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (
                        key,
                        encoded(
                            dict(
                                event_id=event["id"],
                                at=event["updated_at"],
                                input_ids=sorted(accepted_inputs),
                            )
                        ),
                    ),
                )
            if cursor is not None:
                self.connection.execute(
                    "INSERT INTO news_decision_cursors VALUES(?,?) ON CONFLICT(source) DO UPDATE SET observed_at=excluded.observed_at",
                    (cursor[0], kst(cursor[1])),
                )

    def begin_call(self, key, stage, request, now, reservation, budget):
        # Budget day = KST calendar day of the call instant, as an instant range, so legacy
        # UTC-text rows and reservations (STARTED/ERROR rows keep their tokens) count once.
        # The budget bounds the analysis stages only: the screen keeps classifying (and
        # attaching observations to known events) after the day's analysis allowance is spent.
        day_start = (
            timestamp(now)
            .astimezone(KST)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            previous = self.connection.execute(
                "SELECT status,response FROM news_decision_calls WHERE id=?", (key,)
            ).fetchone()
            if previous:
                if previous[0] != "OK":
                    raise ValueError(
                        "previous call incomplete or failed; explicit retry required"
                    )
                return json.loads(previous[1])
            if budget is not None and stage in BUDGETED_STAGES:
                spent = self.connection.execute(
                    "SELECT coalesce(sum(tokens),0) FROM news_decision_calls WHERE stage IN (?,?) AND julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?)",
                    (
                        *BUDGETED_STAGES,
                        day_start.isoformat(),
                        (day_start + timedelta(days=1)).isoformat(),
                    ),
                ).fetchone()[0]
                if spent + reservation > budget:
                    raise ValueError("daily token budget exhausted")
            self.connection.execute(
                "INSERT INTO news_decision_calls VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    stage,
                    kst(now),
                    "STARTED",
                    reservation,
                    reservation,
                    encoded(request),
                    None,
                    None,
                ),
            )
        return None

    def record_failed_attempt(
        self, key, stage, model, now, reservation, category, budget=None
    ):
        """A chain member failed inside the journaled call ``key`` and another member is about to be tried.

        Every journal row is one physical attempt. The call's own row (STARTED, holding ``reservation``)
        now stands for the *next* attempt, so the finished one gets its own ERROR row
        (id = call id + '#' + model, tokens = reservation), which keeps the day's budget honest and
        stays as a durable record. The row is written only if the next attempt still fits the budget:
        otherwise this raises like begin_call and writes nothing, and the call row (finished as ERROR
        by the engine, tokens = reservation) is the record of the attempt that just failed. A last
        member's failure is not recorded here for the same reason: the call row is that attempt."""
        if budget is not None and stage in BUDGETED_STAGES:
            day_start = (
                timestamp(now)
                .astimezone(KST)
                .replace(hour=0, minute=0, second=0, microsecond=0)
            )
            spent = self.connection.execute(
                "SELECT coalesce(sum(tokens),0) FROM news_decision_calls WHERE stage IN (?,?) AND julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?)",
                (
                    *BUDGETED_STAGES,
                    day_start.isoformat(),
                    (day_start + timedelta(days=1)).isoformat(),
                ),
            ).fetchone()[0]
            if (
                spent + reservation > budget
            ):  # spent already holds this call's reservation for the next attempt
                raise ValueError("daily token budget exhausted")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO news_decision_calls VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key + "#" + model,
                    stage,
                    kst(now),
                    "ERROR",
                    reservation,
                    reservation,
                    encoded({"attempt_of": key, "model": model}),
                    None,
                    "chain member failed: " + category,
                ),
            )

    def failed_attempts(self, key):
        """Durable per-member failures recorded under one call id, oldest first. The last attempt of a
        wholly failed call is the call row itself (its error names the category)."""
        rows = self.connection.execute(
            "SELECT id,started_at,tokens,error FROM news_decision_calls WHERE id LIKE ? AND status='ERROR' ORDER BY started_at",
            (key + "#%",),
        ).fetchall()
        return [
            dict(model=row[0].split("#", 1)[1], at=row[1], tokens=row[2], error=row[3])
            for row in rows
        ]

    def finish_call(self, key, response=None, error=None):
        with self.connection:
            if error:
                self.connection.execute(
                    "UPDATE news_decision_calls SET status='ERROR',error=? WHERE id=?",
                    (error, key),
                )
            else:
                usage = response.get("_usage", {})
                actual = usage.get("total_tokens")
                if (
                    not isinstance(actual, int)
                    or isinstance(actual, bool)
                    or actual < 0
                ):
                    actual = self.connection.execute(
                        "SELECT reservation FROM news_decision_calls WHERE id=?", (key,)
                    ).fetchone()[0]
                self.connection.execute(
                    "UPDATE news_decision_calls SET status='OK',response=?,tokens=? WHERE id=?",
                    (encoded(response), actual, key),
                )

    def counts(self):
        return dict(
            self.connection.execute(
                "SELECT state,count(*) FROM news_decision_inputs GROUP BY state"
            )
        )

    def cursor(self, source, default):
        row = self.connection.execute(
            "SELECT observed_at FROM news_decision_cursors WHERE source=?", (source,)
        ).fetchone()
        return row[0] if row else default

    def advance(self, source, observed_at):
        with self.connection:
            self.connection.execute(
                "INSERT INTO news_decision_cursors VALUES(?,?) ON CONFLICT(source) DO UPDATE SET observed_at=excluded.observed_at",
                (source, kst(observed_at)),
            )
