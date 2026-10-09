import ast
import json
import shutil
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from email.message import Message
from hashlib import sha256
from pathlib import Path

import pytest


def _campaign(tmp_path, *, monotonic=None, sleeper=None):
    from tokmo.programs.quant_canary import MeteredCampaign

    return MeteredCampaign(
        tmp_path,
        sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
        identities={"KR": "a" * 64, "US": "b" * 64},
        monotonic=monotonic or (lambda: 0.0),
        sleeper=sleeper or (lambda _: None),
        _test_policy_roots=(),
    )


def _identity_payload(market, identity, *, session="2026-08-24"):
    return {
        "product_kind": "MARKET_DISCOVERY",
        "market": market,
        "completed_session_date": session,
        "completed_iso_week": None,
        "profile_revision": "quant-profile-v1",
        "peer_registry_revision": None,
        "report_revision": "quant-report-v1",
        "identity_sha256": identity,
    }


def _assert_preview_campaign_derives_distinct_session_identity_and_rejects_partial_reuse(
    tmp_path,
):
    from tokmo.programs.quant_canary import open_preview_campaign

    first = open_preview_campaign(
        tmp_path,
        sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
        identities={"KR": "a" * 64, "US": "b" * 64},
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    first_id = first.manifest["campaign_id"]
    assert first.root == tmp_path / "sessions" / first_id
    assert first.manifest["sessions"] == {"KR": "2026-08-24", "US": "2026-08-24"}
    first.close()

    second = open_preview_campaign(
        tmp_path,
        sessions={"KR": date(2026, 8, 25), "US": date(2026, 8, 25)},
        identities={"KR": "c" * 64, "US": "d" * 64},
        _test_policy_roots=(),
    )
    assert second.manifest["campaign_id"] != first_id
    second.close()

    with pytest.raises(ValueError, match="fresh completed session"):
        open_preview_campaign(
            tmp_path,
            sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 26)},
            identities={"KR": "a" * 64, "US": "e" * 64},
            _test_policy_roots=(),
        )


def _assert_preview_family_durably_caps_ten_session_campaigns_at_ninety_starts(
    tmp_path,
):
    from datetime import timedelta

    from tokmo.programs.quant_canary import open_preview_campaign

    def spend_session(day):
        marker = day.isoformat().replace("-", "")
        campaign = open_preview_campaign(
            tmp_path,
            sessions={"KR": day, "US": day},
            identities={
                "KR": sha256(("KR" + marker).encode()).hexdigest(),
                "US": sha256(("US" + marker).encode()).hexdigest(),
            },
            _test_policy_roots=(),
            sleeper=lambda _: None,
        )
        campaign.call("TOKEN", None, lambda: True)
        for market in ("KR", "US"):
            campaign.call("RANKINGS", market, lambda: True)
            targets = (market + "0", market + "1")
            digest = sha256("\0".join(targets).encode()).hexdigest()
            campaign.bind_candidates(market, targets, candidate_digest=digest)
            campaign.call("STOCKS", market, lambda: True, cardinality=2)
            campaign.bind_admitted(
                market, targets, candidate_digest=digest, admitted_digest=digest
            )
            for target in targets:
                campaign.call("STOCK_CANDLES", market, lambda: True, target=target)
        campaign.close()

    start = date(2026, 8, 1)
    for offset in range(10):
        spend_session(start + timedelta(days=offset))
    family = json.loads((tmp_path / "family-ledger.json").read_text())
    assert len(family["starts"]) == 90
    assert len({row["campaign_id"] for row in family["starts"]}) == 10

    day = start + timedelta(days=10)
    with pytest.raises(ValueError, match="preview session cap exceeded"):
        open_preview_campaign(
            tmp_path,
            sessions={"KR": day, "US": day},
            identities={"KR": "e" * 64, "US": "f" * 64},
            _test_policy_roots=(),
        )


