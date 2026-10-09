"""Private receipt-bound scope→native-order-ref persistence (Plan §17.1).

The 300s holdings owner is the only writer; Pulse reads this store instead
of ever calling ACCOUNTS. The raw ref is credential-adjacent material: the
file is 0600 under a 0700 parent, reads return a redacted wrapper whose
repr/str never contain raw values, and callers must use ``reveal(scope)``
only at the provider-call boundary — never in logs, receipts, prompts, or
messages.
"""

import os
import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path

from tokmo.values.account_scope_patterns import ACCOUNT_SCOPE_RE


class NativeRefCorruptError(ValueError):
    pass


class NativeRefMappingV1:
    """Redacted scope→ref mapping; raw values only through ``reveal``."""

    def __init__(self, refs: dict, receipt_binding: "str | None") -> None:
        self._refs = dict(refs)
        self.receipt_binding = receipt_binding

    def scopes(self) -> frozenset:
        return frozenset(self._refs)

    def reveal(self, scope: str) -> "str | None":
        return self._refs.get(scope)

    def __repr__(self) -> str:
        return (
            f"<NativeRefMappingV1 REDACTED scopes={len(self._refs)} "
            f"binding={self.receipt_binding!r}>"
        )

    def __str__(self) -> str:
        return self.__repr__()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_native_refs(
  account_scope TEXT PRIMARY KEY,
  native_ref TEXT NOT NULL CHECK(length(native_ref) BETWEEN 1 AND 128),
  receipt_binding TEXT NOT NULL CHECK(length(receipt_binding) BETWEEN 1 AND 128),
  updated_at TEXT NOT NULL
) STRICT;
"""


def _aware_utc(value) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


class AccountNativeRefStoreV1:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=1.0)
        try:
            if self.path.exists():
                value = os.lstat(self.path)
                if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                    raise NativeRefCorruptError("unsafe native ref store")
                os.chmod(self.path, 0o600)
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            return connection
        except sqlite3.DatabaseError:
            connection.close()
            raise NativeRefCorruptError("corrupt native ref store") from None
        except BaseException:
            connection.close()
            raise

    def replace_refs(self, refs: dict, *, receipt_binding: str) -> None:
        """Whole-inventory replacement bound to one holdings receipt."""
        if type(receipt_binding) is not str or not receipt_binding:
            raise ValueError("receipt binding is mandatory")
        if type(refs) is not dict or not refs:
            raise ValueError("refs must be a non-empty mapping")
        for scope, ref in refs.items():
            if type(scope) is not str or ACCOUNT_SCOPE_RE.fullmatch(scope) is None:
                raise ValueError("only live-order-capable toss scopes carry refs")
            if type(ref) is not str or not 1 <= len(ref) <= 128:
                raise ValueError("invalid native ref")
        now = self._clock()
        if not _aware_utc(now):
            raise ValueError("clock must produce aware UTC")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM account_native_refs")
                connection.executemany(
                    "INSERT INTO account_native_refs VALUES(?,?,?,?)",
                    (
                        (
                            scope,
                            ref,
                            receipt_binding,
                            now.isoformat().replace("+00:00", "Z"),
                        )
                        for scope, ref in sorted(refs.items())
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except sqlite3.DatabaseError:
            raise NativeRefCorruptError("corrupt native ref store") from None
        finally:
            connection.close()
        if hasattr(os, "getuid") and self.path.stat().st_uid == os.getuid():
            os.chmod(self.path, 0o600)

    def read_refs(self) -> NativeRefMappingV1:
        if not self.path.exists():
            return NativeRefMappingV1({}, None)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT account_scope, native_ref, receipt_binding "
                "FROM account_native_refs ORDER BY account_scope"
            ).fetchall()
        except sqlite3.DatabaseError:
            raise NativeRefCorruptError("corrupt native ref store") from None
        finally:
            connection.close()
        refs = {}
        bindings = set()
        for scope, ref, binding in rows:
            if ACCOUNT_SCOPE_RE.fullmatch(scope) is None:
                raise NativeRefCorruptError("non-canonical stored scope")
            refs[scope] = ref
            bindings.add(binding)
        if len(bindings) > 1:
            raise NativeRefCorruptError("mixed receipt bindings")
        return NativeRefMappingV1(refs, bindings.pop() if bindings else None)
