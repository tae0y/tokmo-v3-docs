"""Two-tier read-only OpenDART official primary-evidence pipeline.

SDS 2026-09-01 A2/A3/A4 (§Z2·§V1..V5·§V10·§V11·§W2, decisions 18–20):

- Tier 1 reads the local ``DisclosureStore`` (no network, no credentials).
- Tier 2 fires only when tier 1 found ZERO window rows and the composed
  live seam is bound: ONE ``build_list_request`` through the injected
  ``OpenDartLiveTransport`` unwrapped success-only into
  ``parse_disclosure_list`` (latest row only — an accepted bounded false
  negative, §V11), retry 0.
- Correlation is mandatory: only refs whose ``matched_action`` sits in the
  caller-supplied allowed families are ever returned (§Z3).
- A live ref promotes ONLY through a validated, atomically persisted,
  read-back official-evidence receipt (§V3, decision 19). Local rows are
  themselves local official evidence and need no receipt.
- Every credential/transport/provider/parse/store degradation is a typed
  fail-closed outcome (§V5): refs 0, failure receipt, production continues.

Inert unless a composition root injects the reader; the live seam's
transport/credential closures are constructed only in ``composition.py``
(live-import guard) and are exercised lazily at lookup time — never at
assembly time.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tokmo.facts.opendart import (
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
    build_list_request,
    parse_disclosure_list,
)
from tokmo.records.opendart_disclosures import DisclosureStore, UnsafeStore
from tokmo.values.filings import OpenDartDisclosureFact
from tokmo.values.news_event_confirmation import (
    CORP_ACTION_PATTERNS_V1,
    OfficialEvidenceRefV1,
)

OFFICIAL_EVIDENCE_MAX_REFS = 8
OFFICIAL_RECEIPT_MAX_FILES = 40
OFFICIAL_RECEIPT_MAX_BYTES = 4096
# §V5 closed typed failure classes (+ the §V3 promotion-gate refusal).
OFFICIAL_LOOKUP_FAILURES = frozenset(
    {
        "AUTH_UNAVAILABLE",
        "PROVIDER_TRANSPORT",
        "PROVIDER_STATUS",
        "SCHEMA_INVALID",
        "STORE_UNAVAILABLE",
        "RECEIPT_UNVERIFIED",
    }
)


@dataclass(frozen=True)
class OfficialLookupOutcomeV1:
    """One logical lookup attempt's refs plus its §V10 counter deltas."""

    refs: tuple = ()
    local_reads: int = 0
    http_starts: int = 0
    refs_returned: int = 0
    refs_correlated: int = 0
    failure: "str | None" = None


@dataclass(frozen=True)
class OfficialLiveSeamV1:
    """Composition-owned tier-2 seam: both members are lazy closures —
    nothing here touches credentials or network until called, and the
    transport itself is CONSTRUCTED only inside the guarded lookup
    (§V5, SOL M2)."""

    credential_loader: object  # () -> api_key str; raises on any refusal
    transport_factory: object  # () -> (OpenDartRequest) -> transport union


def _matched_action(report_nm):
    for action, patterns in CORP_ACTION_PATTERNS_V1.items():
        if any(pattern in report_nm for pattern in patterns):
            return action
    return None


def _compact_day(value):
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "")[:8]


