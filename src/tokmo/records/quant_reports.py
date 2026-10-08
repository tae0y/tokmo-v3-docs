"""Single-writer immutable local report and terminal-receipt store."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.values.quant_discovery import (
    DecisionCardV1, FactorOrientation, FactorStandingV1, FactorStatus,
    FactorValueV1, QuantMarketProfileV1, QuantProductKind, QuantRunIdentityV1,
    QuantRunReceiptV1, QuantRunReportV1, QuantUnavailableCode,
    ReferenceEntryZoneV1,
)

_MAX = 1_000_000


class QuantReportConflict(ValueError):
    pass


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, Decimal)):
        return str(value)
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _bytes(value) -> bytes:
    return json.dumps(_plain(asdict(value)), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _identity(data):
    return QuantRunIdentityV1(
        QuantProductKind(data["product_kind"]), QuantMarketProfileV1(data["market"]),
        date.fromisoformat(data["completed_session_date"]) if data["completed_session_date"] else None,
        data["completed_iso_week"], data["profile_revision"],
        data["peer_registry_revision"], data["report_revision"], data["identity_sha256"],
    )


def _factor(data):
    return FactorValueV1(data["name"], Decimal(data["raw_value"]) if data["raw_value"] is not None else None,
                         FactorOrientation(data["orientation"]), FactorStatus(data["status"]),
                         data["observed_count"], data["minimum_count"])


def _standing(data):
    return FactorStandingV1(data["factor_name"], data["symbol"], Decimal(data["ordinal"]),
                            data["sample_size"], Decimal(data["raw_value"]), FactorOrientation(data["orientation"]),
                            Decimal(data["percentile"]) if data["percentile"] is not None else None, data["percentile_label"])


def _zone(data):
    if data is None:
        return None
    return ReferenceEntryZoneV1(Decimal(data["lower"]), Decimal(data["upper"]), data["setup_kind"],
                                tuple(data["evidence"]), Decimal(data["invalidation"]), data["expiry_completed_sessions"],
                                Decimal(data["latest_close"]), Decimal(data["distance_fraction"]), data["confidence"],
                                data["confidence_reason"], data["change_condition"])


def _report(raw):
    data = json.loads(raw)
    cards = tuple(DecisionCardV1(item["symbol"], QuantMarketProfileV1(item["market"]), item["name"],
                                 tuple(_factor(x) for x in item["factors"]), tuple(_standing(x) for x in item["standings"]),
                                 _zone(item["entry_zone"]), tuple(QuantUnavailableCode(x) for x in item["unavailable_codes"]))
                  for item in data["cards"])
    return QuantRunReportV1(_identity(data["identity"]), cards,
                            tuple(QuantUnavailableCode(x) for x in data["unavailable_codes"]),
                            tuple(tuple(x) for x in data["operation_counts"]), data["delivery_state"])


def decode_report(raw: bytes) -> QuantRunReportV1:
    """Decode exact stored report bytes; read-only helper for locators."""
    return _report(raw)


class QuantReportStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    @staticmethod
    def digest(report):
        return sha256(_bytes(report)).hexdigest()

    def _path(self, identity, kind):
        if len(identity.identity_sha256) != 64 or any(c not in "0123456789abcdef" for c in identity.identity_sha256):
            raise ValueError("invalid identity digest")
        return self.root / identity.product_kind.value.lower() / identity.market.value.lower() / f"{identity.identity_sha256}.{kind}.json"

    def append_if_absent(self, report):
        path = self._path(report.identity, "report")
        payload = _bytes(report)
        if path.exists():
            if read_atomic_json(path, max_bytes=_MAX) != payload:
                raise QuantReportConflict("same identity has different report")
            return self.digest(report)
        write_atomic_json(path, payload, max_bytes=_MAX)
        return self.digest(report)

    def read(self, identity):
        path = self._path(identity, "report")
        return None if not path.exists() else _report(read_atomic_json(path, max_bytes=_MAX))

    def read_digest(self, identity):
        path = self._path(identity, "report")
        return None if not path.exists() else sha256(read_atomic_json(path, max_bytes=_MAX)).hexdigest()

    def finish(self, identity, outcome, operation_counts, report_sha256):
        if self.read(identity) is None:
            raise ValueError("report must precede terminal success")
        if self.read_digest(identity) != report_sha256:
            raise ValueError("report digest changed before terminal success")
        receipt = QuantRunReceiptV1(identity, outcome, operation_counts, report_sha256, "NOT_ACTIVATED")
        path = self._path(identity, "receipt")
        payload = _bytes(receipt)
        if path.exists() and read_atomic_json(path, max_bytes=_MAX) != payload:
            raise QuantReportConflict("same identity has different receipt")
        if not path.exists():
            write_atomic_json(path, payload, max_bytes=_MAX)
        return receipt

    def read_receipt(self, identity):
        path = self._path(identity, "receipt")
        if not path.exists():
            return None
        data = json.loads(read_atomic_json(path, max_bytes=_MAX))
        return QuantRunReceiptV1(_identity(data["identity"]), data["outcome"], tuple(tuple(x) for x in data["operation_counts"]), data["report_sha256"], data["delivery_state"])
