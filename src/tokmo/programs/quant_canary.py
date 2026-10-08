"""Bounded, durable, provider-neutral Quant canary safety boundary."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from collections import Counter
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable

CAMPAIGN_ID = "quant-bounded-canary-20260825-a"
POLICY_REVISION = "quant-bounded-canary-policy-v1"
TOTAL_CAP = 9
PREVIEW_FAMILY_CAP = 106
PREVIEW_SESSION_CAP = 10
PREVIEW_FAMILY_POLICY = "quant-g2-preview-family-rev-20260831"
SUCCESSOR_ATTEMPT_CAP = 5
SUCCESSOR_START_CAP = 45
SUCCESSOR_REPAIR_COMMIT = "e599953c72d79de94b6339fa228b960545b05188"
SUCCESSOR_SESSIONS = {"KR": date(2026, 8, 25), "US": date(2026, 8, 25)}
SUCCESSOR_BASELINE_STARTS = 14
SUBCAPS = {"TOKEN": 1, "RANKINGS": 2, "STOCKS": 2, "STOCK_CANDLES": 4}
MARKET_CAPS = {"RANKINGS": 1, "STOCKS": 1, "STOCK_CANDLES": 2}
_SETTLEMENTS = {"SUCCESS", "TRANSPORT_FAILURE", "HTTP_FAILURE", "PARSER_FAILURE", "CONTRACT_FAILURE"}
FAILURE_REASONS = {"http_429", "http_5xx", "bad_request", "forbidden", "auth_rejection",
                   "timeout", "connection", "redirect", "protocol", "parser", "contract"}
_REASON_SETTLEMENTS = {
    **{reason: "HTTP_FAILURE" for reason in
       ("http_429", "http_5xx", "bad_request", "forbidden", "auth_rejection")},
    **{reason: "TRANSPORT_FAILURE" for reason in
       ("timeout", "connection", "redirect", "protocol")},
    "parser": "PARSER_FAILURE", "contract": "CONTRACT_FAILURE",
}


class CampaignFailure(RuntimeError):
    """Closed, sanitized failure-layer signal for the campaign boundary."""
    def __init__(self, reason):
        if reason not in FAILURE_REASONS:
            raise ValueError("invalid campaign failure reason")
        self.reason = reason
        self.settlement = _REASON_SETTLEMENTS[reason]
        super().__init__(reason)


def _canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _manifest_binding(manifest):
    keys = ["campaign_id", "policy_revision", "sessions", "profile_revision",
            "report_revision", "identities", "terminal_bindings"]
    if manifest.get("terminal") is True:
        keys.append("products")
    return sha256(_canonical({key: manifest[key] for key in keys})).hexdigest()


def _legacy_manifest_binding(manifest):
    keys = ("campaign_id", "policy_revision", "sessions", "profile_revision",
            "report_revision", "identities", "terminal_bindings")
    return sha256(_canonical({key: manifest[key] for key in keys})).hexdigest()


def _secure_dir(path: Path, *, create=False) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private path boundary refused")


def _secure_leaf(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("private path boundary refused")


def _write_private(path: Path, value) -> None:
    data = _canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    _secure_leaf(path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _read_object(path: Path):
    _secure_leaf(path)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = os.read(fd, 1_000_001)
    finally:
        os.close(fd)
    if len(raw) > 1_000_000:
        raise ValueError("campaign state corrupt")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("campaign state corrupt") from exc
    if not isinstance(value, dict):
        raise ValueError("campaign state corrupt")
    return value


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _production_campaign_path_allowed(root: Path, campaign_id: str, home: Path) -> bool:
    family = home / ".local/share/tokmo-v3/ops/quant-g2-preview"
    candidate = root.expanduser()
    expected = family / "sessions" / campaign_id
    suffix = campaign_id.removeprefix("quant-g2-session-")
    if (candidate != expected or candidate.name != campaign_id
            or not campaign_id.startswith("quant-g2-session-")
            or len(suffix) != 24 or any(character not in "0123456789abcdef" for character in suffix)):
        return False
    for ancestor in (candidate, candidate.parent, family):
        if ancestor.exists() and stat.S_ISLNK(ancestor.lstat().st_mode):
            return False
    return candidate.resolve(strict=False) == expected.resolve(strict=False)


class MeteredCampaign:
    """Single-lock reserve-before-delegate campaign ledger."""
    def __init__(self, root: Path, *, sessions: dict[str, date], identities: dict[str, str],
                 campaign_id: str = CAMPAIGN_ID,
                 profile_revision: str = "quant-profile-v1", report_revision: str = "quant-report-v1",
                 monotonic: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep,
                 wallclock: Callable[[], datetime] = lambda: datetime.fromtimestamp(time.time(), timezone.utc),
                 _test_policy_roots=None, _family=None):
        self.root = Path(root)
        home = Path.home()
        forbidden = (home / ".local/share/tokmo-v3/stores", home / ".local/share/tokmo-v3/quant",
                     home / "tokmo/tokmo-v3/projects/tokmo-v3")
        if _test_policy_roots is None:
            if not _production_campaign_path_allowed(self.root, campaign_id, home):
                raise ValueError("fixed campaign path boundary refused")
            policy_roots = forbidden
        else:
            policy_roots = tuple(Path(item).resolve(strict=False) for item in _test_policy_roots)
        resolved = self.root.expanduser().resolve(strict=False)
        if any(_overlaps(resolved, item) for item in policy_roots):
            raise ValueError("private path boundary refused")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        _secure_dir(self.root)
        self.ledger_path = self.root / "campaign-ledger.json"
        self.manifest_path = self.root / "campaign-manifest.json"
        self.report_root = self.root / "report-store"
        existing_material = tuple(self.report_root.rglob("*")) if self.report_root.exists() else ()
        if not self.manifest_path.exists() and (self.ledger_path.exists() or any(p.is_file() or p.is_symlink() for p in existing_material)):
            raise ValueError("terminal or ledger material without manifest")
        self.report_root.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.report_root, 0o700)
        _secure_dir(self.report_root)
        lock_flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        self._lock_fd = os.open(self.root / ".lock", lock_flags, 0o600)
        os.fchmod(self._lock_fd, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._lock_fd)
            raise ValueError("campaign lock unavailable") from exc
        self.monotonic, self.sleeper, self.wallclock = monotonic, sleeper, wallclock
        self.campaign_id = campaign_id
        self._family = _family
        self._admitted: dict[str, tuple[str, ...]] = {}
        self._candidates: dict[str, tuple[str, ...]] = {}
        manifest = {"campaign_id": campaign_id, "policy_revision": POLICY_REVISION,
                    "report_root": str(self.report_root.resolve()),
                    "sessions": {key: value.isoformat() for key, value in sorted(sessions.items())},
                    "profile_revision": profile_revision, "report_revision": report_revision,
                    "identities": dict(sorted(identities.items())), "terminal": False, "products": {},
                    "terminal_bindings": {}, "campaign_binding": None}
        if self.manifest_path.exists():
            existing = _read_object(self.manifest_path)
            terminal_binding = existing.get("campaign_binding")
            expected_binding = _manifest_binding(existing)
            legacy_binding = _legacy_manifest_binding(existing)
            legacy_terminal = (existing.get("terminal") is True
                               and terminal_binding == legacy_binding
                               and terminal_binding != expected_binding)
            if (existing.get("terminal") is True
                    and terminal_binding != expected_binding and not legacy_terminal):
                self.close(); raise ValueError("manifest campaign binding mismatch")
            comparable = dict(existing); comparable["terminal"] = False; comparable["products"] = {}; comparable["terminal_bindings"] = {}; comparable["campaign_binding"] = None
            expected = dict(manifest)
            if comparable != expected:
                self.close(); raise ValueError("manifest mismatch")
            self.manifest = existing
            self._legacy_terminal_binding = legacy_terminal
        else:
            self.manifest = manifest; self._legacy_terminal_binding = False
            _write_private(self.manifest_path, manifest)
        if self.ledger_path.exists():
            self.ledger = _read_object(self.ledger_path)
        else:
            self.ledger = {"campaign_id": campaign_id, "reservations": [], "bindings": {}}; _write_private(self.ledger_path, self.ledger)
        self._validate_ledger()
        for market, binding in self.ledger["bindings"].items():
            self._candidates[market] = tuple(binding["candidates"])
            self._admitted[market] = tuple(binding["admitted"])
        if self.manifest["terminal"] is True:
            try:
                self._verify_terminal_material()
                if self._legacy_terminal_binding:
                    self.manifest["campaign_binding"] = _manifest_binding(self.manifest)
                    _write_private(self.manifest_path, self.manifest)
            except Exception:
                self.close()
                raise
        else:
            material = any(p.is_file() or p.is_symlink() for p in self.report_root.rglob("*"))
            self.partial_products = self._validate_partial_material() if material else None

    @property
    def replay(self):
        return self.manifest["terminal"] is True

    def _validate_ledger(self):
        if set(self.ledger) != {"campaign_id", "reservations", "bindings"} or self.ledger["campaign_id"] != self.campaign_id or not isinstance(self.ledger["reservations"], list) or not isinstance(self.ledger["bindings"], dict):
            raise ValueError("ledger corrupt")
        for market, binding in self.ledger["bindings"].items():
            if (market not in ("KR", "US") or set(binding) != {"candidates", "candidate_digest", "admitted", "admitted_digest"}
                    or not isinstance(binding["candidates"], list) or not isinstance(binding["admitted"], list)
                    or len(binding["candidates"]) not in (1, 2) or len(binding["admitted"]) > 2
                    or not set(binding["admitted"]).issubset(binding["candidates"])
                    or binding["candidate_digest"] != sha256("\0".join(binding["candidates"]).encode()).hexdigest()
                    or binding["admitted_digest"] != sha256("\0".join(binding["admitted"]).encode()).hexdigest()):
                raise ValueError("ledger binding corrupt")
        counts = Counter(); market_counts = Counter(); candle_seen = Counter()
        for index, row in enumerate(self.ledger["reservations"], 1):
            expected_keys = {"seq", "operation", "market", "started_at", "settlement"} | ({"target_sha256"} if row.get("operation") == "STOCK_CANDLES" else set())
            if set(row) not in (expected_keys, expected_keys | {"failure_reason"}) or row["seq"] != index:
                raise ValueError("ledger corrupt")
            operation, market = row["operation"], row["market"]
            if operation not in SUBCAPS or market not in (None, "KR", "US") or (operation == "TOKEN") != (market is None):
                raise ValueError("ledger corrupt")
            try:
                stamp = datetime.fromisoformat(row["started_at"])
            except (TypeError, ValueError) as exc:
                raise ValueError("ledger timestamp corrupt") from exc
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("ledger timestamp corrupt")
            if row["settlement"] not in _SETTLEMENTS | {None}:
                raise ValueError("ledger corrupt")
            reason = row.get("failure_reason")
            if (reason is not None and (reason not in FAILURE_REASONS
                    or row["settlement"] != _REASON_SETTLEMENTS[reason])):
                raise ValueError("ledger corrupt")
            if "failure_reason" in row and reason is None:
                raise ValueError("ledger corrupt")
            if operation == "STOCK_CANDLES":
                binding = self.ledger["bindings"].get(market)
                admitted = () if binding is None else binding["admitted"]
                position = candle_seen[market]
                if (position >= len(admitted)
                        or row["target_sha256"] != sha256(admitted[position].encode()).hexdigest()):
                    raise ValueError("ledger candle target corrupt")
                candle_seen[market] += 1
            counts[operation] += 1
            if market is not None: market_counts[(operation, market)] += 1
        if len(self.ledger["reservations"]) > TOTAL_CAP or any(counts[k] > SUBCAPS[k] for k in SUBCAPS):
            raise ValueError("campaign cap corrupt")
        if any(market_counts[(op, market)] > MARKET_CAPS[op] for op in MARKET_CAPS for market in ("KR", "US")):
            raise ValueError("campaign cap corrupt")
        if any(row["settlement"] != "SUCCESS" for row in self.ledger["reservations"]):
            raise ValueError("unresolved or stopped campaign")

    def _verify_terminal_material(self):
        expected_files = set()
        bound_markets = set(self.manifest["terminal_bindings"])
        if bound_markets not in ({"KR", "US"}, {"KR"}):
            raise ValueError("terminal material binding mismatch")
        product_fields = {"outcome", "cards", "report_readback", "receipt_readback", "replay_zero"}
        if (set(self.manifest.get("products", {})) != {"KR", "US"}
                or any(not isinstance(self.manifest["products"][market], dict)
                       or set(self.manifest["products"][market]) != product_fields
                       for market in ("KR", "US"))
                or (bound_markets == {"KR"} and self.manifest["products"]["US"] != {
                    "outcome": "PENDING", "cards": 0, "report_readback": False,
                    "receipt_readback": False, "replay_zero": True})):
            raise ValueError("terminal product binding mismatch")
        for market in bound_markets:
            identity = self.manifest["identities"][market]
            base = self.report_root / "market_discovery" / market.lower() / identity
            report, receipt = Path(f"{base}.report.json"), Path(f"{base}.receipt.json")
            for leaf in (report, receipt):
                _secure_leaf(leaf)
                expected_files.add(leaf.resolve())
            report_raw = report.read_bytes()
            receipt_raw = receipt.read_bytes()
            report_data = json.loads(report_raw); receipt_data = json.loads(receipt_raw)
            expected_identity = {"product_kind": "MARKET_DISCOVERY", "market": market,
                "completed_session_date": self.manifest["sessions"][market], "completed_iso_week": None,
                "profile_revision": self.manifest["profile_revision"], "peer_registry_revision": None,
                "report_revision": self.manifest["report_revision"], "identity_sha256": identity}
            report_identity = report_data.get("identity")
            receipt_identity = receipt_data.get("identity")
            binding = self.manifest.get("terminal_bindings", {}).get(market)
            cards = report_data.get("cards")
            product = self.manifest.get("products", {}).get(market)
            if (receipt_data.get("report_sha256") != sha256(report_raw).hexdigest()
                    or not isinstance(report_identity, dict) or set(report_identity) != set(expected_identity)
                    or not isinstance(receipt_identity, dict) or set(receipt_identity) != set(expected_identity)
                    or report_identity != expected_identity or receipt_identity != expected_identity
                    or report_identity != receipt_identity
                    or not isinstance(cards, list) or len(cards) > 2
                    or not isinstance(product, dict)
                    or product.get("outcome") != receipt_data.get("outcome")
                    or product.get("cards") != len(cards)
                    or product.get("report_readback") is not True
                    or product.get("receipt_readback") is not True
                    or type(product.get("replay_zero")) is not bool
                    or binding != {"identity": identity, "report_sha256": sha256(report_raw).hexdigest(),
                                   "receipt_sha256": sha256(receipt_raw).hexdigest()}):
                raise ValueError("receipt-bound report identity or digest mismatch")
        actual = {p.resolve() for p in self.report_root.rglob("*") if p.is_file() or p.is_symlink()}
        if actual != expected_files:
            raise ValueError("foreign or one-sided report material")

    def _validate_partial_material(self):
        """Admit only observed seven-start KR-settled/US-provider-zero crash shapes."""
        rows = [(row["operation"], row["market"]) for row in self.ledger["reservations"]]
        expected = [("TOKEN", None), ("RANKINGS", "KR"), ("STOCKS", "KR"),
                    ("STOCK_CANDLES", "KR"), ("STOCK_CANDLES", "KR"),
                    ("RANKINGS", "US"), ("STOCKS", "US")]
        if rows != expected or set(self.ledger["bindings"]) != {"KR", "US"}:
            self.close(); raise ValueError("foreign or one-sided report material")
        identity = self.manifest["identities"]["KR"]
        base = self.report_root / "market_discovery" / "kr" / identity
        report, receipt = Path(f"{base}.report.json"), Path(f"{base}.receipt.json")
        try:
            for leaf in (report, receipt): _secure_leaf(leaf)
            report_raw, receipt_raw = report.read_bytes(), receipt.read_bytes()
            report_data, receipt_data = json.loads(report_raw), json.loads(receipt_raw)
            expected_identity = {"product_kind": "MARKET_DISCOVERY", "market": "KR",
                "completed_session_date": self.manifest["sessions"]["KR"], "completed_iso_week": None,
                "profile_revision": self.manifest["profile_revision"], "peer_registry_revision": None,
                "report_revision": self.manifest["report_revision"], "identity_sha256": identity}
            actual = {path.resolve() for path in self.report_root.rglob("*") if path.is_file() or path.is_symlink()}
            cards = report_data.get("cards")
            outcome = receipt_data.get("outcome")
            valid_cards = (type(cards) is list and len(cards) <= 2
                           and all(type(card) is dict for card in cards))
            valid_outcome = ((outcome == "SUCCEEDED_SILENT" and valid_cards and len(cards) == 0)
                             or (outcome == "SUCCEEDED_WITH_CARDS" and valid_cards
                                 and 1 <= len(cards) <= 2))
            if (actual != {report.resolve(), receipt.resolve()}
                    or report_data.get("identity") != expected_identity
                    or receipt_data.get("identity") != expected_identity
                    or not valid_outcome
                    or receipt_data.get("report_sha256") != sha256(report_raw).hexdigest()):
                raise ValueError("receipt-bound report identity or digest mismatch")
        except Exception:
            self.close(); raise
        return {"KR": {"outcome": outcome, "cards": len(cards),
                       "report_readback": True, "receipt_readback": True, "replay_zero": True},
                "US": {"outcome": "PENDING", "cards": 0, "report_readback": False,
                       "receipt_readback": False, "replay_zero": True}}

    def bind_candidates(self, market, targets, *, candidate_digest):
        targets = tuple(targets)
        if (market not in ("KR", "US") or not 1 <= len(targets) <= 2
                or len(set(targets)) != len(targets)
                or candidate_digest != sha256("\0".join(targets).encode()).hexdigest()):
            raise ValueError("candidate target mismatch")
        self._candidates[market] = targets

    def bind_admitted(self, market, targets, *, candidate_digest, admitted_digest=None):
        targets = tuple(targets)
        candidates = self._candidates.get(market)
        if (market not in ("KR", "US") or not 0 <= len(targets) <= 2 or len(set(targets)) != len(targets)
                or candidates is None
                or not set(targets).issubset(candidates)
                or candidate_digest != sha256("\0".join(candidates).encode()).hexdigest()
                or (admitted_digest is not None and admitted_digest != sha256("\0".join(targets).encode()).hexdigest())):
            raise ValueError("admitted target mismatch")
        self._admitted[market] = targets
        self.ledger["bindings"][market] = {"candidates": list(candidates), "candidate_digest": candidate_digest,
            "admitted": list(targets), "admitted_digest": sha256("\0".join(targets).encode()).hexdigest()}
        _write_private(self.ledger_path, self.ledger)

    def call(self, operation, market, delegate, *, cardinality=None, target=None, validator=None):
        if self.replay: raise ValueError("terminal campaign")
        if operation not in SUBCAPS or market not in (None, "KR", "US") or (operation == "TOKEN") != (market is None):
            raise ValueError("operation cardinality invalid")
        if operation == "STOCKS" and (type(cardinality) is not int or not 1 <= cardinality <= 2):
            raise ValueError("cardinality invalid")
        if operation == "STOCK_CANDLES":
            admitted = self._admitted.get(market, ())
            prior = {row.get("target") for row in ()}  # targets are intentionally not durable/private output
            started = sum(row["operation"] == operation and row["market"] == market for row in self.ledger["reservations"])
            if target not in admitted or admitted.index(target) != started or target in prior:
                raise ValueError("candle target mismatch")
        rows = self.ledger["reservations"]; counts = Counter(row["operation"] for row in rows)
        market_count = sum(row["operation"] == operation and row["market"] == market for row in rows)
        if len(rows) >= TOTAL_CAP or counts[operation] >= SUBCAPS[operation] or (market is not None and market_count >= MARKET_CAPS[operation]):
            raise ValueError("campaign cap exceeded")
        if self._family is not None:
            self._family.reserve(self.campaign_id, operation, market)
        now_wall = self.wallclock()
        if now_wall.tzinfo is None or now_wall.utcoffset() is None:
            raise ValueError("clock must be aware")
        if rows:
            predecessor = datetime.fromisoformat(rows[-1]["started_at"]).astimezone(timezone.utc)
            delay = 2.0 - (now_wall.astimezone(timezone.utc) - predecessor).total_seconds()
            if delay > 0: self.sleeper(delay)
        row = {"seq": len(rows) + 1, "operation": operation, "market": market,
               "started_at": self.wallclock().astimezone(timezone.utc).isoformat(timespec="microseconds"), "settlement": None}
        if operation == "STOCK_CANDLES": row["target_sha256"] = sha256(target.encode()).hexdigest()
        rows.append(row); _write_private(self.ledger_path, self.ledger)
        try:
            result = delegate()
        except CampaignFailure as exc:
            row["settlement"] = exc.settlement
            row["failure_reason"] = exc.reason
            _write_private(self.ledger_path, self.ledger); self.close(); raise
        except Exception as exc:
            row["settlement"] = "TRANSPORT_FAILURE"
            row["failure_reason"] = ("timeout" if isinstance(exc, TimeoutError)
                                     else "connection" if isinstance(exc, (ConnectionError, OSError))
                                     else "protocol")
            _write_private(self.ledger_path, self.ledger); self.close(); raise
        except BaseException:
            self.close(); raise
        if validator is not None:
            try:
                validator(result)
            except CampaignFailure as exc:
                row["settlement"] = exc.settlement
                row["failure_reason"] = exc.reason
                _write_private(self.ledger_path, self.ledger); self.close(); raise
            except Exception:
                row["settlement"] = "CONTRACT_FAILURE"
                row["failure_reason"] = "contract"
                _write_private(self.ledger_path, self.ledger); self.close(); raise
            except BaseException:
                self.close(); raise
        row["settlement"] = "SUCCESS"; _write_private(self.ledger_path, self.ledger)
        return result

    def finish(self, products):
        bindings = {}
        for market, identity in self.manifest["identities"].items():
            base = self.report_root / "market_discovery" / market.lower() / identity
            report_raw = Path(f"{base}.report.json").read_bytes(); receipt_raw = Path(f"{base}.receipt.json").read_bytes()
            bindings[market] = {"identity": identity, "report_sha256": sha256(report_raw).hexdigest(),
                                "receipt_sha256": sha256(receipt_raw).hexdigest()}
        self.manifest["terminal_bindings"] = bindings
        self.manifest["products"] = products; self.manifest["terminal"] = True
        self.manifest["campaign_binding"] = _manifest_binding(self.manifest)
        self._verify_terminal_material()
        _write_private(self.manifest_path, self.manifest); self.close()

    def settle_partial(self):
        if self.partial_products is None:
            raise ValueError("partial settlement unavailable")
        identity = self.manifest["identities"]["KR"]
        base = self.report_root / "market_discovery" / "kr" / identity
        report_raw = Path(f"{base}.report.json").read_bytes()
        receipt_raw = Path(f"{base}.receipt.json").read_bytes()
        self.manifest["terminal_bindings"] = {"KR": {"identity": identity,
            "report_sha256": sha256(report_raw).hexdigest(),
            "receipt_sha256": sha256(receipt_raw).hexdigest()}}
        self.manifest["products"] = self.partial_products
        self.manifest["terminal"] = True
        self.manifest["campaign_binding"] = _manifest_binding(self.manifest)
        self._verify_terminal_material()
        _write_private(self.manifest_path, self.manifest)

    def close(self):
        if getattr(self, "_lock_fd", -1) >= 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN); os.close(self._lock_fd); self._lock_fd = -1
        if self._family is not None:
            self._family.close(); self._family = None

    def summary(self, status):
        rows = self.ledger["reservations"]
        counts = Counter((row["operation"], row["market"] or "NONE") for row in rows)
        return {"campaign_status": status, "total_starts": len(rows),
                "counts": {f"{op}:{market}": count for (op, market), count in sorted(counts.items())},
                "products": self.manifest["products"],
                "rate_remaining": 0 if self.manifest["terminal"] and status == "PARTIAL" else TOTAL_CAP - len(rows)}


class _PreviewFamily:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700); _secure_dir(root)
        self.path = root / "family-ledger.json"
        self._lock_fd = os.open(root / ".family.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC |
                                getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(self._lock_fd, 0o600)
        try: fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._lock_fd); self._lock_fd = -1
            raise ValueError("preview family lock unavailable") from exc
        self.ledger = _read_object(self.path) if self.path.exists() else {
            "policy_revision": PREVIEW_FAMILY_POLICY, "family_cap": PREVIEW_FAMILY_CAP,
            "sessions": {}, "starts": []}
        allowed_keys = {"policy_revision", "family_cap", "sessions", "starts"}
        if (set(self.ledger) not in (allowed_keys, allowed_keys | {"revalidations"})
                or self.ledger["policy_revision"] != PREVIEW_FAMILY_POLICY
                or self.ledger["family_cap"] != PREVIEW_FAMILY_CAP
                or not isinstance(self.ledger["sessions"], dict) or not isinstance(self.ledger["starts"], list)
                or len(self.ledger["starts"]) > PREVIEW_FAMILY_CAP):
            self.close(); raise ValueError("preview family ledger corrupt")
        prefix = "quant-g2-session-"
        def valid_campaign_id(value):
            return (type(value) is str and value.startswith(prefix)
                    and len(value) == len(prefix) + 24
                    and all(char in "0123456789abcdef" for char in value[len(prefix):]))

        registered_campaigns = set()
        registered_markets = {}
        for key, campaign_id in self.ledger["sessions"].items():
            market, separator, session = key.partition(":")
            try:
                parsed_session = date.fromisoformat(session)
            except (TypeError, ValueError) as exc:
                self.close(); raise ValueError("preview family ledger corrupt") from exc
            if (separator != ":" or market not in ("KR", "US") or type(parsed_session) is not date
                    or not valid_campaign_id(campaign_id)):
                self.close(); raise ValueError("preview family ledger corrupt")
            registered_campaigns.add(campaign_id)
            registered_markets.setdefault(campaign_id, set()).add(market)
        if (any(markets != {"KR", "US"} for markets in registered_markets.values())
                or len(registered_campaigns) > PREVIEW_SESSION_CAP):
            self.close(); raise ValueError("preview family ledger corrupt")

        revalidations = self.ledger.get("revalidations", {})
        if not isinstance(revalidations, dict):
            self.close(); raise ValueError("preview family ledger corrupt")
        successor_owners = {}
        for predecessor, chain in revalidations.items():
            if (predecessor not in registered_campaigns or not isinstance(chain, dict)
                    or set(chain) != {"repair_commit", "baseline_starts", "successors"}
                    or chain["repair_commit"] != SUCCESSOR_REPAIR_COMMIT
                    or chain["baseline_starts"] != SUCCESSOR_BASELINE_STARTS
                    or not isinstance(chain["successors"], list)
                    or not 1 <= len(chain["successors"]) <= SUCCESSOR_ATTEMPT_CAP
                    or len(set(chain["successors"])) != len(chain["successors"])):
                self.close(); raise ValueError("preview family ledger corrupt")
            for ordinal, successor in enumerate(chain["successors"], 1):
                expected = self.successor_id(predecessor, ordinal)
                if successor != expected or successor in registered_campaigns:
                    self.close(); raise ValueError("preview family ledger corrupt")
                registered_campaigns.add(successor)
                successor_owners[successor] = chain

        campaign_counts = Counter()
        for index, row in enumerate(self.ledger["starts"], 1):
            operation = row.get("operation") if isinstance(row, dict) else None
            market = row.get("market") if isinstance(row, dict) else None
            campaign_id = row.get("campaign_id") if isinstance(row, dict) else None
            valid_operation_market = ((operation == "TOKEN" and market is None)
                                      or (operation in {"RANKINGS", "STOCKS", "STOCK_CANDLES"}
                                          and market in {"KR", "US"}))
            if (not isinstance(row, dict)
                    or set(row) != {"seq", "campaign_id", "operation", "market"} or row["seq"] != index
                    or not valid_campaign_id(campaign_id) or campaign_id not in registered_campaigns
                    or not valid_operation_market):
                self.close(); raise ValueError("preview family ledger corrupt")
            campaign_counts[(campaign_id, operation, market)] += 1
            campaign_counts[(campaign_id, "TOTAL", None)] += 1
        for (campaign_id, operation, market), count in campaign_counts.items():
            if operation == "TOTAL":
                limit = TOTAL_CAP
            elif operation == "TOKEN":
                limit = 1
            else:
                limit = MARKET_CAPS[operation]
            if count > limit:
                self.close(); raise ValueError("preview family ledger corrupt")
        for successor, chain in successor_owners.items():
            successor_starts = sum(row["campaign_id"] in chain["successors"] for row in self.ledger["starts"])
            if successor_starts > SUCCESSOR_START_CAP:
                self.close(); raise ValueError("preview family ledger corrupt")

    @staticmethod
    def successor_id(predecessor, ordinal):
        payload = {"predecessor": predecessor, "ordinal": ordinal,
                   "repair_commit": SUCCESSOR_REPAIR_COMMIT}
        return "quant-g2-session-" + sha256(_canonical(payload)).hexdigest()[:24]

    def admit_successor(self, predecessor):
        chains = self.ledger.setdefault("revalidations", {})
        chain = chains.get(predecessor)
        if chain is None:
            chain = {"repair_commit": SUCCESSOR_REPAIR_COMMIT,
                     "baseline_starts": len(self.ledger["starts"]), "successors": []}
            chains[predecessor] = chain
        if len(chain["successors"]) >= SUCCESSOR_ATTEMPT_CAP:
            raise ValueError("same-session successor attempts exhausted")
        campaign_id = self.successor_id(predecessor, len(chain["successors"]) + 1)
        chain["successors"].append(campaign_id)
        _write_private(self.path, self.ledger)
        return campaign_id

    def reserve(self, campaign_id, operation, market):
        starts = self.ledger["starts"]
        if len(starts) >= PREVIEW_FAMILY_CAP:
            raise ValueError("preview family cap exceeded")
        campaign_starts = [row for row in starts if row["campaign_id"] == campaign_id]
        operation_starts = sum(row["operation"] == operation and row["market"] == market
                               for row in campaign_starts)
        operation_cap = 1 if operation == "TOKEN" else MARKET_CAPS[operation]
        if len(campaign_starts) >= TOTAL_CAP or operation_starts >= operation_cap:
            raise ValueError("preview family campaign cap exceeded")
        for chain in self.ledger.get("revalidations", {}).values():
            if campaign_id in chain["successors"]:
                successor_starts = sum(row["campaign_id"] in chain["successors"] for row in starts)
                if successor_starts >= SUCCESSOR_START_CAP:
                    raise ValueError("same-session revalidation cap exceeded")
        starts.append({"seq": len(starts) + 1, "campaign_id": campaign_id,
                       "operation": operation, "market": market})
        _write_private(self.path, self.ledger)

    def close(self):
        if getattr(self, "_lock_fd", -1) >= 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN); os.close(self._lock_fd); self._lock_fd = -1


def open_preview_campaign(family_root: Path, *, sessions: dict[str, date], identities: dict[str, str],
                          _test_policy_roots=None, **kwargs) -> MeteredCampaign:
    """Open the deterministic 10-session G2 campaign without resetting family ownership."""
    if (set(sessions) != {"KR", "US"} or set(identities) != {"KR", "US"}
            or any(type(value) is not date for value in sessions.values())
            or any(type(value) is not str or len(value) != 64 for value in identities.values())):
        raise ValueError("preview session identity invalid")
    family = _PreviewFamily(Path(family_root))
    payload = {"sessions": {k: sessions[k].isoformat() for k in sorted(sessions)},
               "identities": dict(sorted(identities.items()))}
    campaign_id = "quant-g2-session-" + sha256(_canonical(payload)).hexdigest()[:24]
    existing = family.ledger["sessions"]
    exact = all(existing.get(f"{market}:{sessions[market].isoformat()}") == campaign_id for market in ("KR", "US"))
    overlaps = any(f"{market}:{sessions[market].isoformat()}" in existing for market in ("KR", "US"))
    if overlaps and not exact:
        family.close(); raise ValueError("fresh completed session required")
    if not overlaps:
        registered_sessions = set(family.ledger["sessions"].values())
        if len(registered_sessions) >= PREVIEW_SESSION_CAP:
            family.close(); raise ValueError("preview session cap exceeded")
        for market in ("KR", "US"):
            existing[f"{market}:{sessions[market].isoformat()}"] = campaign_id
        _write_private(family.path, family.ledger)
    selected_id = campaign_id
    if exact:
        predecessor_root = Path(family_root) / "sessions" / campaign_id
        chain = family.ledger.get("revalidations", {}).get(campaign_id)
        if chain is None:
            if (sessions == SUCCESSOR_SESSIONS
                    and len(family.ledger["starts"]) == SUCCESSOR_BASELINE_STARTS
                    and _eligible_partial_predecessor(predecessor_root, campaign_id, sessions, identities)):
                try:
                    selected_id = family.admit_successor(campaign_id)
                except BaseException:
                    family.close(); raise
        else:
            selected_id = chain["successors"][-1]
            state = _successor_state(Path(family_root) / "sessions" / selected_id)
            if state == "SUCCESS":
                pass
            elif state in {"PARTIAL", "FAILED", "EXHAUSTED"}:
                try:
                    selected_id = family.admit_successor(campaign_id)
                except BaseException:
                    family.close(); raise
    root = Path(family_root) / "sessions" / selected_id
    root.parent.mkdir(exist_ok=True, mode=0o700)
    os.chmod(root.parent, 0o700); _secure_dir(root.parent)
    try:
        return MeteredCampaign(root, sessions=sessions, identities=identities, campaign_id=selected_id,
                               _test_policy_roots=_test_policy_roots, _family=family, **kwargs)
    except BaseException:
        family.close(); raise


def _eligible_partial_predecessor(root, campaign_id, sessions, identities):
    manifest_path, ledger_path = root / "campaign-manifest.json", root / "campaign-ledger.json"
    if not manifest_path.exists() or not ledger_path.exists():
        return False
    manifest, ledger = _read_object(manifest_path), _read_object(ledger_path)
    expected_ops = [("TOKEN", None), ("RANKINGS", "KR"), ("STOCKS", "KR"),
                    ("STOCK_CANDLES", "KR"), ("STOCK_CANDLES", "KR"),
                    ("RANKINGS", "US"), ("STOCKS", "US")]
    rows = ledger.get("reservations")
    return (manifest.get("campaign_id") == campaign_id
            and manifest.get("sessions") == {key: value.isoformat() for key, value in sorted(sessions.items())}
            and manifest.get("identities") == dict(sorted(identities.items()))
            and manifest.get("terminal") is True
            and manifest.get("products", {}).get("US", {}).get("outcome") == "PENDING"
            and set(manifest.get("terminal_bindings", {})) == {"KR"}
            and manifest.get("campaign_binding") in {_manifest_binding(manifest), _legacy_manifest_binding(manifest)}
            and isinstance(rows, list) and len(rows) == 7
            and [(row.get("operation"), row.get("market")) for row in rows] == expected_ops
            and all(row.get("settlement") == "SUCCESS" for row in rows)
            and set(ledger.get("bindings", {})) == {"KR", "US"})


def _successor_state(root):
    manifest_path, ledger_path = root / "campaign-manifest.json", root / "campaign-ledger.json"
    if not manifest_path.exists() or not ledger_path.exists():
        return "ACTIVE"
    manifest, ledger = _read_object(manifest_path), _read_object(ledger_path)
    if manifest.get("terminal") is True:
        return "PARTIAL" if manifest.get("products", {}).get("US", {}).get("outcome") == "PENDING" else "SUCCESS"
    rows = ledger.get("reservations", [])
    if any(row.get("settlement") != "SUCCESS" for row in rows):
        return "FAILED"
    return "EXHAUSTED" if len(rows) == TOTAL_CAP else "ACTIVE"


def _verified_replay_products(campaign, product_replay):
    if not callable(product_replay):
        raise ValueError("product replay verification required")
    ledger_before = campaign.ledger_path.read_bytes()
    products = {}
    expected_counts = (("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0))
    for market in ("KR", "US"):
        identity = campaign.manifest["identities"][market]
        replay = product_replay(market, identity)
        report_identity = getattr(getattr(replay, "report", None), "identity", None)
        receipt = getattr(replay, "receipt", None)
        receipt_identity = getattr(receipt, "identity", None)
        binding = campaign.manifest["terminal_bindings"][market]
        if (getattr(replay, "replay_counts", None) != expected_counts
                or getattr(report_identity, "identity_sha256", None) != identity
                or getattr(receipt_identity, "identity_sha256", None) != identity
                or getattr(receipt, "report_sha256", None) != binding["report_sha256"]):
            raise ValueError("product replay read-back mismatch")
        products[market] = {**campaign.manifest["products"][market], "replay_zero": True}
    if campaign.ledger_path.read_bytes() != ledger_before:
        raise ValueError("campaign ledger changed during replay")
    return products


def run_bounded_canary(*, campaign: MeteredCampaign, auth, run_market, local_projection,
                       product_replay=None):
    if campaign.replay:
        if campaign.manifest["products"].get("US", {}).get("outcome") == "PENDING":
            try:
                result = campaign.summary("PARTIAL")
                result["local"] = local_projection()
                return result
            finally:
                campaign.close()
        try:
            products = _verified_replay_products(campaign, product_replay)
            result = campaign.summary("REPLAYED")
            result["products"] = products
            result["local"] = local_projection()
            return result
        finally:
            campaign.close()
    if campaign.partial_products is not None:
        try:
            campaign.settle_partial()
            result = campaign.summary("PARTIAL")
            result["local"] = local_projection()
            return result
        finally:
            campaign.close()
    campaign.call("TOKEN", None, auth)
    products = {}
    for market in ("KR", "US"):
        products[market] = run_market(market, campaign.call)
        if products[market].get("outcome") in {"FAILED", "PROVIDER_FAILURE", "CONTRACT_FAILURE"}:
            raise ValueError("material market failure")
    local = local_projection(); campaign.finish(products)
    result = campaign.summary("SUCCEEDED"); result["local"] = local; return result
