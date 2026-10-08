from datetime import datetime, timezone
import os
import sqlite3

import pytest


def test_campaign_store_reserves_durable_started_and_never_allows_33rd(tmp_path):
    from tokmo.records.official_membership_campaign_store import OfficialMembershipCampaignStore

    path = tmp_path / "private" / "campaign.sqlite3"
    store = OfficialMembershipCampaignStore(path, clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc))
    first = store.reserve("SEC_SIC")
    assert first.ordinal == 1 and first.status == "STARTED"
    assert OfficialMembershipCampaignStore(path, clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc)).usage() == 1
    for _ in range(31):
        store.reserve("SEC_REGISTRY")
    with pytest.raises(ValueError, match="campaign_ceiling"):
        store.reserve("SEC_REGISTRY")
    assert store.usage() == 32


def test_failure_and_crash_reservations_remain_consumed(tmp_path):
    from tokmo.records.official_membership_campaign_store import OfficialMembershipCampaignStore

    store = OfficialMembershipCampaignStore(tmp_path / "p" / "c.sqlite3", clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc))
    reservation = store.reserve("SEC_SIC")
    store.finish(reservation.ordinal, "FAILED")
    assert store.read(reservation.ordinal).status == "FAILED"
    assert OfficialMembershipCampaignStore(store.path, clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc)).usage() == 1


def test_campaign_store_rejects_forged_non_strict_and_schema_drift(tmp_path):
    from tokmo.records.official_membership_campaign_store import OfficialMembershipCampaignStore
    path = tmp_path / "p" / "c.sqlite3"
    path.parent.mkdir(mode=0o700)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE starts(ordinal INTEGER PRIMARY KEY, family TEXT, endpoint_kind TEXT, status TEXT, started_at TEXT, terminal_code TEXT)")
    connection.commit(); connection.close(); os.chmod(path, 0o600)
    store = OfficialMembershipCampaignStore(path, clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="schema_mismatch"):
        store.usage()


def test_campaign_store_rejects_hardlink_and_symlink_paths(tmp_path):
    from tokmo.records.official_membership_campaign_store import OfficialMembershipCampaignStore
    clock=lambda: datetime(2026,8,13,tzinfo=timezone.utc)
    path=tmp_path/"p"/"c.sqlite3"; store=OfficialMembershipCampaignStore(path,clock=clock);store.usage()
    hard=tmp_path/"p"/"hard.sqlite3";os.link(path,hard)
    with pytest.raises(ValueError,match="private_path"): OfficialMembershipCampaignStore(hard,clock=clock).usage()
    path.unlink(); path.symlink_to(hard)
    with pytest.raises(ValueError,match="private_path"): store.usage()
