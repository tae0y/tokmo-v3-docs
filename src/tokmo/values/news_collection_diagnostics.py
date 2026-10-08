"""Closed NEWS collection diagnostics: never render exception text or names."""

import sqlite3
import re

CAUSES = frozenset({
    "UNKNOWN", "STORAGE_BUSY", "STORAGE_INTEGRITY", "STORAGE_ERROR",
    "STORAGE_SCHEMA", "STORAGE_PATH", "TIMEOUT", "CONNECTION_ERROR",
    "FILE_MISSING", "FILE_ACCESS", "OS_ERROR", "CHILD_TIMEOUT", "CHILD_EXIT",
    "CHILD_NO_OUTPUT", "CHILD_INVALID_OUTPUT", "CHILD_LAUNCH",
    "INCOMPLETE_STORAGE", "NO_HTTP_STATUS", "CONTRACT_MISMATCH", "CONFIG_UNAVAILABLE",
    "HTTP_5XX", "HTTP_4XX", "HTTP_AUTH", "HTTP_REDIRECT",
})
STAGES = frozenset({"UNKNOWN", "PREPARE", "RESERVE", "FETCH", "PERSIST", "COMPLETE", "CHILD", "OPERATION"})


def safe_cause(value):
    if type(value) is str and (value in CAUSES or re.fullmatch(r"HTTP_[1-5][0-9]{2}|PROVIDER_(010|011|012|020|021|101)", value)):
        return value
    return "UNKNOWN"


def sanitize_diagnostics(raw):
    """Validate the small optional wire extension; discard all unlisted keys."""
    if type(raw) is not dict or type(raw.get("stage")) is not str or raw["stage"] not in STAGES:
        raise ValueError("invalid_news_diagnostics")
    result = dict(stage=raw["stage"], cause=None if raw.get("cause") is None else safe_cause(raw["cause"]))
    for key in ("received", "persisted", "elapsed_ms"):
        value = raw.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
            raise ValueError("invalid_news_diagnostics")
        result[key] = value
    if (result["received"] is not None and result["persisted"] is not None
            and result["persisted"] > result["received"]):
        raise ValueError("invalid_news_diagnostics")
    code = raw.get("http_status")
    if code is not None and (type(code) is not int or not 100 <= code <= 599):
        raise ValueError("invalid_news_diagnostics")
    result["http_status"] = code
    trace = raw.get("trace")
    if type(trace) is not str or not re.fullmatch(r"[0-9a-f]{16}", trace):
        raise ValueError("invalid_news_diagnostics")
    result["trace"] = trace
    scope = raw.get("progress_scope", "invocation")
    if scope not in ("invocation", "page"):
        raise ValueError("invalid_news_diagnostics")
    result["progress_scope"] = scope
    return result


def collection_complete(entry):
    if entry["status"] not in {"COLLECTED", "EMPTY"}:
        return False
    if entry.get("diagnostics") is None:  # legacy receipt
        return True
    try:
        diag = sanitize_diagnostics(entry["diagnostics"])
    except ValueError:
        return False
    return ((diag["received"] is None or diag["received"] == diag["persisted"])
            and diag["cause"] is None and diag["stage"] == "COMPLETE")


def diagnostic_summary(raw):
    if raw is None:
        return ""
    try:
        diag = sanitize_diagnostics(raw)
    except ValueError:
        return ""
    return f"{diag['stage']} saved={diag['persisted']}/{diag['received']} trace={diag['trace']}"


def exception_cause(error: Exception) -> str:
    """Classify known exception types/codes; arbitrary text stays unknown."""
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", 0)
        if type(code) is int and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return "STORAGE_BUSY"
        if isinstance(error, sqlite3.IntegrityError):
            return "STORAGE_INTEGRITY"
        return "STORAGE_ERROR"
    if isinstance(error, RuntimeError) and error.args == ("schema_mismatch",):
        cause = error.__cause__
        if isinstance(cause, sqlite3.Error) and exception_cause(cause) == "STORAGE_BUSY":
            return "STORAGE_BUSY"
        return "STORAGE_SCHEMA"
    if (isinstance(error, OSError) and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in {
        "unsafe_path_swap", "unsafe_symlink", "unsafe_mode", "unsafe_owner",
        "unsafe_nonregular", "sqlite_pragma_mismatch",
    }):
        return "STORAGE_PATH"
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if isinstance(error, ConnectionError):
        return "CONNECTION_ERROR"
    if isinstance(error, FileNotFoundError):
        return "FILE_MISSING"
    if isinstance(error, PermissionError):
        return "FILE_ACCESS"
    if isinstance(error, OSError):
        return "OS_ERROR"
    return "UNKNOWN"


def failure_diagnostics(error, *, elapsed_ms, trace, stage="OPERATION"):
    return sanitize_diagnostics(dict(stage=stage, cause=exception_cause(error),
        elapsed_ms=elapsed_ms, trace=trace, received=None, persisted=None,
        http_status=None))