def _assert_same_session_successors_are_durable_and_bounded(tmp_path):
    from tokmo.programs.quant_canary import (
        SUCCESSOR_ATTEMPT_CAP,
        SUCCESSOR_START_CAP,
        CampaignFailure,
        open_preview_campaign,
        run_bounded_canary,
    )

    assert (SUCCESSOR_ATTEMPT_CAP, SUCCESSOR_START_CAP) == (5, 45)

    sessions = {"KR": date(2026, 8, 25), "US": date(2026, 8, 25)}
    identities = {"KR": "c" * 64, "US": "d" * 64}

    # The approved family already contains seven unrelated starts plus the exact
    # seven-start PARTIAL predecessor.  Its campaign bytes must not be rewritten.
    other = open_preview_campaign(
        tmp_path,
        sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
        identities={"KR": "a" * 64, "US": "b" * 64},
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    for operation, market in (
        ("TOKEN", None),
        ("RANKINGS", "KR"),
        ("STOCKS", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("RANKINGS", "US"),
        ("STOCKS", "US"),
    ):
        if operation == "STOCKS":
            targets = (market + "0", market + "1")
            digest = sha256("\0".join(targets).encode()).hexdigest()
            other.bind_candidates(market, targets, candidate_digest=digest)
            other.call(operation, market, lambda: True, cardinality=2)
            other.bind_admitted(
                market, targets, candidate_digest=digest, admitted_digest=digest
            )
        elif operation == "STOCK_CANDLES":
            started = sum(
                row["operation"] == operation for row in other.ledger["reservations"]
            )
            other.call(operation, market, lambda: True, target=(market + str(started)))
        else:
            other.call(operation, market, lambda: True)
    other.close()
    predecessor = open_preview_campaign(
        tmp_path,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    predecessor.call("TOKEN", None, lambda: True)
    for market in ("KR", "US"):
        targets = (market + "0", market + "1")
        digest = sha256("\0".join(targets).encode()).hexdigest()
        predecessor.call("RANKINGS", market, lambda: True)
        predecessor.bind_candidates(market, targets, candidate_digest=digest)
        predecessor.call("STOCKS", market, lambda: True, cardinality=2)
        predecessor.bind_admitted(
            market, targets if market == "KR" else (), candidate_digest=digest
        )
        if market == "KR":
            for target in targets:
                predecessor.call("STOCK_CANDLES", market, lambda: True, target=target)
            identity = identities[market]
            leaf = predecessor.report_root / "market_discovery" / "kr"
            leaf.mkdir(parents=True)
            report = json.dumps(
                {
                    "identity": _identity_payload("KR", identity, session="2026-08-25"),
                    "cards": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            (leaf / f"{identity}.report.json").write_bytes(report)
            (leaf / f"{identity}.receipt.json").write_text(
                json.dumps(
                    {
                        "identity": _identity_payload(
                            "KR", identity, session="2026-08-25"
                        ),
                        "outcome": "SUCCEEDED_SILENT",
                        "report_sha256": sha256(report).hexdigest(),
                    }
                )
            )
            for path in leaf.iterdir():
                path.chmod(0o600)
    predecessor.close()
    partial = open_preview_campaign(
        tmp_path,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    run_bounded_canary(
        campaign=partial,
        auth=lambda: pytest.fail("repeated"),
        run_market=lambda *_: pytest.fail("repeated"),
        local_projection=lambda: {
            "personal_present": False,
            "personal_projected": 0,
            "pension_present": False,
            "pension_projected": 0,
        },
    )
    predecessor_root = partial.root
    predecessor_bytes = {
        path.name: path.read_bytes()
        for path in predecessor_root.iterdir()
        if path.is_file()
    }
    assert (
        len(json.loads((tmp_path / "family-ledger.json").read_text())["starts"]) == 14
    )

    success_family = tmp_path.parent / "success-replay"
    shutil.copytree(tmp_path, success_family)
    for manifest_path in success_family.rglob("campaign-manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        manifest["report_root"] = str((manifest_path.parent / "report-store").resolve())
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o600)
    successful = open_preview_campaign(
        success_family,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    for operation, market in (
        ("TOKEN", None),
        ("RANKINGS", "KR"),
        ("STOCKS", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("RANKINGS", "US"),
        ("STOCKS", "US"),
        ("STOCK_CANDLES", "US"),
        ("STOCK_CANDLES", "US"),
    ):
        if operation == "STOCKS":
            targets = (market + "0", market + "1")
            digest = sha256("\0".join(targets).encode()).hexdigest()
            successful.bind_candidates(market, targets, candidate_digest=digest)
            successful.call(operation, market, lambda: True, cardinality=2)
            successful.bind_admitted(
                market, targets, candidate_digest=digest, admitted_digest=digest
            )
        elif operation == "STOCK_CANDLES":
            started = sum(
                row["operation"] == operation and row["market"] == market
                for row in successful.ledger["reservations"]
            )
            successful.call(
                operation, market, lambda: True, target=market + str(started)
            )
        else:
            successful.call(operation, market, lambda: True)
    products = {}
    for market, identity in identities.items():
        leaf = successful.report_root / "market_discovery" / market.lower()
        leaf.mkdir(parents=True)
        report = json.dumps(
            {
                "identity": _identity_payload(market, identity, session="2026-08-25"),
                "cards": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        (leaf / f"{identity}.report.json").write_bytes(report)
        (leaf / f"{identity}.receipt.json").write_text(
            json.dumps(
                {
                    "identity": _identity_payload(
                        market, identity, session="2026-08-25"
                    ),
                    "outcome": "SUCCEEDED_SILENT",
                    "report_sha256": sha256(report).hexdigest(),
                }
            )
        )
        for path in leaf.iterdir():
            path.chmod(0o600)
        products[market] = {
            "outcome": "SUCCEEDED_SILENT",
            "cards": 0,
            "report_readback": True,
            "receipt_readback": True,
            "replay_zero": False,
        }
    successful_root = successful.root
    successful.finish(products)
    family_before_replay = (success_family / "family-ledger.json").read_bytes()
    replay = open_preview_campaign(
        success_family,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    assert replay.root == successful_root and replay.replay is True
    replayed = run_bounded_canary(
        campaign=replay,
        auth=lambda: pytest.fail("provider auth called"),
        run_market=lambda *_: pytest.fail("provider market called"),
        local_projection=lambda: {
            "personal_present": False,
            "personal_projected": 0,
            "pension_present": False,
            "pension_projected": 0,
        },
        product_replay=lambda market, identity: __import__("types").SimpleNamespace(
            report=__import__("types").SimpleNamespace(
                identity=__import__("types").SimpleNamespace(identity_sha256=identity)
            ),
            receipt=__import__("types").SimpleNamespace(
                identity=__import__("types").SimpleNamespace(identity_sha256=identity),
                report_sha256=json.loads(
                    (successful_root / "campaign-manifest.json").read_text()
                )["terminal_bindings"][market]["report_sha256"],
            ),
            replay_counts=(("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0)),
        ),
    )
    assert replayed["campaign_status"] == "REPLAYED"
    assert (success_family / "family-ledger.json").read_bytes() == family_before_replay
    assert (
        len(
            json.loads(family_before_replay)["revalidations"][
                json.loads((predecessor_root / "campaign-manifest.json").read_text())[
                    "campaign_id"
                ]
            ]["successors"]
        )
        == 1
    )

    # Later, unrelated completed sessions consume the family budget but must not
    # retroactively corrupt this closed successor chain or its zero-wire replay.
    family_path = success_family / "family-ledger.json"
    later = json.loads(family_path.read_text())
    pattern = (
        ("TOKEN", None),
        ("RANKINGS", "KR"),
        ("STOCKS", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("RANKINGS", "US"),
        ("STOCKS", "US"),
        ("STOCK_CANDLES", "US"),
        ("STOCK_CANDLES", "US"),
    )
    remaining = 37
    for offset in range(5):
        day = date(2026, 8, 26 + offset)
        campaign_id = "quant-g2-session-" + f"{offset + 1:024x}"
        for market in ("KR", "US"):
            later["sessions"][f"{market}:{day.isoformat()}"] = campaign_id
        for operation, market in pattern[:remaining]:
            later["starts"].append(
                {
                    "seq": len(later["starts"]) + 1,
                    "campaign_id": campaign_id,
                    "operation": operation,
                    "market": market,
                }
            )
        remaining -= min(remaining, len(pattern))
    family_path.write_text(
        json.dumps(later, sort_keys=True, separators=(",", ":")) + "\n"
    )
    family_path.chmod(0o600)
    assert remaining == 0 and len(later["starts"]) > 14 + 45
    later_replay = open_preview_campaign(
        success_family,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    assert later_replay.replay is True
    later_replay.close()

    first = open_preview_campaign(
        tmp_path,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    assert first.root != predecessor_root
    for operation, market in (
        ("TOKEN", None),
        ("RANKINGS", "KR"),
        ("STOCKS", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("RANKINGS", "US"),
        ("STOCKS", "US"),
        ("STOCK_CANDLES", "US"),
    ):
        if operation == "STOCKS":
            targets = (market + "0", market + "1")
            digest = sha256("\0".join(targets).encode()).hexdigest()
            first.bind_candidates(market, targets, candidate_digest=digest)
            first.call(operation, market, lambda: True, cardinality=2)
            first.bind_admitted(
                market, targets, candidate_digest=digest, admitted_digest=digest
            )
        elif operation == "STOCK_CANDLES":
            started = sum(
                row["operation"] == operation and row["market"] == market
                for row in first.ledger["reservations"]
            )
            first.call(operation, market, lambda: True, target=market + str(started))
        else:
            first.call(operation, market, lambda: True)
    with pytest.raises(CampaignFailure):
        first.call(
            "STOCK_CANDLES",
            "US",
            lambda: (_ for _ in ()).throw(CampaignFailure("connection")),
            target="US1",
        )
    second = open_preview_campaign(
        tmp_path,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    assert second.root not in {predecessor_root, first.root}
    for operation, market in (
        ("TOKEN", None),
        ("RANKINGS", "KR"),
        ("STOCKS", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("RANKINGS", "US"),
        ("STOCKS", "US"),
        ("STOCK_CANDLES", "US"),
        ("STOCK_CANDLES", "US"),
    ):
        if operation == "STOCKS":
            targets = (market + "0", market + "1")
            digest = sha256("\0".join(targets).encode()).hexdigest()
            second.bind_candidates(market, targets, candidate_digest=digest)
            second.call(operation, market, lambda: True, cardinality=2)
            second.bind_admitted(
                market, targets, candidate_digest=digest, admitted_digest=digest
            )
        elif operation == "STOCK_CANDLES":
            started = sum(
                row["operation"] == operation and row["market"] == market
                for row in second.ledger["reservations"]
            )
            second.call(operation, market, lambda: True, target=market + str(started))
        else:
            second.call(operation, market, lambda: True)
    second.close()
    for _ in range(3):
        successor = open_preview_campaign(
            tmp_path,
            sessions=sessions,
            identities=identities,
            _test_policy_roots=(),
            sleeper=lambda _: None,
        )
        assert successor.manifest["campaign_id"] not in {
            predecessor.manifest["campaign_id"],
            first.manifest["campaign_id"],
            second.manifest["campaign_id"],
        }
        for operation, market in (
            ("TOKEN", None),
            ("RANKINGS", "KR"),
            ("STOCKS", "KR"),
            ("STOCK_CANDLES", "KR"),
            ("STOCK_CANDLES", "KR"),
            ("RANKINGS", "US"),
            ("STOCKS", "US"),
            ("STOCK_CANDLES", "US"),
            ("STOCK_CANDLES", "US"),
        ):
            if operation == "STOCKS":
                targets = (market + "0", market + "1")
                digest = sha256("\0".join(targets).encode()).hexdigest()
                successor.bind_candidates(market, targets, candidate_digest=digest)
                successor.call(operation, market, lambda: True, cardinality=2)
                successor.bind_admitted(
                    market, targets, candidate_digest=digest, admitted_digest=digest
                )
            elif operation == "STOCK_CANDLES":
                started = sum(
                    row["operation"] == operation and row["market"] == market
                    for row in successor.ledger["reservations"]
                )
                successor.call(
                    operation, market, lambda: True, target=market + str(started)
                )
            else:
                successor.call(operation, market, lambda: True)
        successor.close()
    with pytest.raises(ValueError, match="successor|revalidation"):
        open_preview_campaign(
            tmp_path,
            sessions=sessions,
            identities=identities,
            _test_policy_roots=(),
            sleeper=lambda _: None,
        )
    family = json.loads((tmp_path / "family-ledger.json").read_text())
    assert len(family["starts"]) == 59
    assert len(next(iter(family["revalidations"].values()))["successors"]) == 5
    assert {
        path.name: path.read_bytes()
        for path in predecessor_root.iterdir()
        if path.is_file()
    } == predecessor_bytes

    malformed = tmp_path / "malformed"
    bad = open_preview_campaign(
        malformed,
        sessions=sessions,
        identities=identities,
        _test_policy_roots=(),
        sleeper=lambda _: None,
    )
    bad.call("TOKEN", None, lambda: True)
    bad.close()
    manifest_path = bad.root / "campaign-manifest.json"
    poisoned = json.loads(manifest_path.read_text())
    poisoned["terminal"] = True
    manifest_path.write_text(json.dumps(poisoned))
    manifest_path.chmod(0o600)
    with pytest.raises(ValueError, match="partial|predecessor|session|manifest"):
        open_preview_campaign(
            malformed,
            sessions=sessions,
            identities=identities,
            _test_policy_roots=(),
            sleeper=lambda _: None,
        )


def test_fixture_campaign_reserves_nine_and_replay_is_zero(tmp_path):
    from tokmo.programs.quant_canary import run_bounded_canary

    _assert_quant_pulse_window_gate_accepts_completed_receipt(tmp_path / "pulse-ok")
    _assert_quant_pulse_window_gate_refuses_without_valid_receipt(
        tmp_path / "pulse-missing"
    )
    _assert_preview_campaign_derives_distinct_session_identity_and_rejects_partial_reuse(
        tmp_path / "identity"
    )
    _assert_preview_family_durably_caps_ten_session_campaigns_at_ninety_starts(
        tmp_path / "family-cap"
    )
    _assert_same_session_successors_are_durable_and_bounded(tmp_path / "successors")
    _assert_one_sided_seven_start_campaign_settles_partial_without_recall(
        tmp_path / "partial"
    )
    _assert_one_sided_seven_start_campaign_settles_partial_without_recall(
        tmp_path / "partial-us-admitted", us_admitted=True
    )
    _assert_one_sided_seven_start_campaign_settles_partial_without_recall(
        tmp_path / "partial-cards", outcome="SUCCEEDED_WITH_CARDS", cards=[{}, {}]
    )
    for label, outcome, cards in (
        ("silent-with-card", "SUCCEEDED_SILENT", [{}]),
        ("cards-empty", "SUCCEEDED_WITH_CARDS", []),
        ("cards-over-cap", "SUCCEEDED_WITH_CARDS", [{}, {}, {}]),
        ("cards-malformed", "SUCCEEDED_WITH_CARDS", {"not": "a list"}),
        ("card-malformed", "SUCCEEDED_WITH_CARDS", ["not-a-card"]),
        ("unsupported", "NO_DATA", []),
    ):
        _assert_one_sided_seven_start_campaign_settles_partial_without_recall(
            tmp_path / label, outcome=outcome, cards=cards, expect_error=True
        )
    calls = []
    campaign = _campaign(tmp_path)

    def auth():
        calls.append(("TOKEN", None))
        return object()

    def market(name, metered):
        metered("RANKINGS", name, lambda: calls.append(("RANKINGS", name)) or object())
        metered(
            "STOCKS",
            name,
            lambda: calls.append(("STOCKS", name)) or object(),
            cardinality=2,
        )
        targets = (f"{name}0", f"{name}1")
        digest = sha256("\0".join(targets).encode()).hexdigest()
        campaign.bind_candidates(name, targets, candidate_digest=digest)
        campaign.bind_admitted(
            name, targets, candidate_digest=digest, admitted_digest=digest
        )
        for target in targets:
            metered(
                "STOCK_CANDLES",
                name,
                lambda: calls.append(("STOCK_CANDLES", name)) or object(),
                target=target,
            )
        return {
            "outcome": "SUCCEEDED_WITH_CARDS",
            "cards": 2,
            "report_readback": True,
            "receipt_readback": True,
            "replay_zero": False,
        }

    def terminal_material(name):
        identity = ("a" if name == "KR" else "b") * 64
        leaf = tmp_path / "report-store" / "market_discovery" / name.lower()
        leaf.mkdir(parents=True, exist_ok=True)
        report = json.dumps(
            {"identity": _identity_payload(name, identity), "cards": [{}, {}]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        (leaf / f"{identity}.report.json").write_bytes(report)
        (leaf / f"{identity}.receipt.json").write_text(
            json.dumps(
                {
                    "identity": _identity_payload(name, identity),
                    "outcome": "SUCCEEDED_WITH_CARDS",
                    "report_sha256": sha256(report).hexdigest(),
                }
            )
        )
        (leaf / f"{identity}.report.json").chmod(0o600)
        (leaf / f"{identity}.receipt.json").chmod(0o600)

    def bound_market(name, metered):
        result = market(name, metered)
        terminal_material(name)
        return result

    result = run_bounded_canary(
        campaign=campaign,
        auth=auth,
        run_market=bound_market,
        local_projection=lambda: {
            "personal_present": True,
            "personal_projected": 1,
            "pension_present": True,
            "pension_projected": 1,
        },
    )
    assert result["total_starts"] == 9 and len(calls) == 9
    ledger_before = (tmp_path / "campaign-ledger.json").read_bytes()
    replay_calls = []

    def product_replay(name, expected_identity):
        from types import SimpleNamespace

        replay_calls.append((name, expected_identity))
        identity = SimpleNamespace(identity_sha256=expected_identity)
        binding = json.loads((tmp_path / "campaign-manifest.json").read_text())[
            "terminal_bindings"
        ][name]
        return SimpleNamespace(
            report=SimpleNamespace(identity=identity),
            receipt=SimpleNamespace(
                identity=identity, report_sha256=binding["report_sha256"]
            ),
            replay_counts=(("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0)),
        )

    replay = run_bounded_canary(
        campaign=_campaign(tmp_path),
        auth=lambda: pytest.fail("auth"),
        run_market=lambda *_: pytest.fail("market"),
        local_projection=dict,
        product_replay=product_replay,
    )
    assert replay["total_starts"] == 9 and replay["campaign_status"] == "REPLAYED"
    assert replay_calls == [("KR", "a" * 64), ("US", "b" * 64)]
    assert all(
        product["replay_zero"] is True for product in replay["products"].values()
    )
    assert (tmp_path / "campaign-ledger.json").read_bytes() == ledger_before

    def false_zero(name, expected_identity):
        value = product_replay(name, expected_identity)
        value.replay_counts = (("WIRE", 1), ("RECOMPUTE", 0), ("RESEND", 0))
        return value

    with pytest.raises(ValueError, match="replay read-back"):
        run_bounded_canary(
            campaign=_campaign(tmp_path),
            auth=lambda: pytest.fail("auth"),
            run_market=lambda *_: pytest.fail("market"),
            local_projection=lambda: pytest.fail("local"),
            product_replay=false_zero,
        )
    assert (tmp_path / "campaign-ledger.json").read_bytes() == ledger_before
    ledger = json.loads((tmp_path / "campaign-ledger.json").read_text())
    assert len(ledger["reservations"]) == 9 and all(
        row["settlement"] == "SUCCESS" for row in ledger["reservations"]
    )
    report = next((tmp_path / "report-store").rglob("*.report.json"))
    tampered = json.dumps(
        {**json.loads(report.read_text()), "extra": 1},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    report.write_bytes(tampered)
    with pytest.raises(ValueError, match="report|receipt|digest"):
        _campaign(tmp_path)
    receipt = Path(str(report).replace(".report.json", ".receipt.json"))
    data = json.loads(receipt.read_text())
    data["report_sha256"] = sha256(tampered).hexdigest()
    receipt.write_text(json.dumps(data))
    receipt.chmod(0o600)
    with pytest.raises(ValueError, match="manifest|digest"):
        _campaign(tmp_path)

    foreign_root = tmp_path / "foreign-identity"
    foreign = _campaign(foreign_root)
    for name, identity in (("KR", "a" * 64), ("US", "b" * 64)):
        leaf = foreign.report_root / "market_discovery" / name.lower()
        leaf.mkdir(parents=True)
        poisoned = _identity_payload("FOREIGN", identity, session="1900-01-01")
        poisoned["report_revision"] = "foreign-report-v999"
        report_raw = json.dumps(
            {"identity": poisoned}, sort_keys=True, separators=(",", ":")
        ).encode()
        (leaf / f"{identity}.report.json").write_bytes(report_raw)
        (leaf / f"{identity}.receipt.json").write_text(
            json.dumps(
                {"identity": poisoned, "report_sha256": sha256(report_raw).hexdigest()}
            )
        )
        (leaf / f"{identity}.report.json").chmod(0o600)
        (leaf / f"{identity}.receipt.json").chmod(0o600)
    try:
        with pytest.raises(ValueError, match="identity|product"):
            foreign.finish({})
    finally:
        foreign.close()


def _assert_one_sided_seven_start_campaign_settles_partial_without_recall(
    tmp_path,
    *,
    us_admitted=False,
    outcome="SUCCEEDED_SILENT",
    cards=None,
    expect_error=False,
):
    from tokmo.programs.quant_canary import run_bounded_canary

    campaign = _campaign(tmp_path)
    campaign.call("TOKEN", None, lambda: True)
    for market, targets in (("KR", ("KR0", "KR1")), ("US", ("US0", "US1"))):
        campaign.call("RANKINGS", market, lambda: True)
        digest = sha256("\0".join(targets).encode()).hexdigest()
        campaign.bind_candidates(market, targets, candidate_digest=digest)
        campaign.call("STOCKS", market, lambda: True, cardinality=2)
        campaign.bind_admitted(
            market,
            targets if market == "KR" or us_admitted else (),
            candidate_digest=digest,
        )
        if market == "KR":
            for target in targets:
                campaign.call("STOCK_CANDLES", market, lambda: True, target=target)
            identity = "a" * 64
            leaf = campaign.report_root / "market_discovery" / "kr"
            leaf.mkdir(parents=True)
            cards = [] if cards is None else cards
            report = json.dumps(
                {"identity": _identity_payload("KR", identity), "cards": cards},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            (leaf / f"{identity}.report.json").write_bytes(report)
            (leaf / f"{identity}.receipt.json").write_text(
                json.dumps(
                    {
                        "identity": _identity_payload("KR", identity),
                        "outcome": outcome,
                        "report_sha256": sha256(report).hexdigest(),
                    }
                )
            )
            for path in leaf.iterdir():
                path.chmod(0o600)
    campaign.close()

    if expect_error:
        with pytest.raises(ValueError, match="identity|digest|material|outcome|cards"):
            _campaign(tmp_path)
        return
    resumed = _campaign(tmp_path)
    ledger_before = resumed.ledger_path.read_bytes()
    result = run_bounded_canary(
        campaign=resumed,
        auth=lambda: pytest.fail("token repeated"),
        run_market=lambda *_: pytest.fail("market repeated"),
        local_projection=lambda: {
            "personal_present": False,
            "personal_projected": 0,
            "pension_present": False,
            "pension_projected": 0,
        },
    )
    assert result["campaign_status"] == "PARTIAL"
    assert result["total_starts"] == 7 and result["rate_remaining"] == 0
    assert result["products"]["KR"]["outcome"] == outcome
    assert result["products"]["KR"]["cards"] == len(cards)
    assert result["products"]["US"] == {
        "outcome": "PENDING",
        "cards": 0,
        "report_readback": False,
        "receipt_readback": False,
        "replay_zero": True,
    }
    assert resumed.ledger_path.read_bytes() == ledger_before
    assert (
        json.loads((tmp_path / "campaign-manifest.json").read_text())["terminal"]
        is True
    )

    receipt = next((tmp_path / "report-store").rglob("*.receipt.json"))
    poisoned = json.loads(receipt.read_text())
    poisoned["report_sha256"] = "0" * 64
    receipt.write_text(json.dumps(poisoned))
    receipt.chmod(0o600)
    with pytest.raises(ValueError, match="identity|digest|binding"):
        _campaign(tmp_path)


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("http_429", "HTTP_FAILURE"),
        ("http_5xx", "HTTP_FAILURE"),
        ("bad_request", "HTTP_FAILURE"),
        ("forbidden", "HTTP_FAILURE"),
        ("auth_rejection", "HTTP_FAILURE"),
        ("timeout", "TRANSPORT_FAILURE"),
        ("connection", "TRANSPORT_FAILURE"),
        ("redirect", "TRANSPORT_FAILURE"),
        ("protocol", "TRANSPORT_FAILURE"),
        ("parser", "PARSER_FAILURE"),
        ("contract", "CONTRACT_FAILURE"),
        ("crash", None),
    ],
)
def test_failure_consumes_reservation_and_restart_blocks(tmp_path, failure, expected):
    from tokmo.programs.quant_canary import CampaignFailure

    campaign = _campaign(tmp_path)

    def bad():
        if failure != "crash":
            raise CampaignFailure(failure)
        raise KeyboardInterrupt()

    with pytest.raises(BaseException):
        campaign.call("TOKEN", None, bad)
    rows = json.loads((tmp_path / "campaign-ledger.json").read_text())["reservations"]
    assert len(rows) == 1 and rows[0]["settlement"] == expected
    assert rows[0].get("failure_reason") == (None if failure == "crash" else failure)
    if failure == "http_429":
        rows[0].pop("failure_reason")
        (tmp_path / "campaign-ledger.json").write_text(
            json.dumps(
                {
                    "campaign_id": campaign.campaign_id,
                    "reservations": rows,
                    "bindings": {},
                }
            )
        )
        (tmp_path / "campaign-ledger.json").chmod(0o600)
    with pytest.raises(ValueError, match="unresolved|stopped"):
        _campaign(tmp_path).call("RANKINGS", "KR", lambda: pytest.fail("delegate"))
    if failure == "connection":
        transport_root = tmp_path / "ordinary-transport"
        ordinary = _campaign(transport_root)
        with pytest.raises(OSError, match="wire"):
            ordinary.call(
                "TOKEN",
                None,
                lambda: (_ for _ in ()).throw(OSError("wire")),
                validator=lambda _: pytest.fail("validator must not run"),
            )
        row = json.loads((transport_root / "campaign-ledger.json").read_text())[
            "reservations"
        ][0]
        assert row["settlement"] == "TRANSPORT_FAILURE"
        assert row["failure_reason"] == "connection"


def test_cap_cardinality_and_spacing_refuse_before_delegate(tmp_path):
    clock = [0.0]
    sleeps = []
    calls = []

    def monotonic():
        return clock[0]

    def sleeper(value):
        sleeps.append(value)
        clock[0] += value

    campaign = _campaign(tmp_path, monotonic=monotonic, sleeper=sleeper)
    campaign.call("TOKEN", None, lambda: calls.append(1))
    clock[0] = 0.5
    campaign.call("RANKINGS", "KR", lambda: calls.append(1))
    assert sleeps and all(value > 0 for value in sleeps)
    digest = sha256(b"A\0B").hexdigest()
    campaign.bind_candidates("KR", ("A", "B"), candidate_digest=digest)
    campaign.bind_admitted(
        "KR", ("A", "B"), candidate_digest=digest, admitted_digest=digest
    )
    with pytest.raises(ValueError, match="target"):
        campaign.call("STOCK_CANDLES", "KR", lambda: calls.append(1), target="X")
    with pytest.raises(ValueError, match="cardinality"):
        campaign.call("STOCKS", "KR", lambda: calls.append(1), cardinality=3)
    for market, operation, count in (
        ("US", "RANKINGS", 1),
        ("KR", "STOCKS", 1),
        ("US", "STOCKS", 1),
        ("KR", "STOCK_CANDLES", 2),
        ("US", "STOCK_CANDLES", 2),
    ):
        if operation == "STOCK_CANDLES":
            targets = tuple(f"{market}{i}" for i in range(count))
            digest = sha256("\0".join(targets).encode()).hexdigest()
            campaign.bind_candidates(market, targets, candidate_digest=digest)
            campaign.bind_admitted(
                market, targets, candidate_digest=digest, admitted_digest=digest
            )
        for i in range(count):
            campaign.call(
                operation,
                market,
                lambda: calls.append(1),
                cardinality=1 if operation == "STOCKS" else None,
                target=(f"{market}{i}" if operation == "STOCK_CANDLES" else None),
            )
    with pytest.raises(ValueError, match="cap|target"):
        campaign.call("STOCK_CANDLES", "US", lambda: calls.append(1))
    assert len(calls) == 9


@pytest.mark.parametrize(
    "state",
    [
        "bad-manifest",
        "foreign-ledger",
        "unresolved",
        "terminal-no-manifest",
        "one-sided",
    ],
)
def test_mixed_preflight_is_provider_zero(tmp_path, state):
    from tokmo.programs.quant_canary import MeteredCampaign

    if state == "bad-manifest":
        (tmp_path / "campaign-manifest.json").write_text(json.dumps({"bad": True}))
    elif state == "foreign-ledger":
        (tmp_path / "campaign-ledger.json").write_text(
            json.dumps({"campaign_id": "other", "reservations": []})
        )
    elif state == "unresolved":
        (tmp_path / "campaign-ledger.json").write_text(
            json.dumps(
                {
                    "campaign_id": "quant-bounded-canary-20260825-a",
                    "reservations": [
                        {
                            "seq": 1,
                            "operation": "TOKEN",
                            "market": None,
                            "settlement": None,
                        }
                    ],
                }
            )
        )
    elif state == "one-sided":
        seed = MeteredCampaign(
            tmp_path,
            sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
            identities={"KR": "a" * 64, "US": "b" * 64},
            _test_policy_roots=(),
        )
        seed.close()
        leaf = tmp_path / "report-store" / "market_discovery" / "kr"
        leaf.mkdir(parents=True)
        (leaf / f"{'a' * 64}.report.json").write_text("{}")
    else:
        leaf = tmp_path / "report-store" / "market_discovery" / "kr"
        leaf.mkdir(parents=True)
        (leaf / f"{'a' * 64}.report.json").write_text("{}")
        if state == "one-sided":
            (tmp_path / "campaign-manifest.json").write_text(
                json.dumps({"campaign_id": "wrong"})
            )
    with pytest.raises(ValueError):
        MeteredCampaign(
            tmp_path,
            sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
            identities={"KR": "a" * 64, "US": "b" * 64},
        )


def _assert_quant_pulse_window_gate_accepts_completed_receipt(tmp_path):
    from tools.run_quant_canary_once import wait_for_pulse_safe_window

    now = datetime(2026, 9, 1, 0, 0, 5, tzinfo=UTC)
    slot = tmp_path / "20260901T0000"
    slot.mkdir(parents=True)
    (slot / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": "tokmo-v3-pulse-20260901T0000",
                "status": "SUCCEEDED",
                "child_exit_code": 0,
                "timed_out": False,
                "finished_at": "2026-09-01T00:00:02+00:00",
            }
        )
    )
    assert wait_for_pulse_safe_window(
        tmp_path, clock=lambda: now, monotonic=lambda: 0.0, sleeper=lambda _: None
    )
    late = datetime(2026, 9, 1, 0, 0, 45, tzinfo=UTC)
    ticks = iter((0.0, 1.0))
    assert not wait_for_pulse_safe_window(
        tmp_path,
        clock=lambda: late,
        monotonic=lambda: next(ticks),
        sleeper=lambda _: None,
        timeout_seconds=0.5,
    )


def _assert_quant_pulse_window_gate_refuses_without_valid_receipt(tmp_path):
    from tools.run_quant_canary_once import wait_for_pulse_safe_window

    now = datetime(2026, 9, 1, 0, 0, 5, tzinfo=UTC)
    ticks = iter((0.0, 1.0))
    assert not wait_for_pulse_safe_window(
        tmp_path,
        clock=lambda: now,
        monotonic=lambda: next(ticks),
        sleeper=lambda _: None,
        timeout_seconds=0.5,
    )
    slot = tmp_path / "20260901T0000"
    slot.mkdir(parents=True)
    for bad in (
        None,
        [],
        {
            "job_id": "wrong",
            "status": "SUCCEEDED",
            "child_exit_code": 0,
            "timed_out": False,
        },
        {
            "job_id": "tokmo-v3-pulse-20260901T0000",
            "status": "FAILED",
            "child_exit_code": 1,
            "timed_out": False,
        },
    ):
        (slot / "manifest.json").write_text(json.dumps(bad))
        ticks = iter((0.0, 1.0))
        assert not wait_for_pulse_safe_window(
            tmp_path,
            clock=lambda: now,
            monotonic=lambda: next(ticks),
            sleeper=lambda _: None,
            timeout_seconds=0.5,
        )
    from tools.run_quant_canary_once import build_production_runner

    ticks = iter((0.0, 1.0))
    assert not wait_for_pulse_safe_window(
        tmp_path,
        clock=lambda: datetime(2026, 9, 1, 0, 0, 5),
        monotonic=lambda: next(ticks),
        sleeper=lambda _: None,
        timeout_seconds=0.5,
    )
    with pytest.raises(ValueError, match="pulse token window refused"):
        build_production_runner(
            pulse_gate=lambda: False,
            clock=lambda: (_ for _ in ()).throw(AssertionError("clock reached")),
        )


def test_public_entrypoint_injected_factory_is_sanitized(capsys):
    from tokmo.programs.quant_canary import CampaignFailure
    from tools.run_quant_canary_once import _wire_failure_reason, main

    def factory():
        product = {
            "outcome": "SUCCEEDED_SILENT",
            "cards": 0,
            "report_readback": True,
            "receipt_readback": True,
            "replay_zero": True,
        }
        return lambda: {
            "campaign_status": "SUCCEEDED",
            "total_starts": 0,
            "counts": {},
            "products": {"KR": dict(product), "US": dict(product)},
            "local": {
                "personal_present": False,
                "personal_projected": 0,
                "pension_present": False,
                "pension_projected": 0,
            },
            "rate_remaining": 9,
        }

    assert main([], factory=factory) == 0
    output = capsys.readouterr()
    assert set(json.loads(output.out)) == {
        "campaign_status",
        "total_starts",
        "counts",
        "products",
        "local",
        "rate_remaining",
    }
    assert output.err == "" and "token" not in output.out.lower()

    def bad_factory():
        return lambda: {
            "campaign_status": "SUCCEEDED",
            "total_starts": 0,
            "counts": {"x": {"symbol": "SECRET"}},
            "products": {},
            "local": {},
            "rate_remaining": 9,
        }

    assert main([], factory=bad_factory) == 2
    for poisoned in (
        {"counts": {"TOKEN:NONE/secret": 1}},
        {
            "products": {
                "KR": {
                    "outcome": "/private/provider",
                    "cards": 0,
                    "report_readback": True,
                    "receipt_readback": True,
                    "replay_zero": True,
                }
            }
        },
    ):
        value = factory()()
        value.update(poisoned)
        assert main([], factory=lambda value=value: lambda: value) == 2
    capsys.readouterr()
    assert (
        main(
            [],
            factory=lambda: lambda: (_ for _ in ()).throw(CampaignFailure("http_429")),
        )
        == 2
    )
    refused = json.loads(capsys.readouterr().err)
    assert refused == {"campaign_status": "REFUSED", "reason": "http_429"}
    assert {
        _wire_failure_reason(code)
        for code in (
            "http_429",
            "http_5xx",
            "bad_request",
            "forbidden",
            "auth_rejection",
            "timeout",
            "connection_failure",
            "redirect",
            "protocol_failure",
        )
    } == {
        "http_429",
        "http_5xx",
        "bad_request",
        "forbidden",
        "auth_rejection",
        "timeout",
        "connection",
        "redirect",
        "protocol",
    }
    with pytest.raises(ValueError, match="reason"):
        CampaignFailure("private-provider-text")


def test_architecture_privacy_and_adjusted_request(monkeypatch):
    root = Path(__file__).parents[1]
    source = root / "src/tokmo/programs/quant_canary.py"
    tree = ast.parse(source.read_text())
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(
        name.endswith(("toss_auth_live", "toss_reads_live")) for name in imports
    )
    tests = [
        node
        for node in ast.walk(
            ast.parse((root / "tests/test_quant_canary.py").read_text())
        )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    assert len(tests) <= 6
    import tempfile

    from tokmo.programs.quant_canary import MeteredCampaign

    with tempfile.TemporaryDirectory() as outer:
        root = Path(outer)
        canary = root / "canary"
        forbidden = root / "stores"
        forbidden.mkdir()
        with pytest.raises(ValueError, match="boundary"):
            MeteredCampaign(
                canary,
                sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
                identities={"KR": "a" * 64, "US": "b" * 64},
                _test_policy_roots=(root, forbidden),
            )
    with tempfile.TemporaryDirectory() as outer:
        home = Path(outer)
        canonical_family = home / ".local/share/tokmo-v3/ops/quant-g2-preview"
        campaign_id = "quant-g2-session-" + "a" * 24
        allowed = canonical_family / "sessions" / campaign_id
        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "home", classmethod(lambda cls: home))
            production = MeteredCampaign(
                allowed,
                campaign_id=campaign_id,
                sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
                identities={"KR": "a" * 64, "US": "b" * 64},
            )
            production.close()
            refused = (
                home / ".local/share/tokmo-v3/ops/quant-bounded-canary",
                canonical_family,
                canonical_family / campaign_id,
                canonical_family / "sessions" / "foreign",
                canonical_family / "sessions" / campaign_id / "nested",
                canonical_family.parent
                / "quant-g2-preview-sibling"
                / "sessions"
                / campaign_id,
            )
            for path in refused:
                with pytest.raises(ValueError, match="boundary"):
                    MeteredCampaign(
                        path,
                        campaign_id=campaign_id,
                        sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
                        identities={"KR": "a" * 64, "US": "b" * 64},
                    )
            symlink = canonical_family / "sessions" / ("quant-g2-session-" + "b" * 24)
            symlink.parent.mkdir(parents=True, exist_ok=True)
            symlink.symlink_to(allowed, target_is_directory=True)
            with pytest.raises(ValueError, match="boundary"):
                MeteredCampaign(
                    symlink,
                    campaign_id=symlink.name,
                    sessions={"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
                    identities={"KR": "a" * 64, "US": "b" * 64},
                )

    # Decisive offline production vertical: real builders, live adapters, parsers and report store.
    from types import SimpleNamespace

    from tools.run_quant_canary_once import build_production_runner

    wire = []

    class Reply:
        status = code = 200
        msg = "OK"
        headers = Message()

        def __init__(self, body):
            self.body = body
            self.url = "https://openapi.tossinvest.com/"

        def read(self, size):
            return self.body[:size]

        def info(self):
            return self.headers

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class Handler(urllib.request.HTTPSHandler):
        def __init__(self, auth=False):
            super().__init__()
            self.auth = auth

        def https_open(self, request):
            wire.append(request.full_url)
            if self.auth:
                body = {"access_token": "SYNTHETIC-TOKEN", "expires_in": 3600}
            else:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(request.full_url).query
                )
                market = query.get("marketCountry", [""])[0]
                symbol = "005930" if market == "KR" else "AAPL"
                if "/rankings" in request.full_url:
                    currency = "KRW" if market == "KR" else "USD"
                    body = {
                        "rankedAt": "2026-08-25T12:00:00+09:00",
                        "rankings": [
                            {
                                "rank": 1,
                                "symbol": symbol,
                                "currency": currency,
                                "price": {
                                    "lastPrice": "100",
                                    "basePrice": "99",
                                    "changeRate": "0.01",
                                },
                                "tradingVolume": "10",
                                "tradingAmount": "1000",
                            }
                        ],
                    }
                elif "/stocks" in request.full_url:
                    symbol = query["symbols"][0]
                    currency = "KRW" if symbol == "005930" else "USD"
                    body = [
                        {
                            "symbol": symbol,
                            "name": "Public",
                            "englishName": "Public",
                            "isinCode": "XX1",
                            "market": "KOSPI" if currency == "KRW" else "NASDAQ",
                            "securityType": "STOCK",
                            "isCommonShare": True,
                            "status": "ACTIVE",
                            "currency": currency,
                            "sharesOutstanding": "1000",
                        }
                    ]
                else:
                    currency = "KRW" if query.get("symbol") == ["005930"] else "USD"
                    body = {
                        "candles": [
                            {
                                "timestamp": "2026-08-24T12:00:00+09:00",
                                "openPrice": "100",
                                "highPrice": "101",
                                "lowPrice": "99",
                                "closePrice": "100",
                                "volume": "10",
                                "currency": currency,
                            }
                        ]
                    }
            return Reply(json.dumps(body if self.auth else {"result": body}).encode())

    with tempfile.TemporaryDirectory() as outer:
        context = {
            "config": SimpleNamespace(client_id="id", client_secret="secret"),
            "sessions": {"KR": date(2026, 8, 24), "US": date(2026, 8, 24)},
            "holdings": None,
            "pension": None,
            "campaign_root": Path(outer),
        }
        result = build_production_runner(
            auth_handler=Handler(True),
            reads_handler=Handler(),
            clock=lambda: datetime(2026, 8, 25, 3, tzinfo=UTC),
            _test_context=context,
        )()
        assert result["campaign_status"] == "SUCCEEDED" and result["total_starts"] == 7
        assert (
            len(tuple(Path(outer).rglob("*.report.json"))) == 2
            and len(tuple(Path(outer).rglob("*.receipt.json"))) == 2
        )
        assert sum("adjusted=true" in url for url in wire) == 2
        wire_before = tuple(wire)
        campaign_ledgers = tuple(
            (Path(outer) / "sessions").rglob("campaign-ledger.json")
        )
        assert len(campaign_ledgers) == 1
        ledger_before = campaign_ledgers[0].read_bytes()
        family_before = (Path(outer) / "family-ledger.json").read_bytes()
        assert len(json.loads(family_before)["starts"]) == 7
        replay = build_production_runner(
            auth_handler=Handler(True),
            reads_handler=Handler(),
            clock=lambda: datetime(2026, 8, 25, 3, tzinfo=UTC),
            _test_context=context,
        )()
        assert replay["campaign_status"] == "REPLAYED"
        assert all(
            product["replay_zero"] is True for product in replay["products"].values()
        )
        assert tuple(wire) == wire_before
        assert campaign_ledgers[0].read_bytes() == ledger_before
        assert (Path(outer) / "family-ledger.json").read_bytes() == family_before