def _iso_day(compact):
    day = str(compact)
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def build_official_receipt_sink(root):
    """Owner-only durable receipt writer with read-back (§V3/§V10).

    Atomic (0700 dir / 0600 file), identity-prefixed idempotent names,
    bounded retention (≤40 files, ≤4KiB each). Returns the re-read parsed
    payload on success and None on any write/read-back degradation —
    promotion then fails closed."""
    directory = Path(root) / "news" / "official_evidence_receipts"

    def write_receipt(identity, payload):
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            if len(encoded) > OFFICIAL_RECEIPT_MAX_BYTES:
                return None
            name = f"official-{identity[:16]}.json"
            fd, tmp_name = tempfile.mkstemp(prefix=".receipt.", dir=directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                os.replace(tmp_name, directory / name)
            finally:
                if Path(tmp_name).exists():
                    os.unlink(tmp_name)
            receipts = sorted(
                (
                    path
                    for path in directory.iterdir()
                    if path.name.startswith("official-")
                ),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
            for stale in receipts[:-OFFICIAL_RECEIPT_MAX_FILES]:
                stale.unlink(missing_ok=True)
            read_back = json.loads((directory / name).read_text("utf-8"))
            return read_back if read_back == payload else None
        except (OSError, ValueError):
            return None

    return write_receipt


def build_official_evidence_reader(db_path, *, live=None, receipt_sink=None):
    """callable(stock_codes, date_from, date_to, allowed_families, ...)
    -> :class:`OfficialLookupOutcomeV1`; never raises (§V5)."""
    db_path = Path(db_path)

    def _receipt(identity, payload):
        if receipt_sink is None:
            return None
        return receipt_sink(identity, payload)

    def read_official_evidence(
        stock_codes,
        date_from,
        date_to,
        allowed_families,
        *,
        candidate_id="",
        attempt_no=0,
        now=None,
        event_type="",
    ):
        codes = tuple(
            dict.fromkeys(
                str(code).strip() for code in stock_codes if str(code).strip()
            )
        )
        families = frozenset(allowed_families or ())
        if not codes or not families:
            return OfficialLookupOutcomeV1()

        window = (_compact_day(date_from), _compact_day(date_to))

        def _identity(tier):
            # §V10: the receipt identity carries the ACTUAL tier.
            return hashlib.sha256(
                "\0".join(
                    (str(candidate_id), str(attempt_no), tier, ",".join(codes), *window)
                ).encode()
            ).hexdigest()

        def _counters(*, failures, refs_correlated):
            # Full six-counter snapshot (§V10): this call is exactly one
            # logical attempt.
            return {
                "official_attempts": 1,
                "official_local_reads": local_reads,
                "official_http_starts": http_starts,
                "official_refs_returned": refs_returned,
                "official_refs_correlated": refs_correlated,
                "official_failures": failures,
            }

        def failed(tier, code):
            _receipt(
                _identity(tier),
                {
                    "receipt": "news-official-evidence-receipt-v1",
                    "typed": code,
                    "tier": tier,
                    "stock_codes": list(codes),
                    "window": list(window),
                    "allowed_families": sorted(families),
                    "event_type": str(event_type),
                    "counters": _counters(failures=1, refs_correlated=0),
                },
            )
            return OfficialLookupOutcomeV1(
                local_reads=local_reads,
                http_starts=http_starts,
                refs_returned=refs_returned,
                failure=code,
            )

        local_reads = 1
        http_starts = 0
        refs_returned = 0
        corp_code = None
        window_rows = None
        try:
            store = DisclosureStore.open_readonly(db_path)
        except (UnsafeStore, OSError, sqlite3.Error):
            store = None
        if store is not None:
            try:
                placeholders = ",".join("?" for _ in codes)
                window_rows = store.connection.execute(
                    "select rcept_no,report_nm,stock_code,rcept_dt"
                    " from disclosures"
                    f" where stock_code in ({placeholders})"
                    " and rcept_dt between ? and ?"
                    " order by rcept_dt desc,rcept_no desc",
                    (*codes, *window),
                ).fetchall()
                corp_row = store.connection.execute(
                    "select corp_code from disclosures"
                    f" where stock_code in ({placeholders})"
                    " order by rcept_dt desc limit 1",
                    codes,
                ).fetchone()
                corp_code = corp_row[0] if corp_row else None
            except sqlite3.Error:
                window_rows = None
            finally:
                store.close()
        if window_rows is None:
            # A degraded/absent local store also blocks tier 2: corp_code
            # is resolvable locally only (§Z2 fail-closed, no guessing).
            return failed("local", "STORE_UNAVAILABLE")
        refs = []
        for rcept_no, report_nm, stock_code, rcept_dt in window_rows or ():
            action = _matched_action(str(report_nm))
            if action is None:
                continue
            refs_returned += 1
            if action not in families:
                continue
            refs.append(
                OfficialEvidenceRefV1(
                    "opendart",
                    str(rcept_no),
                    str(report_nm),
                    str(stock_code),
                    _iso_day(rcept_dt),
                    action,
                )
            )
            if len(refs) >= OFFICIAL_EVIDENCE_MAX_REFS:
                break
        if refs or window_rows:
            # Window coverage exists locally: tier 2 never fires (§Z2);
            # incompatible-only coverage stays media-confirmed (JB-2).
            return OfficialLookupOutcomeV1(
                refs=tuple(refs),
                local_reads=local_reads,
                refs_returned=refs_returned,
                refs_correlated=len(refs),
            )
        if live is None or corp_code is None or len(codes) != 1:
            # No live seam, or corp_code unresolvable locally (§Z2): the
            # bounded fallback is structurally unavailable — fail closed
            # without inventing identity.
            return OfficialLookupOutcomeV1(
                local_reads=local_reads, refs_returned=refs_returned
            )
        # ---- tier 2: ONE bounded live list read, retry 0 (§V-2/§W-2) ----
        try:
            api_key = live.credential_loader()
        except Exception:
            # §V10 (SOL M3): a credential refusal never starts transport —
            # http_starts stays 0.
            return failed("live", "AUTH_UNAVAILABLE")
        try:
            # §V5 (SOL M2): the transport is constructed HERE, inside the
            # guarded lookup — never at composition/assembly time.
            transport = live.transport_factory()
            request = build_list_request(
                corp_code=str(corp_code),
                begin_date=window[0],
                end_date=window[1],
                api_key=api_key,
            )
        except Exception:
            return failed("live", "PROVIDER_TRANSPORT")
        http_starts = 1  # counted immediately before the actual invocation
        try:
            result = transport(request)
        except Exception:
            return failed("live", "PROVIDER_TRANSPORT")
        if isinstance(result, _OpenDartTransportFailure):
            return failed("live", "PROVIDER_TRANSPORT")
        if not isinstance(result, _OpenDartTransportSuccess):
            return failed("live", "PROVIDER_TRANSPORT")
        if now is None:
            return failed("live", "SCHEMA_INVALID")
        parsed = parse_disclosure_list(
            result.body, corp_code=str(corp_code), stock_code=codes[0], observed_at=now
        )
        if not isinstance(parsed, OpenDartDisclosureFact):
            code = (
                "PROVIDER_STATUS"
                if type(parsed).__name__ == "_ProviderStatusFailure"
                else "SCHEMA_INVALID"
            )
            return failed("live", code)
        receipt_day = (
            parsed.receipt_date.strftime("%Y%m%d")
            if isinstance(parsed.receipt_date, date)
            else _compact_day(parsed.receipt_date)
        )
        action = _matched_action(str(parsed.report_name))
        # §V10 (SOL M3): a ref is "returned" only after BOTH the parser
        # and the report-name action pattern validated it.
        if action is not None:
            refs_returned += 1
        if (
            action is None
            or action not in families
            or not (window[0] <= receipt_day <= window[1])
        ):
            # latest-row-only false negative accepted (§V11, JB-13).
            return OfficialLookupOutcomeV1(
                local_reads=local_reads,
                http_starts=http_starts,
                refs_returned=refs_returned,
            )
        ref = OfficialEvidenceRefV1(
            "opendart",
            str(parsed.receipt_number),
            str(parsed.report_name),
            codes[0],
            _iso_day(receipt_day),
            action,
        )
        # §V3 (decision 19): the validated durable receipt IS the local
        # official evidence — no read-back, no promotion.
        read_back = _receipt(
            _identity("live"),
            {
                "receipt": "news-official-evidence-receipt-v1",
                "typed": "OK",
                "tier": "live",
                "stock_codes": list(codes),
                "window": list(window),
                "allowed_families": sorted(families),
                "event_type": str(event_type),
                "ref": {
                    "source": ref.source,
                    "ref_id": ref.ref_id,
                    "report_name": ref.report_name,
                    "stock_code": ref.stock_code,
                    "receipt_date": ref.receipt_date,
                    "matched_action": ref.matched_action,
                },
                "counters": _counters(failures=0, refs_correlated=1),
            },
        )
        if not read_back or read_back.get("ref", {}).get("ref_id") != ref.ref_id:
            return failed("live", "RECEIPT_UNVERIFIED")
        return OfficialLookupOutcomeV1(
            refs=(ref,),
            local_reads=local_reads,
            http_starts=http_starts,
            refs_returned=refs_returned,
            refs_correlated=1,
        )

    return read_official_evidence
