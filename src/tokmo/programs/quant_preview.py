"""Local-only progress/readiness view for the bounded Quant value preview."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from hashlib import sha256
from pathlib import Path

from tokmo.values.quant_discovery import PeerRegistryV1, QuantProductKind

_SUCCESS_OUTCOMES = {"SUCCEEDED_WITH_CARDS", "SUCCEEDED_SILENT", "NO_DATA"}
_MARKETS = ("KR", "US")
_FAMILY_POLICY = "quant-g2-preview-family-rev-20260831"
_FAMILY_CAP = 106
_SESSION_CAP = 10
_CAMPAIGN_POLICY = "quant-bounded-canary-policy-v1"
_OPERATIONS = {"TOKEN": 1, "RANKINGS": 2, "STOCKS": 2, "STOCK_CANDLES": 4}
_MARKET_LIMITS = {"RANKINGS": 1, "STOCKS": 1, "STOCK_CANDLES": 2}
_PRODUCT_FIELDS = {
    "outcome",
    "cards",
    "report_readback",
    "receipt_readback",
    "replay_zero",
}
_SUCCESSOR_REPAIR_COMMIT = "e599953c72d79de94b6339fa228b960545b05188"
_SUCCESSOR_BASELINE_STARTS = 14
_SUCCESSOR_ATTEMPT_CAP = 5
_SUCCESSOR_START_CAP = 45


def _canonical(value) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def _campaign_id_valid(value) -> bool:
    prefix = "quant-g2-session-"
    return (
        type(value) is str
        and value.startswith(prefix)
        and len(value) == len(prefix) + 24
        and all(character in "0123456789abcdef" for character in value[len(prefix) :])
    )


def _successor_id(predecessor, ordinal):
    payload = {
        "predecessor": predecessor,
        "ordinal": ordinal,
        "repair_commit": _SUCCESSOR_REPAIR_COMMIT,
    }
    return "quant-g2-session-" + sha256(_canonical(payload)).hexdigest()[:24]


def _registered_campaigns(ledger):
    registered = set(ledger["sessions"].values())
    owners = {}
    for predecessor, chain in ledger.get("revalidations", {}).items():
        for successor in chain["successors"]:
            registered.add(successor)
            owners[successor] = predecessor
    return registered, owners


def _family_ledger_valid(ledger) -> bool:
    required = {"policy_revision", "family_cap", "sessions", "starts"}
    if (
        type(ledger) is not dict
        or set(ledger) not in (required, required | {"revalidations"})
        or ledger["policy_revision"] != _FAMILY_POLICY
        or ledger["family_cap"] != _FAMILY_CAP
        or type(ledger["sessions"]) is not dict
        or type(ledger["starts"]) is not list
        or len(ledger["starts"]) > _FAMILY_CAP
    ):
        return False
    registered = set()
    for key, campaign_id in ledger["sessions"].items():
        market, separator, session = key.partition(":")
        if (
            separator != ":"
            or market not in _MARKETS
            or not _campaign_id_valid(campaign_id)
        ):
            return False
        try:
            if type(date.fromisoformat(session)) is not date:
                return False
        except (TypeError, ValueError):
            return False
        registered.add(campaign_id)
    if len(registered) > _SESSION_CAP:
        return False
    revalidations = ledger.get("revalidations", {})
    if type(revalidations) is not dict:
        return False
    successors = set()
    for predecessor, chain in revalidations.items():
        if (
            predecessor not in registered
            or type(chain) is not dict
            or set(chain) != {"repair_commit", "baseline_starts", "successors"}
            or chain["repair_commit"] != _SUCCESSOR_REPAIR_COMMIT
            or chain["baseline_starts"] != _SUCCESSOR_BASELINE_STARTS
            or type(chain["successors"]) is not list
            or not 1 <= len(chain["successors"]) <= _SUCCESSOR_ATTEMPT_CAP
            or len(set(chain["successors"])) != len(chain["successors"])
        ):
            return False
        for ordinal, successor in enumerate(chain["successors"], 1):
            if (
                successor != _successor_id(predecessor, ordinal)
                or successor in registered
            ):
                return False
            registered.add(successor)
            successors.add(successor)
    counts, market_counts = Counter(), Counter()
    for index, row in enumerate(ledger["starts"], 1):
        if (
            type(row) is not dict
            or set(row) != {"seq", "campaign_id", "operation", "market"}
            or row["seq"] != index
            or row["campaign_id"] not in registered
            or row["operation"] not in _OPERATIONS
            or row["market"] not in (None, *_MARKETS)
            or (row["operation"] == "TOKEN") != (row["market"] is None)
        ):
            return False
        key = (row["campaign_id"], row["operation"])
        counts[key] += 1
        if row["market"] is not None:
            market_counts[(row["campaign_id"], row["operation"], row["market"])] += 1
    successor_starts = sum(row["campaign_id"] in successors for row in ledger["starts"])
    return (
        successor_starts <= _SUCCESSOR_START_CAP
        and all(
            counts[(campaign, operation)] <= limit
            for campaign in registered
            for operation, limit in _OPERATIONS.items()
        )
        and all(
            market_counts[(campaign, operation, market)] <= limit
            for campaign in registered
            for operation, limit in _MARKET_LIMITS.items()
            for market in _MARKETS
        )
        and all(
            sum(counts[(campaign, operation)] for operation in _OPERATIONS) <= 9
            for campaign in registered
        )
    )


def _manifest_binding(manifest) -> str:
    fields = {
        key: manifest[key]
        for key in (
            "campaign_id",
            "policy_revision",
            "sessions",
            "profile_revision",
            "report_revision",
            "identities",
            "terminal_bindings",
            "products",
        )
    }
    return sha256(_canonical(fields)).hexdigest()


def _verified_sessions(report_root: Path, market: str) -> set[str]:
    sessions: set[str] = set()
    leaf = report_root / "market_discovery" / market.lower()
    if not leaf.is_dir():
        return sessions
    for receipt_path in leaf.glob("*.receipt.json"):
        report_path = Path(str(receipt_path).replace(".receipt.json", ".report.json"))
        if not report_path.is_file():
            continue
        try:
            receipt = json.loads(receipt_path.read_bytes())
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
            identity = report["identity"]
            if (
                receipt.get("outcome") in _SUCCESS_OUTCOMES
                and receipt.get("report_sha256") == sha256(report_raw).hexdigest()
                and receipt.get("identity") == identity
                and identity.get("market") == market
                and type(identity.get("completed_session_date")) is str
            ):
                date.fromisoformat(identity["completed_session_date"])
                sessions.add(identity["completed_session_date"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sessions


def _verified_family_sessions(root: Path, market: str) -> set[str]:
    try:
        ledger = json.loads((root / "family-ledger.json").read_bytes())
        if not _family_ledger_valid(ledger):
            return set()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()

    registered, successor_owners = _registered_campaigns(ledger)
    sessions = set()
    manifest_keys = {
        "campaign_id",
        "policy_revision",
        "report_root",
        "sessions",
        "profile_revision",
        "report_revision",
        "identities",
        "terminal",
        "products",
        "terminal_bindings",
        "campaign_binding",
    }
    for campaign_id in registered:
        campaign = root / "sessions" / campaign_id
        manifest_path = campaign / "campaign-manifest.json"
        report_root = campaign / "report-store"
        try:
            if (
                campaign.is_symlink()
                or manifest_path.is_symlink()
                or report_root.is_symlink()
            ):
                continue
            manifest = json.loads(manifest_path.read_bytes())
            products = manifest.get("products")
            bound_markets = set(manifest.get("terminal_bindings", {}))
            if (
                type(manifest) is not dict
                or set(manifest) != manifest_keys
                or manifest["campaign_id"] != campaign_id
                or manifest["policy_revision"] != _CAMPAIGN_POLICY
                or manifest["report_root"] != str(report_root.resolve())
                or manifest["profile_revision"] != "quant-profile-v1"
                or manifest["report_revision"] != "quant-report-v1"
                or manifest["terminal"] is not True
                or set(manifest["sessions"]) != set(_MARKETS)
                or set(manifest["identities"]) != set(_MARKETS)
                or bound_markets not in ({"KR", "US"}, {"KR"})
                or type(products) is not dict
                or set(products) != set(_MARKETS)
                or manifest["campaign_binding"] != _manifest_binding(manifest)
            ):
                continue
            registration_id = successor_owners.get(campaign_id, campaign_id)
            expected_registration = {
                f"{bound_market}:{manifest['sessions'][bound_market]}": registration_id
                for bound_market in _MARKETS
            }
            actual_registration = {
                key: value
                for key, value in ledger["sessions"].items()
                if value == registration_id
            }
            if actual_registration != expected_registration:
                continue
            partial = bound_markets == {"KR"}
            if any(
                type(products[bound_market]) is not dict
                or set(products[bound_market]) != _PRODUCT_FIELDS
                or products[bound_market]["outcome"] not in _SUCCESS_OUTCOMES
                or type(products[bound_market]["cards"]) is not int
                or not 0 <= products[bound_market]["cards"] <= 2
                or products[bound_market]["report_readback"] is not True
                or products[bound_market]["receipt_readback"] is not True
                or type(products[bound_market]["replay_zero"]) is not bool
                for bound_market in bound_markets
            ) or (
                partial
                and products["US"]
                != {
                    "outcome": "PENDING",
                    "cards": 0,
                    "report_readback": False,
                    "receipt_readback": False,
                    "replay_zero": True,
                }
            ):
                continue
            expected_files = set()
            valid = True
            for bound_market in bound_markets:
                session = manifest["sessions"][bound_market]
                identity = manifest["identities"][bound_market]
                if (
                    type(session) is not str
                    or type(date.fromisoformat(session)) is not date
                    or type(identity) is not str
                    or len(identity) != 64
                    or any(
                        character not in "0123456789abcdef" for character in identity
                    )
                    or ledger["sessions"].get(f"{bound_market}:{session}")
                    != registration_id
                ):
                    valid = False
                    break
                base = (
                    report_root / "market_discovery" / bound_market.lower() / identity
                )
                report_path, receipt_path = (
                    Path(f"{base}.report.json"),
                    Path(f"{base}.receipt.json"),
                )
                if report_path.is_symlink() or receipt_path.is_symlink():
                    valid = False
                    break
                report_raw, receipt_raw = (
                    report_path.read_bytes(),
                    receipt_path.read_bytes(),
                )
                report, receipt = json.loads(report_raw), json.loads(receipt_raw)
                expected_identity = {
                    "product_kind": "MARKET_DISCOVERY",
                    "market": bound_market,
                    "completed_session_date": session,
                    "completed_iso_week": None,
                    "profile_revision": manifest["profile_revision"],
                    "peer_registry_revision": None,
                    "report_revision": manifest["report_revision"],
                    "identity_sha256": identity,
                }
                expected_binding = {
                    "identity": identity,
                    "report_sha256": sha256(report_raw).hexdigest(),
                    "receipt_sha256": sha256(receipt_raw).hexdigest(),
                }
                expected_files.update((report_path.resolve(), receipt_path.resolve()))
                if (
                    manifest["terminal_bindings"][bound_market] != expected_binding
                    or receipt.get("outcome") not in _SUCCESS_OUTCOMES
                    or receipt.get("report_sha256") != expected_binding["report_sha256"]
                    or report.get("identity") != expected_identity
                    or receipt.get("identity") != expected_identity
                    or not isinstance(report.get("cards"), list)
                    or products[bound_market]["outcome"] != receipt.get("outcome")
                    or products[bound_market]["cards"] != len(report["cards"])
                ):
                    valid = False
                    break
            actual_files = {
                path.resolve()
                for path in report_root.rglob("*")
                if path.is_file() or path.is_symlink()
            }
            if valid and actual_files == expected_files and market in bound_markets:
                sessions.add(manifest["sessions"][market])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sessions


def build_local_preview(
    *,
    report_root: str | Path,
    registry: PeerRegistryV1,
    personal_symbols,
    pension_symbols,
    as_of: date,
    target_completed_sessions: int,
    peer_review_max_age_days: int,
    legacy_report_root: str | Path | None = None,
) -> dict:
    """Summarize verified local reports and fail-closed reviewed-peer coverage."""
    if (
        type(target_completed_sessions) is not int
        or target_completed_sessions < 1
        or type(peer_review_max_age_days) is not int
        or peer_review_max_age_days < 1
        or type(as_of) is not date
    ):
        raise ValueError("invalid preview settings")
    root = Path(report_root)
    markets = {}
    for market in _MARKETS:
        sessions = (
            _verified_family_sessions(root, market)
            if (root / "family-ledger.json").is_file()
            else _verified_sessions(root, market)
        )
        if legacy_report_root is not None:
            sessions |= _verified_sessions(Path(legacy_report_root), market)
        completed = len(sessions)
        markets[market] = {
            "completed": completed,
            "remaining": max(0, target_completed_sessions - completed),
            "ready": completed >= target_completed_sessions,
        }

    confirmed = set()
    for group in registry.groups:
        age = (as_of - group.last_reviewed).days
        if 0 <= age <= peer_review_max_age_days:
            namespace = (
                "PERSONAL"
                if group.product_kind is QuantProductKind.PERSONAL_PEER
                else "PENSION"
            )
            confirmed.add((namespace, group.market.value, group.reference_symbol))
    personal = {("PERSONAL", market, symbol) for symbol, market in personal_symbols}
    pension = {("PENSION", market, symbol) for symbol, market in pension_symbols}
    missing_rows = (personal | pension) - confirmed
    missing = [
        ":".join(item)
        for item in sorted(
            missing_rows,
            key=lambda item: (0 if item[0] == "PERSONAL" else 1, item[1], item[2]),
        )
    ]
    peer = {
        "revision": registry.revision,
        "personal_confirmed": len(personal & confirmed),
        "personal_required": len(personal),
        "pension_confirmed": len(pension & confirmed),
        "pension_required": len(pension),
        "missing": missing,
        "ready": not missing,
    }
    return {
        "markets": markets,
        "value_gate_ready": all(row["ready"] for row in markets.values()),
        "peer_registry": peer,
    }
