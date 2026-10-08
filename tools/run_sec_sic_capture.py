#!/usr/bin/env python3
"""Fixed SIC capture gate.

Preparation runbook: default/offline performs no path read or source I/O.  A
later authorized operator must select ``bounded-live-canary`` *and* ``--armed``;
that branch validates pre-existing private directories and never creates them.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import NamedTuple

from tokmo.composition import execute_official_membership_sic_capture
from tokmo.facts.official_membership_live import EndpointKindV1, OfficialMembershipLiveTransport, OfficialMembershipRequestV1
from tokmo.records.canonical_observation_registry import OFFICIAL_MEMBERSHIP_DOCUMENTATION_CAPTURE_FAMILY, registered_family
from tokmo.records.official_membership_campaign_store import CAMPAIGN_CEILING, OfficialMembershipCampaignStore
from tokmo.records.official_membership_capture_store import OfficialMembershipCaptureStore
from tokmo.runtime.config import load_sec_config


class FixedSicPaths(NamedTuple):
    sec_config: Path
    campaign: Path
    capture: Path

    @classmethod
    def production(cls):
        home = Path.home()
        membership = home / ".local/share/tokmo-v3/official-membership"
        return cls(
            home / ".config/tokmo-v3/sec.env",
            membership / "live-transition-campaign.sqlite3",
            membership / "documentation-captures/capture.sqlite3",
        )

    @classmethod
    def for_test(cls, root):
        root = Path(root)
        membership = root / "official-membership"
        return cls(
            root / "config/sec.env",
            membership / "live-transition-campaign.sqlite3",
            membership / "documentation-captures/capture.sqlite3",
        )


class SicPreflightResult(NamedTuple):
    remaining_capacity: int
    sec_user_agent: str


class SicGateError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _private_dir(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise SicGateError("REQUIRED_DIRECTORY_MISSING") from None
    except OSError:
        raise SicGateError("PRIVATE_PATH_INVALID") from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise SicGateError("PRIVATE_PATH_INVALID")


def _private_file(path):
    _private_dir(Path(path).parent)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise SicGateError("REQUIRED_FILE_MISSING") from None
    except OSError:
        raise SicGateError("PRIVATE_PATH_INVALID") from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1):
        raise SicGateError("PRIVATE_PATH_INVALID")


def _fixed_campaign_store(paths, *, candidate=None):
    selected = paths.campaign if candidate is None else Path(candidate)
    if selected != paths.campaign or selected.name != "live-transition-campaign.sqlite3":
        raise ValueError("campaign_path")
    _private_dir(selected.parent)
    if os.path.lexists(selected):
        try:_private_file(selected)
        except (OSError, ValueError):raise ValueError("campaign_path") from None
    if any(item != selected for item in selected.parent.glob("*campaign*.sqlite3")):
        raise ValueError("campaign_path")
    from datetime import datetime, timezone
    return OfficialMembershipCampaignStore(selected, clock=lambda: datetime.now(timezone.utc))


def _campaign_store_for_test(paths, *, candidate=None):
    return _fixed_campaign_store(paths, candidate=candidate)


def _evaluate_preflight(*, paths, campaign_store, capture_store):
    identity = OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC)
    if not identity.url.startswith("https://www.sec.gov/"):
        raise ValueError("source_identity")
    if registered_family(OFFICIAL_MEMBERSHIP_DOCUMENTATION_CAPTURE_FAMILY) is None:
        raise ValueError("capture_family")
    _private_file(paths.sec_config)
    sec = load_sec_config(paths.sec_config)
    used = campaign_store.usage()
    if used > CAMPAIGN_CEILING - 1:
        raise ValueError("campaign_capacity")
    if capture_store.count() != 0:
        raise ValueError("capture_conflict")
    return SicPreflightResult(CAMPAIGN_CEILING - used, sec.user_agent)


def _offline_preflight_for_test(**dependencies):
    return _evaluate_preflight(**dependencies)


def _execute_armed(*, paths, campaign_store, capture_store, transport):
    preflight = _evaluate_preflight(
        paths=paths, campaign_store=campaign_store,
        capture_store=capture_store,
    )
    return execute_official_membership_sic_capture(
        campaign_store=campaign_store, capture_store=capture_store,
        transport=transport, sec_user_agent=preflight.sec_user_agent,
    )


def _execute_armed_for_test(**dependencies):
    """Temporary-layout/fake-transport adapter unreachable from main()."""
    return _execute_armed(**dependencies)


def _production_preflight():
    paths = FixedSicPaths.production()
    for directory in (paths.sec_config.parent, paths.campaign.parent, paths.capture.parent):
        _private_dir(directory)
    campaign = _fixed_campaign_store(paths)
    capture = OfficialMembershipCaptureStore(paths.capture)
    preflight = _evaluate_preflight(
        paths=paths, campaign_store=campaign, capture_store=capture)
    return paths, campaign, capture, preflight


def _refused(code="OFFLINE_PREFLIGHT_REFUSED", *, campaign_starts=0, source_starts=0):
    sys.stderr.write(json.dumps(
        {"campaign_starts": campaign_starts, "code": code, "source_starts": source_starts},
        separators=(",", ":"), sort_keys=True) + "\n")
    return 2


def _safe_capture_failure_code(error):
    text = str(error)
    prefix = "sic_capture_failed:"
    if not text.startswith(prefix):
        return "UNKNOWN"
    detail = text[len(prefix):]
    if detail == "TRANSPORT_FAILURE" or detail in {
        "CONTENT_ENCODING", "CONTENT_LENGTH", "BODY_TOO_LARGE",
    } or re.fullmatch(r"HTTP_[0-9]{3}", detail):
        return detail
    return "UNKNOWN"


def main(argv=None):
    parser = argparse.ArgumentParser(
        epilog="bounded execution requires pre-created owner-only directories and separate --armed authorization")
    parser.add_argument("--mode", choices=("offline-preflight", "bounded-live-canary"), default="offline-preflight")
    parser.add_argument("--armed", action="store_true", help="arm the separately authorized fixed one-GET gate")
    args = parser.parse_args(argv)
    if args.mode == "offline-preflight":
        if args.armed:
            parser.error("offline preflight cannot be armed")
        return _refused()
    if not args.armed:
        parser.error("bounded live gate requires --armed")
    try:
        paths, campaign, capture, preflight = _production_preflight()
    except SicGateError as error:
        return _refused(error.code)
    except (OSError, ValueError):
        return _refused("PREFLIGHT_REFUSED")
    try:
        _execute_armed(
            paths=paths, campaign_store=campaign, capture_store=capture,
            transport=OfficialMembershipLiveTransport(
                clock=lambda: __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc)),
        )
    except (OSError, ValueError) as error:
        safe = _safe_capture_failure_code(error)
        return _refused(f"CAPTURE_FAILED_{safe}", campaign_starts=1, source_starts=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
