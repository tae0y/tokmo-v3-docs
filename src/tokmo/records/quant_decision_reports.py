"""Small local report/history store. No broker or messaging connections."""

import json
import os
import re
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo


def atomic_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class DecisionReportStore:
    def __init__(self, root):
        self.root = Path(root)

    def save(self, report):
        identity = report["report_id"]
        if not re.fullmatch(r"qd-[a-f0-9]{16}", identity):
            raise ValueError("invalid report ID")
        path = self.root / (identity + ".json")
        if path.exists():
            if json.loads(path.read_text()) != report:
                raise ValueError("report ID conflict")
        else:
            atomic_private(path, report)
        return path

    def read(self, report_id):
        if not re.fullmatch(r"qd-[a-f0-9]{16}", report_id):
            raise ValueError("invalid report ID")
        return json.loads((self.root / (report_id + ".json")).read_text())

    def previous(self, *, before):
        found = []
        if not self.root.exists():
            return None
        for path in self.root.glob("qd-*.json"):
            try:
                report = json.loads(path.read_text())
                at = datetime.fromisoformat(report["as_of"])
                if at < before:
                    found.append((at, report))
            except (OSError, ValueError, KeyError):
                continue
        return max(found, key=lambda p: p[0])[1] if found else None

    def previous_successes(self, *, before, purpose):
        """Read immutable originals in existing request directories, no index.

        Reused rows are links, not replacement originals. A missing original,
        edited/manual report or legacy report without a contract is ineligible.
        """
        found = {}
        root = self.root.resolve()
        for path in sorted(root.rglob("qd-*.json")):
            if not re.fullmatch(r"qd-[a-f0-9]{16}\.json", path.name):
                continue
            if not path.resolve().is_relative_to(root):
                continue
            try:
                report = json.loads(path.read_text())
                at = datetime.fromisoformat(report["as_of"])
                if at >= before or report.get("purpose") != purpose:
                    continue
                payload = {k: v for k, v in report.items() if k != "report_id"}
                identity = (
                    "qd-"
                    + sha256(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()[:16]
                )
                if report["report_id"] != identity or path.stem != identity:
                    continue
                for row in report["results"]:
                    packet = row["packet"]
                    if (
                        row["status"] != "ANALYZED"
                        or row.get("analysis_source") not in {"model", "recorded"}
                        or not packet.get("analysis_contract")
                        or packet.get("purpose") != purpose
                        or packet["as_of"] != report["as_of"]
                    ):
                        continue
                    key = tuple(
                        packet[k]
                        for k in ("key", "account", "scope", "scenario", "purpose")
                    )
                    if key not in found or at > found[key][0]:
                        origin = {
                            "report_id": identity,
                            "path": str(path.resolve()),
                            "as_of": report["as_of"],
                        }
                        found[key] = (at, dict(row, analysis_origin=origin))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return {"results": [entry[1] for entry in found.values()]}


def meaningful_changes(report, previous, *, on_demand=False, now=None):
    """Changes relative to the last delivered report, not a failed send.

    Caller owns transmission and must advance its cursor only after success.
    Nighttime results are pending for morning, never marked delivered here.
    """
    previous_rows = (
        {r["identity"]: r for r in previous.get("results", [])} if previous else {}
    )
    changes = []
    for row in report["results"]:
        if row["status"] != "ANALYZED":
            continue
        current = row["analysis"]
        old = previous_rows.get(row["identity"])
        if on_demand:
            changes.append(row["identity"])
            continue
        if not old or not old.get("analysis"):
            if current["action"] not in {"HOLD", "WATCH"}:
                changes.append(row["identity"])
            continue
        old_analysis = old["analysis"]
        changed = (current["action"], current["thesis_status"]) != (
            old_analysis["action"],
            old_analysis["thesis_status"],
        )
        a, b = row.get("selected_option"), old.get("selected_option")
        if a and b:
            changed |= (a["quantity"], a["range_id"], a["scenario"]) != (
                b["quantity"],
                b["range_id"],
                b["scenario"],
            )
            if abs(float(a["price_upper"]) / float(b["price_upper"]) - 1) >= 0.05:
                changed = True
        used = {s for r in current["reasons"] for s in r["sources"]}
        previous_used = {s for r in old_analysis["reasons"] for s in r["sources"]}
        business_ids = {
            e["id"]
            for e in row["packet"]["evidence"]
            if e["kind"] in {"disclosure", "fundamental", "business", "user_thesis"}
        }
        changed |= bool((used - previous_used) & business_ids)
        if changed:
            changes.append(row["identity"])
    hour = (
        (now or datetime.fromisoformat(report["as_of"]))
        .astimezone(ZoneInfo("Asia/Seoul"))
        .hour
    )
    return {
        "report_id": report["report_id"],
        "identities": changes,
        "disposition": "RESPOND"
        if on_demand
        else "PENDING_MORNING"
        if changes and (hour >= 22 or hour < 8)
        else "READY"
        if changes
        else "SILENT",
    }


class DecisionDeliveryQueue:
    """One file for pending morning changes and the last acknowledged report.

    No transport or automatic retry lives here. Caller records a real send's
    receipt only after success. A failed or uncertain send keeps its cursor.
    """

    def __init__(self, root):
        self.store = DecisionReportStore(root)
        self.path = Path(root) / "delivery-state.json"

    def state(self):
        return (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"delivered": {}, "pending_report_id": None}
        )

    def stage(self, report, *, now):
        self.store.save(report)
        state = self.state()
        previous = {"results": list(state["delivered"].values())}
        result = meaningful_changes(report, previous, now=now)
        # A newer successful opinion replaces the overnight proposal. A
        # failed analysis preserves earlier evidence but never sends it as
        # though freshly verified; the unresolved item is explicit.
        state["pending_report_id"] = (
            report["report_id"] if result["identities"] else None
        )
        state["unresolved"] = [
            r["identity"] for r in report["results"] if r["status"] == "FAILED"
        ]
        state["staged_at"] = now.isoformat()
        atomic_private(self.path, state)
        return result

    def ready(self, *, now):
        state = self.state()
        identity = state.get("pending_report_id")
        if not identity:
            return None
        report = self.store.read(identity)
        result = meaningful_changes(
            report, {"results": list(state["delivered"].values())}, now=now
        )
        # Morning needs a new snapshot; a yesterday price/size is not an
        # instruction to buy this morning at yesterday's available balance.
        age = (now - datetime.fromisoformat(report["as_of"])).total_seconds()
        if result["disposition"] == "READY" and age > 3600:
            result["disposition"] = "REFRESH_REQUIRED"
        return result

    def acknowledge(self, report_id, identities, *, receipt, now):
        if not isinstance(receipt, str) or not receipt.strip():
            raise ValueError("send receipt required")
        report = self.store.read(report_id)
        rows = {
            r["identity"]: r for r in report["results"] if r["status"] == "ANALYZED"
        }
        if not set(identities) <= rows.keys():
            raise ValueError("acknowledgement does not match report")
        state = self.state()
        for identity in identities:
            state["delivered"][identity] = rows[identity]
        state["receipt"] = receipt
        state["acknowledged_at"] = now.isoformat()
        if state.get("pending_report_id") == report_id:
            remaining = meaningful_changes(
                report, {"results": list(state["delivered"].values())}, now=now
            )
            if not remaining["identities"]:
                state["pending_report_id"] = None
        atomic_private(self.path, state)
