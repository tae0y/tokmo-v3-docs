"""Fixture-first EP-1 tracer. Live execution requires controller injection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tokmo.facts.opendart import (
    OpenDartCorpMapping,
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
    build_corp_code_request,
    parse_corp_code_zip,
)
from tokmo.facts.opendart_source_contract import OpenDartTraceJsonRequest

PHASE = "KR_QUANT_SOURCE_CONTRACT_V1"
EXACT_LIVE_FLAG = "--phase=KR_QUANT_SOURCE_CONTRACT_V1"
CAPS = {
    "corpCode": 1,
    "fnlttSinglAcnt": 10,
    "fnlttSinglAcntAll": 5,
    "stockTotqySttus": 5,
}
TOSS_CAPS = {"TOKEN": 1, "STOCKS": 1}
ALL_CAPS = TOSS_CAPS | CAPS
# Historical EP-1 verification-campaign identities. They are retained for
# immutable ledger compatibility and are not production-universe policies.
TOSS_V2_PHASE = "KR_QUANT_SOURCE_CONTRACT_TOSS_V2"
DART_V2_PHASE = "KR_QUANT_SOURCE_CONTRACT_DART_V2"
V2_INCREMENTAL_CAPS = {
    TOSS_V2_PHASE: {"TOKEN": 1, "STOCKS": 1},
    DART_V2_PHASE: {
        "corpCode": 1, "fnlttSinglAcnt": 4,
        "fnlttSinglAcntAll": 4, "stockTotqySttus": 2,
    },
}
PRIOR_ACTUAL_STARTS = {
    "TOKEN": 2, "STOCKS": 2, "corpCode": 2,
    "fnlttSinglAcnt": 2, "fnlttSinglAcntAll": 2,
    "stockTotqySttus": 0,
}
V2_CUMULATIVE_CAPS = {
    endpoint: PRIOR_ACTUAL_STARTS[endpoint] + maximum
    for phase_caps in V2_INCREMENTAL_CAPS.values()
    for endpoint, maximum in phase_caps.items()
}
_DATA_ROOT = Path(__file__).resolve().parents[3] / "var" / "data"
PREDECESSOR_LEDGERS = (
    str(_DATA_ROOT / "quant-kr-source-contract/20260901T224118Z"),
    str(_DATA_ROOT / "quant-kr-source-contract/20260901T234525Z-correction"),
)
PREDECESSOR_TERMINALS = ("STOP:SourceContractRefusal", "RESOLVED_HYPOTHESIS")
PREDECESSOR_FIXTURE_SHA256 = (
    "4c22c6cda96dae4bdd8e426345a48e6d2cf47813488d274bf5960219bdb35f50")
# Exact StockInfo attributes already consumed by the membership gate.  This
# evidence lane deliberately excludes names, prices, quantities and shares.
TOSS_MEMBERSHIP_STOCK_INFO_FIELDS = frozenset({
    "symbol", "isin_code", "market", "security_type", "is_common_share",
    "status", "currency", "list_date", "delist_date",
    "krx_trading_suspended",
})
STOP_FAILURES = frozenset({
    "auth_rejection", "http_429", "redirect", "body_too_large",
    "unsupported_content_encoding", "protocol_failure",
})
SAFE_ROW_FIELDS = frozenset({
    "corp_code", "corp_name", "stock_code", "corp_cls", "rcept_no",
    "reprt_code", "bsns_year", "sj_div", "account_id", "account_nm",
    "fs_div", "thstrm_nm", "frmtrm_nm", "bfefrmtrm_nm",
    "se", "stock_knd", "istc_totqy", "distb_stock_co",
})
SAFE_FIELD_NAMES = SAFE_ROW_FIELDS | frozenset({
    "thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount",
    "thstrm_add_amount", "frmtrm_add_amount",
})


class SourceContractRefusal(RuntimeError):
    pass


@dataclass(frozen=True)
class HeldIssuer:
    symbol: str
    corp_code: str

    def __post_init__(self) -> None:
        if (len(self.symbol) != 6 or not self.symbol.isdigit()
                or len(self.corp_code) != 8 or not self.corp_code.isdigit()):
            raise ValueError("invalid_held_issuer_identity")


@dataclass(frozen=True)
class TossTraceRequest:
    phase: str
    operation: str
    held_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class TossTraceResult:
    request_identity: str
    status: str
    body_sha256: str
    symbols: tuple[str, ...] = ()
    stock_info: tuple[dict, ...] = ()


class PhaseLedger:
    """Single immutable phase ledger; reservations are durable before I/O."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def create(self, held_symbols: tuple[str, ...]) -> None:
        if self.path.exists():
            raise SourceContractRefusal("phase_already_exists")
        if self.path.parent.exists():
            if stat.S_IMODE(self.path.parent.stat().st_mode) != 0o700:
                raise SourceContractRefusal("output_dir_not_private")
        else:
            self.path.parent.mkdir(mode=0o700, parents=True)
        payload = {"phase": PHASE, "held_symbols": list(held_symbols),
                   "starts": [], "terminal": None}
        self._write_new(payload)

    def reserve(self, endpoint: str, identity: str) -> int:
        data = self.read()
        if data["terminal"] is not None:
            raise SourceContractRefusal("phase_terminal")
        used = sum(row["endpoint"] == endpoint for row in data["starts"])
        if endpoint not in ALL_CAPS or used >= ALL_CAPS[endpoint]:
            raise SourceContractRefusal("endpoint_ceiling_reached")
        sequence = len(data["starts"]) + 1
        data["starts"].append({"sequence": sequence, "endpoint": endpoint,
                               "request_identity": identity,
                               "outcome": "RESERVED"})
        self._replace(data)
        return sequence

    def settle(self, sequence: int, outcome: str, observation: dict) -> None:
        data = self.read()
        row = data["starts"][sequence - 1]
        if row["sequence"] != sequence or row["outcome"] != "RESERVED":
            raise SourceContractRefusal("reservation_disagreement")
        row["outcome"] = outcome
        row["observation"] = observation
        self._replace(data)

    def finish(self, terminal: str) -> None:
        data = self.read()
        data["terminal"] = terminal
        self._replace(data)

    def read(self) -> dict:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("phase") not in (PHASE, TOSS_V2_PHASE, DART_V2_PHASE):
            raise SourceContractRefusal("phase_identity_disagreement")
        return data

    def _write_new(self, data: dict) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(self.path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        self._sync_parent()

    def _replace(self, data: dict) -> None:
        temporary = self.path.with_name(self.path.name + ".next")
        if temporary.exists():
            raise SourceContractRefusal("ledger_temporary_exists")
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
        self._sync_parent()

    def _sync_parent(self) -> None:
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def request_identity(endpoint: str, corp_code: str | None = None,
                     year: str | None = None) -> str:
    if endpoint == "corpCode":
        return "GET opendart.fss.or.kr/api/corpCode.xml"
    suffix = f"corp_code={corp_code}&bsns_year={year}&reprt_code=11011"
    if endpoint == "fnlttSinglAcntAll":
        suffix += "&fs_div=CFS"
    return f"GET opendart.fss.or.kr/api/{endpoint}.json?{suffix}"


def inspect_json_body(body: bytes, identity: str, endpoint: str,
                      ) -> tuple[dict, bool, bool]:
    digest = hashlib.sha256(body).hexdigest()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ({"request_identity": identity, "body_sha256": digest,
                 "schema": "invalid_json"}, False, False)
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return ({"request_identity": identity, "body_sha256": digest,
                 "schema": "drift"}, False, False)
    status = payload["status"]
    rows = payload.get("list", [])
    if status == "000" and not isinstance(rows, list):
        return ({"request_identity": identity, "body_sha256": digest,
                 "status": status, "schema": "drift"}, False, False)
    selected = []
    field_names = set()
    resolved_major = False
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                field_names.update(k for k in row if k in SAFE_FIELD_NAMES)
                if (row.get("fs_div") == "CFS" and row.get("account_id")):
                    resolved_major = True
                if len(selected) < 20:
                    selected.append({k: row[k] for k in sorted(row)
                                     if k in SAFE_ROW_FIELDS and isinstance(row[k], str)})
    observation = {"request_identity": identity, "body_sha256": digest,
                   "status": status, "schema": "observed",
                   "field_names": sorted(field_names),
                   "selected_rows": selected}
    unresolved_major = (endpoint == "fnlttSinglAcnt" and status == "000"
                        and not resolved_major)
    resolved = status == "000" and bool(selected) and not unresolved_major
    return observation, resolved, unresolved_major


def execute_opendart(*, issuers: tuple[HeldIssuer, ...], years: tuple[str, ...],
                     output_dir: Path, api_key: str, transport,
                     clock: Callable[[], datetime],
                     collect_share_evidence: bool = True,
                     ledger: PhaseLedger | None = None,
                     initial_observations: tuple[dict, ...] = ()) -> dict:
    if not issuers or len(issuers) > 5 or len({x.symbol for x in issuers}) != len(issuers):
        raise SourceContractRefusal("held_symbols_outside_bound")
    if not years or len(years) > 2 or any(len(y) != 4 or not y.isdigit() for y in years):
        raise SourceContractRefusal("years_outside_bound")
    output_dir = output_dir.resolve()
    owns_ledger = ledger is None
    ledger = ledger or PhaseLedger(output_dir / "phase-ledger.json")
    if owns_ledger:
        ledger.create(tuple(x.symbol for x in issuers))
    else:
        data = ledger.read()
        if tuple(data["held_symbols"]) != tuple(x.symbol for x in issuers):
            raise SourceContractRefusal("held_symbols_identity_disagreement")
    observations = list(initial_observations)

    def call(endpoint: str, request, identity: str):
        sequence = ledger.reserve(endpoint, identity)
        outcome = (transport(request) if endpoint == "corpCode"
                   else transport.trace_json(request))
        if isinstance(outcome, _OpenDartTransportFailure):
            observation = {"request_identity": identity, "failure": outcome.code}
            ledger.settle(sequence, outcome.code, observation)
            if outcome.code in STOP_FAILURES:
                raise SourceContractRefusal(outcome.code)
            return observation, False, False
        if not isinstance(outcome, _OpenDartTransportSuccess):
            ledger.settle(sequence, "request_identity_disagreement", {})
            raise SourceContractRefusal("request_identity_disagreement")
        observation, resolved, unresolved = inspect_json_body(
            outcome.body, identity, endpoint)
        rows = observation.get("selected_rows", [])
        if any(row.get("corp_code") not in (None, request.corp_code) for row in rows):
            ledger.settle(sequence, "request_identity_disagreement", observation)
            raise SourceContractRefusal("request_identity_disagreement")
        if (endpoint == "fnlttSinglAcntAll" and rows
                and any("fs_div" in row and row.get("fs_div") != "CFS"
                        for row in rows)):
            ledger.settle(sequence, "schema_drift", observation)
            raise SourceContractRefusal("schema_drift")
        code = "success" if observation.get("schema") == "observed" else "schema_drift"
        ledger.settle(sequence, code, observation)
        status = observation.get("status")
        if status not in ("000", "013") or code == "schema_drift":
            raise SourceContractRefusal(observation.get("status", code))
        return observation, resolved, unresolved

    try:
        identity = request_identity("corpCode")
        seq = ledger.reserve("corpCode", identity)
        outcome = transport(build_corp_code_request(api_key=api_key))
        if isinstance(outcome, _OpenDartTransportFailure):
            obs = {"request_identity": identity, "failure": outcome.code}
            ledger.settle(seq, outcome.code, obs)
            raise SourceContractRefusal(outcome.code)
        observed_at = clock()
        mappings = []
        for issuer in issuers:
            mapping = parse_corp_code_zip(
                outcome.body, stock_code=issuer.symbol, observed_at=observed_at)
            if isinstance(mapping, OpenDartCorpMapping):
                if mapping.corp_code != issuer.corp_code:
                    ledger.settle(seq, "request_identity_disagreement", {})
                    raise SourceContractRefusal("request_identity_disagreement")
                mappings.append({"stock_code": mapping.stock_code,
                                 "corp_code": mapping.corp_code,
                                 "corp_name": mapping.corp_name})
        obs = {"request_identity": identity,
               "body_sha256": hashlib.sha256(outcome.body).hexdigest(),
               "schema": "bounded_zip", "selected_rows": mappings}
        ledger.settle(seq, "success", obs)
        observations.append(obs)

        for issuer in issuers:
            fallback_used = False
            for year in years:
                identity = request_identity("fnlttSinglAcnt", issuer.corp_code, year)
                req = OpenDartTraceJsonRequest("fnlttSinglAcnt", issuer.corp_code,
                                               year, api_key=api_key)
                obs, resolved, unresolved = call("fnlttSinglAcnt", req, identity)
                observations.append(obs)
                if unresolved and not fallback_used:
                    fallback_used = True
                    fallback_identity = request_identity(
                        "fnlttSinglAcntAll", issuer.corp_code, year)
                    fallback = OpenDartTraceJsonRequest(
                        "fnlttSinglAcntAll", issuer.corp_code, year,
                        fs_div="CFS", api_key=api_key)
                    fallback_obs, resolved, _ = call(
                        "fnlttSinglAcntAll", fallback, fallback_identity)
                    observations.append(fallback_obs)
                if resolved:
                    ledger.finish("RESOLVED_HYPOTHESIS")
                    return _write_fixture(output_dir, issuers, observations, clock())
            if collect_share_evidence:
                year = years[0]
                identity = request_identity("stockTotqySttus", issuer.corp_code, year)
                req = OpenDartTraceJsonRequest("stockTotqySttus", issuer.corp_code,
                                               year, api_key=api_key)
                obs, resolved, _ = call("stockTotqySttus", req, identity)
                obs["admissibility"] = "NON_DILUTED_EVIDENCE_ONLY"
                observations.append(obs)
                if resolved:
                    ledger.finish("RESOLVED_HYPOTHESIS")
                    return _write_fixture(output_dir, issuers, observations, clock())
        ledger.finish("EXHAUSTED_WITHIN_CAPS")
        return _write_fixture(output_dir, issuers, observations, clock())
    except Exception as exc:
        if ledger.path.exists() and ledger.read()["terminal"] is None:
            ledger.finish(f"STOP:{type(exc).__name__}")
        raise


def execute_source_contract(*, phase: str, issuers: tuple[HeldIssuer, ...],
                            years: tuple[str, ...], output_dir: Path,
                            toss_token: Callable[[TossTraceRequest], TossTraceResult],
                            toss_stocks: Callable[[TossTraceRequest], TossTraceResult],
                            opendart_api_key: str, opendart_transport,
                            clock: Callable[[], datetime],
                            collect_share_evidence: bool = True) -> dict:
    """Execute both approved lanes once through explicitly injected I/O seams."""
    if phase != PHASE:
        raise SourceContractRefusal("exact_phase_required")
    symbols = tuple(issuer.symbol for issuer in issuers)
    if not symbols or len(symbols) > 5 or len(set(symbols)) != len(symbols):
        raise SourceContractRefusal("held_symbols_outside_bound")
    ledger = PhaseLedger(output_dir.resolve() / "phase-ledger.json")
    ledger.create(symbols)
    observations: list[dict] = []

    def toss_call(operation: str, injected, expected_symbols: tuple[str, ...]) -> None:
        identity = operation
        sequence = ledger.reserve(operation, identity)
        request = TossTraceRequest(PHASE, operation, expected_symbols)
        try:
            result = injected(request)
        except Exception:
            ledger.settle(sequence, "injected_failure",
                          {"request_identity": identity, "failure": "injected_failure"})
            raise
        valid_digest = (isinstance(result, TossTraceResult)
                        and len(result.body_sha256) == 64
                        and all(char in "0123456789abcdef"
                                for char in result.body_sha256))
        if (not isinstance(result, TossTraceResult)
                or result.request_identity != identity or result.status != "success"
                or result.symbols != expected_symbols or not valid_digest):
            ledger.settle(sequence, "request_identity_disagreement",
                          {"request_identity": identity,
                           "failure": "sanitized_read_back_disagreement"})
            raise SourceContractRefusal("request_identity_disagreement")
        observation = {"request_identity": identity, "status": "success",
                       "body_sha256": result.body_sha256,
                       "symbols": list(expected_symbols),
                       "schema": "toss_read_back"}
        ledger.settle(sequence, "success", observation)
        observations.append(observation)

    try:
        toss_call("TOKEN", toss_token, ())
        toss_call("STOCKS", toss_stocks, symbols)
        return execute_opendart(
            issuers=issuers, years=years, output_dir=output_dir,
            api_key=opendart_api_key, transport=opendart_transport, clock=clock,
            collect_share_evidence=collect_share_evidence, ledger=ledger,
            initial_observations=tuple(observations))
    except Exception as exc:
        if ledger.path.exists() and ledger.read()["terminal"] is None:
            ledger.finish(f"STOP:{type(exc).__name__}")
        raise


def _write_fixture(output_dir: Path, issuers: tuple[HeldIssuer, ...],
                   observations: list[dict], observed_at: datetime) -> dict:
    payload = {"phase": PHASE, "held_symbols": [x.symbol for x in issuers],
               "comparison_acquisition": "FORBIDDEN", "retry": 0,
               "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
               "observations": observations}
    path = output_dir / "sanitized-fixture.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SourceContractRefusal("fixture_permissions_not_private")
    return payload


def _canonical_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    canonical = tuple(sorted(set(symbols)))
    if (not canonical or len(canonical) > 5
            or any(len(symbol) != 6 or not symbol.isdigit()
                   for symbol in canonical)):
        raise SourceContractRefusal("held_symbols_outside_bound")
    return canonical


def validate_predecessor_readback(payloads: tuple[dict, dict]) -> tuple[str, str]:
    """Validate immutable V1 ledger bytes against the SDS accounting pin."""
    if len(payloads) != 2:
        raise SourceContractRefusal("predecessor_ledger_disagreement")
    actual = {endpoint: 0 for endpoint in PRIOR_ACTUAL_STARTS}
    digests = []
    for payload, terminal in zip(payloads, PREDECESSOR_TERMINALS):
        if not isinstance(payload, dict) or payload.get("terminal") != terminal:
            raise SourceContractRefusal("predecessor_ledger_disagreement")
        starts = payload.get("starts")
        if not isinstance(starts, list):
            raise SourceContractRefusal("predecessor_ledger_disagreement")
        for row in starts:
            endpoint = row.get("endpoint") if isinstance(row, dict) else None
            if endpoint in actual:
                actual[endpoint] += 1
        encoded = json.dumps(payload, sort_keys=True,
                             separators=(",", ":")).encode()
        digests.append(hashlib.sha256(encoded).hexdigest())
    if actual != PRIOR_ACTUAL_STARTS:
        raise SourceContractRefusal("predecessor_ledger_disagreement")
    return tuple(digests)


def pinned_predecessor_readback() -> tuple[dict, dict]:
    """Offline-only exact accounting fixture; live controller replaces this."""
    endpoints = tuple(PRIOR_ACTUAL_STARTS)
    phases = []
    for phase_index, terminal in enumerate(PREDECESSOR_TERMINALS):
        starts = [{"endpoint": endpoint, "outcome": "settled"}
                  for endpoint in endpoints
                  if PRIOR_ACTUAL_STARTS[endpoint] > phase_index]
        phases.append({"phase": PHASE, "starts": starts, "terminal": terminal})
    return tuple(phases)


def _create_v2_ledger(output_dir: Path, phase: str,
                      symbols: tuple[str, ...], predecessor_payloads) -> PhaseLedger:
    predecessor_digests = validate_predecessor_readback(predecessor_payloads)
    ledger = PhaseLedger(output_dir.resolve() / "phase-ledger.json")
    ledger.create(symbols)
    data = ledger.read()
    data["phase"] = phase
    data["incremental_caps"] = V2_INCREMENTAL_CAPS[phase]
    data["prior_actual_starts"] = PRIOR_ACTUAL_STARTS
    data["cumulative_caps"] = V2_CUMULATIVE_CAPS
    data["predecessor_fixture_sha256"] = PREDECESSOR_FIXTURE_SHA256
    data["predecessor_ledgers"] = [
        {"path": path, "terminal": terminal, "ledger_sha256": digest}
        for path, terminal, digest in zip(
            PREDECESSOR_LEDGERS, PREDECESSOR_TERMINALS, predecessor_digests)
    ]
    ledger._replace(data)
    return ledger


def _v2_read(ledger: PhaseLedger, phase: str) -> dict:
    if (stat.S_IMODE(ledger.path.parent.stat().st_mode) != 0o700
            or stat.S_IMODE(ledger.path.stat().st_mode) != 0o600
            or ledger.path.with_name(ledger.path.name + ".next").exists()):
        raise SourceContractRefusal("ledger_permissions_disagreement")
    data = json.loads(ledger.path.read_text(encoding="utf-8"))
    if data.get("phase") != phase:
        raise SourceContractRefusal("phase_identity_disagreement")
    return data


def _v2_reserve(ledger: PhaseLedger, phase: str, endpoint: str,
                identity: str, predecessor_readback) -> int:
    validate_predecessor_readback(predecessor_readback())
    data = _v2_read(ledger, phase)
    if data["terminal"] is not None:
        raise SourceContractRefusal("phase_terminal")
    caps = V2_INCREMENTAL_CAPS[phase]
    used = sum(row["endpoint"] == endpoint for row in data["starts"])
    if endpoint not in caps or used >= caps[endpoint]:
        raise SourceContractRefusal("endpoint_ceiling_reached")
    cumulative = PRIOR_ACTUAL_STARTS[endpoint] + used + 1
    if cumulative > V2_CUMULATIVE_CAPS[endpoint]:
        raise SourceContractRefusal("endpoint_ceiling_reached")
    sequence = len(data["starts"]) + 1
    data["starts"].append({"sequence": sequence, "endpoint": endpoint,
                           "request_identity": identity,
                           "outcome": "RESERVED"})
    ledger._replace(data)
    return sequence


def _v2_settle(ledger: PhaseLedger, phase: str, sequence: int,
               outcome: str, observation: dict) -> None:
    data = _v2_read(ledger, phase)
    row = data["starts"][sequence - 1]
    if row["sequence"] != sequence or row["outcome"] != "RESERVED":
        raise SourceContractRefusal("reservation_disagreement")
    row["outcome"] = outcome
    row["observation"] = observation
    ledger._replace(data)


def _v2_finish(ledger: PhaseLedger, phase: str, terminal: str) -> None:
    data = _v2_read(ledger, phase)
    data["terminal"] = terminal
    ledger._replace(data)


def _write_v2_fixture(output_dir: Path, phase: str,
                      symbols: tuple[str, ...], observations: list[dict],
                      observed_at: datetime, terminal: str) -> dict:
    payload = {
        "phase": phase, "held_symbols": list(symbols),
        "comparison_acquisition": "FORBIDDEN", "send": 0, "retry": 0,
        "terminal": terminal,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "observations": observations,
    }
    path = output_dir / "sanitized-fixture.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SourceContractRefusal("fixture_permissions_not_private")
    return payload


def execute_toss_v2(*, phase: str, held_symbols: tuple[str, ...],
                    output_dir: Path,
                    toss_token: Callable[[TossTraceRequest], TossTraceResult],
                    toss_stocks: Callable[[TossTraceRequest], TossTraceResult],
                    clock: Callable[[], datetime],
                    predecessor_readback: Callable[[], tuple[dict, dict]] =
                    pinned_predecessor_readback) -> dict:
    """Run the symbol-only membership evidence phase; no vocabulary mapping."""
    if phase != TOSS_V2_PHASE:
        raise SourceContractRefusal("exact_phase_required")
    symbols = _canonical_symbols(held_symbols)
    ledger = _create_v2_ledger(output_dir, phase, symbols,
                               predecessor_readback())
    observations: list[dict] = []
    try:
        for operation, injected, expected in (
                ("TOKEN", toss_token, ()), ("STOCKS", toss_stocks, symbols)):
            sequence = _v2_reserve(
                ledger, phase, operation, operation, predecessor_readback)
            request = TossTraceRequest(phase, operation, expected)
            result = injected(request)
            valid_digest = (isinstance(result, TossTraceResult)
                            and len(result.body_sha256) == 64
                            and all(c in "0123456789abcdef"
                                    for c in result.body_sha256))
            if (not isinstance(result, TossTraceResult)
                    or result.request_identity != operation
                    or result.status != "success" or result.symbols != expected
                    or not valid_digest):
                _v2_settle(ledger, phase, sequence,
                           "request_identity_disagreement", {})
                raise SourceContractRefusal("request_identity_disagreement")
            observation = {"request_identity": operation, "status": "success",
                           "body_sha256": result.body_sha256,
                           "symbols": list(expected)}
            if operation == "STOCKS":
                if tuple(row.get("symbol") for row in result.stock_info) != symbols:
                    _v2_settle(ledger, phase, sequence,
                               "exact_echo_disagreement", observation)
                    raise SourceContractRefusal("exact_echo_disagreement")
                closed_rows = []
                for row in result.stock_info:
                    if not isinstance(row, dict):
                        _v2_settle(ledger, phase, sequence,
                                   "schema_drift", observation)
                        raise SourceContractRefusal("schema_drift")
                    if set(row) != TOSS_MEMBERSHIP_STOCK_INFO_FIELDS:
                        _v2_settle(ledger, phase, sequence,
                                   "schema_drift", observation)
                        raise SourceContractRefusal("schema_drift")
                    closed = {name: row[name]
                              for name in sorted(TOSS_MEMBERSHIP_STOCK_INFO_FIELDS)}
                    closed_rows.append(closed)
                observation["stock_info"] = closed_rows
                observation["schema"] = "raw_stock_info_closed_set"
            _v2_settle(ledger, phase, sequence, "success", observation)
            observations.append(observation)
        terminal = "MEMBERSHIP_CONTRACT_COMPLETE"
        _v2_finish(ledger, phase, terminal)
        return _write_v2_fixture(output_dir, phase, symbols, observations,
                                 clock(), terminal)
    except Exception as exc:
        if ledger.path.exists() and _v2_read(ledger, phase)["terminal"] is None:
            _v2_finish(ledger, phase, f"STOP:{type(exc).__name__}")
        raise


def execute_dart_v2(*, phase: str, issuers: tuple[HeldIssuer, ...],
                    years: tuple[str, ...], output_dir: Path, api_key: str,
                    transport, clock: Callable[[], datetime],
                    corp_code_pin: dict[str, str | None],
                    predecessor_readback: Callable[[], tuple[dict, dict]] =
                    pinned_predecessor_readback) -> dict:
    """Run the historical EP-1 two-issuer verification campaign.

    This bounded executor must never be reused as a production target resolver.
    Future target selection belongs to ``facts.opendart_targeted``.
    """
    if phase != DART_V2_PHASE:
        raise SourceContractRefusal("exact_phase_required")
    ordered = tuple(sorted(issuers, key=lambda item: item.symbol))
    if (tuple(item.symbol for item in ordered) != ("000660", "005380")
            or tuple(sorted(set(years))) != ("2024", "2025")):
        raise SourceContractRefusal("dart_matrix_identity_disagreement")
    expected_symbols = {item.symbol for item in ordered}
    if set(corp_code_pin) != expected_symbols:
        raise SourceContractRefusal("predecessor_fixture_disagreement")
    for issuer in ordered:
        pinned = corp_code_pin[issuer.symbol]
        if pinned is not None and pinned != issuer.corp_code:
            raise SourceContractRefusal("corp_code_mismatch")
    corp_codes_validated = all(corp_code_pin[item.symbol] is not None
                               for item in ordered)
    years = ("2024", "2025")
    ledger = _create_v2_ledger(output_dir, phase,
                               tuple(item.symbol for item in ordered),
                               predecessor_readback())
    observations: list[dict] = []

    def call(endpoint: str, issuer: HeldIssuer, year: str):
        identity = request_identity(endpoint, issuer.corp_code, year)
        sequence = _v2_reserve(
            ledger, phase, endpoint, identity, predecessor_readback)
        request = OpenDartTraceJsonRequest(
            endpoint, issuer.corp_code, year,
            fs_div="CFS" if endpoint == "fnlttSinglAcntAll" else None,
            api_key=api_key)
        outcome = transport.trace_json(request)
        if isinstance(outcome, _OpenDartTransportFailure):
            observation = {"request_identity": identity, "failure": outcome.code}
            _v2_settle(ledger, phase, sequence, outcome.code, observation)
            raise SourceContractRefusal(outcome.code)
        if not isinstance(outcome, _OpenDartTransportSuccess):
            raise SourceContractRefusal("request_identity_disagreement")
        observation, resolved, unresolved = inspect_json_body(
            outcome.body, identity, endpoint)
        rows = observation.get("selected_rows", [])
        if any(row.get("corp_code") not in (None, issuer.corp_code)
               for row in rows):
            _v2_settle(ledger, phase, sequence,
                       "request_identity_disagreement", observation)
            raise SourceContractRefusal("request_identity_disagreement")
        if (endpoint == "fnlttSinglAcntAll" and rows
                and any("fs_div" in row and row.get("fs_div") != "CFS"
                        for row in rows)):
            _v2_settle(ledger, phase, sequence, "schema_drift", observation)
            raise SourceContractRefusal("schema_drift")
        status = observation.get("status")
        code = "success" if observation.get("schema") == "observed" else "schema_drift"
        _v2_settle(ledger, phase, sequence, code, observation)
        if status not in ("000", "013") or code == "schema_drift":
            raise SourceContractRefusal(status or code)
        return observation, resolved, unresolved

    try:
        if not corp_codes_validated:
            identity = request_identity("corpCode")
            sequence = _v2_reserve(
                ledger, phase, "corpCode", identity, predecessor_readback)
            outcome = transport(build_corp_code_request(api_key=api_key))
            if not isinstance(outcome, _OpenDartTransportSuccess):
                code = getattr(outcome, "code", "protocol_failure")
                _v2_settle(ledger, phase, sequence, code,
                           {"request_identity": identity, "failure": code})
                raise SourceContractRefusal(code)
            observed_at = clock()
            mappings = []
            for issuer in ordered:
                mapping = parse_corp_code_zip(
                    outcome.body, stock_code=issuer.symbol, observed_at=observed_at)
                if not isinstance(mapping, OpenDartCorpMapping) or mapping.corp_code != issuer.corp_code:
                    _v2_settle(ledger, phase, sequence, "corp_code_mismatch", {})
                    raise SourceContractRefusal("corp_code_mismatch")
                mappings.append({"stock_code": issuer.symbol,
                                 "corp_code": issuer.corp_code})
            observation = {"request_identity": identity,
                           "body_sha256": hashlib.sha256(outcome.body).hexdigest(),
                           "schema": "bounded_zip", "selected_rows": mappings}
            _v2_settle(ledger, phase, sequence, "success", observation)
            observations.append(observation)
        for issuer in ordered:
            for year in years:
                observation, _resolved, unresolved = call(
                    "fnlttSinglAcnt", issuer, year)
                observations.append(observation)
                if unresolved:
                    observation, _resolved, _ = call(
                        "fnlttSinglAcntAll", issuer, year)
                    observations.append(observation)
            observation, _resolved, _ = call(
                "stockTotqySttus", issuer, max(years))
            observation["admissibility"] = "NON_DILUTED_EVIDENCE_ONLY"
            observations.append(observation)
        terminal = "MATRIX_COMPLETE"
        _v2_finish(ledger, phase, terminal)
        return _write_v2_fixture(output_dir, phase,
                                 tuple(item.symbol for item in ordered),
                                 observations, clock(), terminal)
    except Exception as exc:
        if ledger.path.exists() and _v2_read(ledger, phase)["terminal"] is None:
            _v2_finish(ledger, phase, f"STOP:{type(exc).__name__}")
        raise


def build_preflight(symbols: tuple[str, ...]) -> dict:
    return {"phase": PHASE, "mode": "NO_LIVE", "held_symbols": list(symbols),
            "toss": {"execution_owner": "controller", "token_max": 1,
                     "stocks_max": 1, "operation": "STOCKS"},
            "opendart_caps": CAPS, "comparison_operations": [], "retry": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase")
    parser.add_argument("--held-symbol", action="append", default=[])
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    symbols = tuple(args.held_symbol)
    if args.phase is None:
        print(json.dumps(build_preflight(symbols), sort_keys=True))
        return 0
    if args.phase != PHASE:
        parser.error("exact_phase_required")
    if args.output_dir is None:
        parser.error("explicit_private_output_dir_required")
    parser.error("live_dependencies_must_be_injected_by_controller")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
