#!/usr/bin/env python3
"""Fixed exact-cohort gate; the public command exposes no dependency override.

Preparation runbook: default/offline stops at the zero-start preflight.  A later
authorized operator must select ``bounded-live-canary`` *and* ``--armed`` to
reach the seven-start exact-cohort cycle; that branch validates pre-existing
private paths, never creates them, and admits no consumer or recurring step.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
from typing import NamedTuple

from tokmo.composition import execute_official_membership_canary
from tokmo.facts.official_membership_live import OfficialMembershipLiveTransport
from tokmo.facts.quant_membership import load_production_sic_snapshot
from tokmo.programs.quant_membership import describe_us_personal_membership_cohort
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.official_membership_campaign_store import (
    CAMPAIGN_CEILING,
    OfficialMembershipCampaignStore,
)
from tokmo.records.official_membership_observation_store import (
    OfficialMembershipObservationStore,
)
from tokmo.runtime.config import load_sec_config
from tokmo.values.current_holdings import canonical_holdings_bytes
from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

CANARY_START_PLAN = 7
OFFICIAL_CAPTURE_CLASSIFICATION = "OFFICIAL_CAPTURE"
_CANARY_FAILURE_CODES = frozenset({
    "sic_resource", "private_path", "campaign_path", "holdings",
    "holdings_mutated", "cohort_size", "campaign_capacity",
    "acquisition_conflict", "canary_preflight", "canary_conflict",
    "source_failure", "canary_parse", "canary_assembly", "canary_readback",
})


class FixedProductionPaths(NamedTuple):
    sec_config: Path
    holdings: Path
    campaign: Path
    observations: Path

    @classmethod
    def production(cls):
        home = Path.home()
        private = home / ".local" / "share" / "tokmo-v3"
        membership = private / "official-membership"
        return cls(
            home / ".config" / "tokmo-v3" / "sec.env",
            private / "stores" / "current_holdings.json",
            membership / "live-transition-campaign.sqlite3",
            membership / "observations.sqlite3",
        )

    @classmethod
    def for_test(cls, root):
        """Explicit temporary dependency boundary; never called by main()."""
        root = Path(root)
        return cls(
            root / "config" / "sec.env",
            root / "stores" / "current_holdings.json",
            root / "official-membership" / "live-transition-campaign.sqlite3",
            root / "official-membership" / "observations.sqlite3",
        )


class PreflightResult(NamedTuple):
    acquisition_id: str
    expected_count: int
    remaining_capacity: int
    sec_user_agent: str


class ArmedContext(NamedTuple):
    """Fixed production dependency graph; built only after a green preflight."""
    paths: FixedProductionPaths
    sic_snapshot: OfficialSicSnapshotV1
    campaign_store: OfficialMembershipCampaignStore
    observation_store: OfficialMembershipObservationStore
    preflight: PreflightResult


def _require_private_directory(path):
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_nlink < 1):
        raise ValueError("private_path")


def _require_private_regular(path):
    _require_private_directory(Path(path).parent)
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1):
        raise ValueError("private_path")


def _fixed_acquisition_id(market_date):
    return "official-membership-canary-v1-" + market_date.isoformat()


def _fixed_campaign_store(paths, *, now, candidate=None):
    selected = paths.campaign if candidate is None else Path(candidate)
    if selected != paths.campaign or selected.name != "live-transition-campaign.sqlite3":
        raise ValueError("campaign_path")
    _require_private_directory(selected.parent)
    if os.path.lexists(selected):
        try:
            _require_private_regular(selected)
        except (OSError, ValueError):
            raise ValueError("campaign_path") from None
    aliases = tuple(
        item for item in selected.parent.glob("*campaign*.sqlite3")
        if item != selected
    )
    if aliases:
        raise ValueError("campaign_path")
    return OfficialMembershipCampaignStore(selected, clock=now)


def _fixed_holdings_reader(paths, *, clock):
    """Sole canonical holdings path; preflight and the seam both read here."""
    def read():
        _require_private_regular(paths.holdings)
        raw = paths.holdings.read_bytes()
        try:
            holdings = CurrentHoldingsStore(paths.holdings, clock=clock).read()
        except ValueError:
            raise ValueError("holdings") from None
        if holdings is None or raw != canonical_holdings_bytes(holdings):
            raise ValueError("holdings")
        return raw, holdings
    return read


def _evaluate_preflight(
    *, paths, now, sic_snapshot, campaign_store, observation_store,
):
    if (not isinstance(sic_snapshot, OfficialSicSnapshotV1)
            or sic_snapshot.classification != OFFICIAL_CAPTURE_CLASSIFICATION):
        raise ValueError("sic_resource")
    _require_private_regular(paths.sec_config)
    read_holdings = _fixed_holdings_reader(paths, clock=lambda: now)
    first, holdings = read_holdings()
    binding = describe_us_personal_membership_cohort(holdings, now)
    second, _ = read_holdings()
    if first != second:
        raise ValueError("holdings_mutated")
    if len(binding.expected_symbols) != 4:
        raise ValueError("cohort_size")
    used = campaign_store.usage()
    if used > CAMPAIGN_CEILING - CANARY_START_PLAN:
        raise ValueError("campaign_capacity")
    acquisition_id = _fixed_acquisition_id(binding.membership_as_of)
    if observation_store.read_acquisition(acquisition_id) is not None:
        raise ValueError("acquisition_conflict")
    sec = load_sec_config(paths.sec_config)
    return PreflightResult(acquisition_id, 4, CAMPAIGN_CEILING - used, sec.user_agent)


def _campaign_store_for_test(paths, *, now, candidate=None):
    """Temporary-root adapter; the public command cannot reach this name."""
    return _fixed_campaign_store(paths, now=now, candidate=candidate)


def _offline_preflight_for_test(**dependencies):
    """Injected preflight adapter; the public command cannot reach this name."""
    return _evaluate_preflight(**dependencies)


def _execute_armed(
    *, paths, now, sic_snapshot, campaign_store, observation_store, transport,
    clock,
):
    """Fresh preflight, then exactly the reviewed seven-start exact-cohort seam."""
    preflight = _evaluate_preflight(
        paths=paths, now=now, sic_snapshot=sic_snapshot,
        campaign_store=campaign_store, observation_store=observation_store,
    )
    return execute_official_membership_canary(
        campaign_store=campaign_store, observation_store=observation_store,
        transport=transport,
        holdings_reader=_fixed_holdings_reader(paths, clock=clock),
        sic_snapshot=sic_snapshot, sec_user_agent=preflight.sec_user_agent,
        clock=clock, acquisition_id=preflight.acquisition_id,
    )


def _execute_armed_for_test(**dependencies):
    """Temporary-layout/fake-transport adapter unreachable from main()."""
    return _execute_armed(**dependencies)


def _utc_now():
    return datetime.now(timezone.utc)


def _production_preflight(now):
    paths = FixedProductionPaths.production()
    snapshot = load_production_sic_snapshot()
    for directory in (
        paths.sec_config.parent,
        paths.holdings.parents[1], paths.holdings.parent,
        paths.campaign.parent,
    ):
        _require_private_directory(directory)
    campaign = _fixed_campaign_store(paths, now=lambda: now)
    observations = OfficialMembershipObservationStore(paths.observations)
    preflight = _evaluate_preflight(
        paths=paths, now=now, sic_snapshot=snapshot,
        campaign_store=campaign, observation_store=observations,
    )
    return ArmedContext(paths, snapshot, campaign, observations, preflight)


def _failure(code, *, campaign_starts=0, source_starts=0):
    sys.stderr.write(json.dumps(
        {"code": code, "campaign_starts": campaign_starts,
         "source_starts": source_starts},
        separators=(",", ":"), sort_keys=True,
    ) + "\n")
    return 2


def _safe_canary_failure_code(error):
    """Exact-match sanitizer; anything else collapses to UNKNOWN."""
    if type(error) is not ValueError:
        return "UNKNOWN"
    text = str(error)
    if text in _CANARY_FAILURE_CODES:
        return text.upper()
    return "UNKNOWN"


def _consumed_starts(campaign_store, before):
    """Durable ledger delta; any unreadable ledger assumes the whole plan.

    The catch stays wide on purpose: a driver-level failure (for example a
    sqlite ``DatabaseError``) must not escape and print an unsanitized
    traceback while the tool is emitting a sanitized failure line.
    """
    try:
        used = campaign_store.usage()
    except Exception:
        return CANARY_START_PLAN
    return min(max(used - before, 0), CANARY_START_PLAN)


def main(argv=None):
    parser = argparse.ArgumentParser(
        epilog="bounded execution requires pre-created owner-only private paths"
               " and separate --armed authorization")
    parser.add_argument(
        "--mode", choices=("offline-preflight", "bounded-live-canary"),
        default="offline-preflight",
    )
    parser.add_argument(
        "--armed", action="store_true",
        help="arm the separately authorized fixed seven-start exact-cohort gate")
    args = parser.parse_args(argv)
    if args.mode == "offline-preflight":
        if args.armed:
            parser.error("offline preflight cannot be armed")
        try:
            _production_preflight(_utc_now())
        except Exception:
            return _failure("OFFLINE_PREFLIGHT_REFUSED")
        return 0
    if not args.armed:
        parser.error("bounded live gate requires --armed")
    try:
        context = _production_preflight(_utc_now())
    except Exception:
        return _failure("PREFLIGHT_REFUSED")
    before = CAMPAIGN_CEILING - context.preflight.remaining_capacity
    try:
        _execute_armed(
            paths=context.paths, now=_utc_now(),
            sic_snapshot=context.sic_snapshot,
            campaign_store=context.campaign_store,
            observation_store=context.observation_store,
            transport=OfficialMembershipLiveTransport(clock=_utc_now),
            clock=_utc_now,
        )
    except Exception as error:
        consumed = _consumed_starts(context.campaign_store, before)
        return _failure(
            "CANARY_FAILED_" + _safe_canary_failure_code(error),
            campaign_starts=consumed, source_starts=consumed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
