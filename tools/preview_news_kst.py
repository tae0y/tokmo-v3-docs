"""Read-only D3 preview of explicitly named normalized NEWS timestamps.

No UPDATE/apply mode. Does not traverse raw API JSON, model requests/responses or date-only fields.
This is a cutover assessment, not a migration or authorization to mix old/new storage formats.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

TABLES = {
    "news_documents", "source_families", "sources", "source_connectors", "intake_requests", "intake_receipts",
    "disclosures", "dart_revisions", "news_decision_inputs", "news_decision_cursors",
}
FIELDS = {"published_at", "updated_at", "first_seen_at", "last_seen_at", "created_at", "retired_at", "registered_at",
          "disabled_at", "requested_at", "expires_at", "started_at", "finished_at", "compacted_at",
          "first_observed_at", "last_observed_at", "observed_at"}


def project(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("not an aware timestamp")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("not an aware timestamp")
    # Keep strict source-store width (32) where used. No invented time for date-only values.
    return instant.astimezone(ZoneInfo("Asia/Seoul")).isoformat(timespec="microseconds" if len(value) == 32 else "auto")


def preview(path):
    path = Path(path).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    report = {"mode": "READ_ONLY_PREVIEW", "tables": {}, "invalid_fields": 0,
              "writes": 0, "raw_json_and_model_audits": "excluded", "source_writer_cutover_required": True}
    try:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(TABLES & present):
            columns = [row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')]
            fields = [field for field in columns if field in FIELDS]
            rows = list(connection.execute('SELECT * FROM "' + table + '"'))
            projected_rows = [list(row) for row in rows]
            byfield = {}
            identity = hashlib.sha256()
            converted_identity = hashlib.sha256()
            # Hash all non-time values, including IDs/hashes/raw JSON, in the same row order.
            for row in rows:
                stable = [value for name, value in zip(columns, row) if name not in fields]
                data = json.dumps(stable, ensure_ascii=False, default=str).encode()
                identity.update(data)
            for field in fields:
                index = columns.index(field)
                values, projected, invalid = [], [], 0
                for ordinal, row in enumerate(rows):
                    if row[index] is None:
                        continue
                    try:
                        value = project(row[index])
                    except (ValueError, TypeError):
                        invalid += 1
                        continue
                    values.append((row[index], ordinal))
                    projected.append((value, ordinal))
                    projected_rows[ordinal][index] = value
                byfield[field] = {"nonnull_valid": len(values), "would_change": sum(a[0] != b[0] for a, b in zip(values, projected)),
                    "invalid": invalid,
                    "lexical_order_would_change": [row[1] for row in sorted(values)] != [row[1] for row in sorted(projected)]}
                report["invalid_fields"] += invalid
            for row in projected_rows:
                stable = [value for name, value in zip(columns, row) if name not in fields]
                converted_identity.update(json.dumps(stable, ensure_ascii=False, default=str).encode())
            report["tables"][table] = {"rows": len(rows), "fields": byfield,
                "non_time_values_sha256": identity.hexdigest(), "projected_non_time_values_sha256": converted_identity.hexdigest()}
    finally:
        connection.rollback()
        connection.close()
    report["cutover_notes"] = [
        "external_news compares published_at inside identity tuples; time representation alone must not increment revision_no",
        "TEXT CHECK/order/range predicates need one consistent storage basis or instant comparisons before any mixed writes",
        "Keep DART receipt IDs, rcept_dt partitions, revision IDs and canonical_json hashes unchanged",
        "Raw API JSON and news_decision_calls are audit originals, excluded from this preview",
    ]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    report = preview(args.database)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["invalid_fields"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
