"""Test-only legacy Ops Kernel schema builder (Plan/SDS 2026-08-30 §4).

Historical v4-v13 compatibility stays provable here without exercising any
production constructor: exact legacy DDL is materialized byte-exactly.
Production v15 exposes only the explicit v14 -> v15 migration; migrating a
v4-v13 database in production requires a separate reviewed command.
"""

import sqlite3

from tokmo.records import ops_kernel as _module

LEGACY_VERSIONS = {
    4: _module._LEGACY_SCHEMA_SQL_V4,
    6: _module._LEGACY_SCHEMA_SQL_V6,
    7: _module._LEGACY_SCHEMA_SQL_V7,
    8: _module._LEGACY_SCHEMA_SQL_V8,
    9: _module._LEGACY_SCHEMA_SQL_V9,
    10: _module._LEGACY_SCHEMA_SQL_V10,
    11: _module._LEGACY_SCHEMA_SQL_V11,
    12: _module._LEGACY_SCHEMA_SQL_V12,
    13: _module._LEGACY_SCHEMA_SQL_V13,
    14: _module._LEGACY_SCHEMA_SQL_V14,
}


def build_legacy(path, version):
    """Create an exact legacy database at path (0600, 0700 parent)."""
    import os

    if version not in LEGACY_VERSIONS:
        raise ValueError("unknown legacy version")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(LEGACY_VERSIONS[version])
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)
    return path
