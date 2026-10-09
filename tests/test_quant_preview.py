import json
from datetime import date
from hashlib import sha256


def _canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def _write_terminal(
    root, market, session, identity=None, *, outcome="SUCCEEDED_SILENT"
):
    leaf = root / "market_discovery" / market.lower()
    leaf.mkdir(parents=True, exist_ok=True)
    identity = identity or f"{market.lower()}-{session}"
    report_identity = (
        {"market": market, "completed_session_date": session}
        if "-" in identity
        else {
            "product_kind": "MARKET_DISCOVERY",
            "market": market,
            "completed_session_date": session,
            "completed_iso_week": None,
            "profile_revision": "quant-profile-v1",
            "peer_registry_revision": None,
            "report_revision": "quant-report-v1",
            "identity_sha256": identity,
        }
    )
    report = {
        "identity": report_identity,
        "cards": [],
    }
    raw = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    (leaf / f"{identity}.report.json").write_bytes(raw)
    (leaf / f"{identity}.receipt.json").write_text(
        json.dumps(
            {
                "identity": report["identity"],
                "outcome": outcome,
                "report_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            }
        )
    )


def _write_family_terminal(
    family, campaign_id, sessions, *, tamper=None, bound_markets=("KR", "US")
):
    campaign = family / "sessions" / campaign_id
    bindings = {}
    identities = {}
    for market, session in sessions.items():
        identity = ("a" if market == "KR" else "b") * 64
        identities[market] = identity
        if market in bound_markets:
            _write_terminal(campaign / "report-store", market, session, identity)
            base = (
                campaign
                / "report-store"
                / "market_discovery"
                / market.lower()
                / identity
            )
            report_raw = (base.with_suffix(".report.json")).read_bytes()
            receipt_raw = (base.with_suffix(".receipt.json")).read_bytes()
            bindings[market] = {
                "identity": identity,
                "report_sha256": sha256(report_raw).hexdigest(),
                "receipt_sha256": sha256(receipt_raw).hexdigest(),
            }
    manifest = {
        "campaign_id": campaign_id,
        "policy_revision": "quant-bounded-canary-policy-v1",
        "report_root": str((campaign / "report-store").resolve()),
        "sessions": sessions,
        "profile_revision": "quant-profile-v1",
        "report_revision": "quant-report-v1",
        "identities": identities,
        "terminal": True,
        "products": {
            market: (
                {
                    "outcome": "SUCCEEDED_SILENT",
                    "cards": 0,
                    "report_readback": True,
                    "receipt_readback": True,
                    "replay_zero": False,
                }
                if market in bound_markets
                else {
                    "outcome": "PENDING",
                    "cards": 0,
                    "report_readback": False,
                    "receipt_readback": False,
                    "replay_zero": True,
                }
            )
            for market in ("KR", "US")
        },
        "terminal_bindings": bindings,
    }
    binding_fields = {
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
    manifest["campaign_binding"] = sha256(_canonical(binding_fields)).hexdigest()
    if tamper == "terminal_binding":
        manifest["terminal_bindings"]["KR"]["receipt_sha256"] = "0" * 64
    elif tamper == "campaign_binding":
        manifest["campaign_binding"] = "0" * 64
    campaign.mkdir(parents=True, exist_ok=True)
    (campaign / "campaign-manifest.json").write_text(json.dumps(manifest))


def test_local_preview_counts_verified_unique_sessions_and_reports_registry_gates(
    tmp_path,
):
    from tokmo.programs.quant_preview import build_local_preview
    from tokmo.values.quant_discovery import load_peer_registry

    reports = tmp_path / "reports"
    for day in ("2026-08-21", "2026-08-22"):
        _write_terminal(reports, "KR", day)
    _write_terminal(reports, "US", "2026-08-21")
    _write_terminal(reports, "US", "2026-08-22", outcome="FAILED")

    family = tmp_path / "family"
    first_id = "quant-g2-session-" + "a" * 24
    duplicate_id = "quant-g2-session-" + "b" * 24
    tampered_id = "quant-g2-session-" + "c" * 24
    binding_tampered_id = "quant-g2-session-" + "d" * 24
    _write_family_terminal(family, first_id, {"KR": "2026-08-21", "US": "2026-08-21"})
    _write_family_terminal(
        family, duplicate_id, {"KR": "2026-08-22", "US": "2026-08-22"}
    )
    _write_family_terminal(
        family,
        tampered_id,
        {"KR": "2026-08-23", "US": "2026-08-23"},
        tamper="terminal_binding",
    )
    _write_family_terminal(
        family,
        binding_tampered_id,
        {"KR": "2026-08-24", "US": "2026-08-24"},
        tamper="campaign_binding",
    )
    family.mkdir(exist_ok=True)
    (family / "family-ledger.json").write_text(
        json.dumps(
            {
                "policy_revision": "quant-g2-preview-family-rev-20260831",
                "family_cap": 106,
                "sessions": {
                    "KR:2026-08-21": first_id,
                    "US:2026-08-21": first_id,
                    "KR:2026-08-22": duplicate_id,
                    "US:2026-08-22": duplicate_id,
                    "KR:2026-08-23": tampered_id,
                    "US:2026-08-23": tampered_id,
                    "KR:2026-08-24": binding_tampered_id,
                    "US:2026-08-24": binding_tampered_id,
                },
                "starts": [],
            }
        )
    )

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "group_id": "personal-kr-ref",
                        "instrument_type": "STOCK",
                        "last_reviewed": "2026-08-24",
                        "market": "KR",
                        "peer_symbols": ["PEER"],
                        "product_kind": "PERSONAL",
                        "provenance_note": "reviewed public issuer classification",
                        "reference_symbol": "REF",
                    }
                ],
                "reviewed_at": "2026-08-24",
                "revision": "reviewed-1",
                "schema": "quant-peer-registry-v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )

    result = build_local_preview(
        report_root=reports,
        registry=load_peer_registry(registry_path),
        personal_symbols=(("REF", "KR"), ("MISSING", "US")),
        pension_symbols=(("ETF", "KR"),),
        as_of=date(2026, 8, 25),
        target_completed_sessions=10,
        peer_review_max_age_days=90,
    )

    assert result["markets"] == {
        "KR": {"completed": 2, "remaining": 8, "ready": False},
        "US": {"completed": 1, "remaining": 9, "ready": False},
    }
    assert result["value_gate_ready"] is False
    assert result["peer_registry"] == {
        "revision": "reviewed-1",
        "personal_confirmed": 1,
        "personal_required": 2,
        "pension_confirmed": 0,
        "pension_required": 1,
        "missing": ["PERSONAL:US:MISSING", "PENSION:KR:ETF"],
        "ready": False,
    }

    aggregated = build_local_preview(
        report_root=family,
        legacy_report_root=reports,
        registry=load_peer_registry(registry_path),
        personal_symbols=(),
        pension_symbols=(),
        as_of=date(2026, 8, 25),
        target_completed_sessions=10,
        peer_review_max_age_days=90,
    )
    assert aggregated["markets"] == {
        "KR": {"completed": 2, "remaining": 8, "ready": False},
        "US": {"completed": 2, "remaining": 8, "ready": False},
    }

    ledger_path = family / "family-ledger.json"
    family_ledger = json.loads(ledger_path.read_text())
    family_ledger["starts"] = [
        {"seq": 2, "campaign_id": first_id, "operation": "TOKEN", "market": None}
    ]
    ledger_path.write_text(json.dumps(family_ledger))
    starts_tampered = build_local_preview(
        report_root=family,
        legacy_report_root=reports,
        registry=load_peer_registry(registry_path),
        personal_symbols=(),
        pension_symbols=(),
        as_of=date(2026, 8, 25),
        target_completed_sessions=10,
        peer_review_max_age_days=90,
    )
    assert starts_tampered["markets"] == {
        "KR": {"completed": 2, "remaining": 8, "ready": False},
        "US": {"completed": 1, "remaining": 9, "ready": False},
    }

    successor_family = tmp_path / "successor-family"
    old_id = "quant-g2-session-" + "1" * 24
    predecessor_id = "quant-g2-session-" + "2" * 24
    repair_commit = "e599953c72d79de94b6339fa228b960545b05188"
    successor_id = (
        "quant-g2-session-"
        + sha256(
            _canonical(
                {
                    "predecessor": predecessor_id,
                    "ordinal": 1,
                    "repair_commit": repair_commit,
                }
            )
        ).hexdigest()[:24]
    )
    _write_family_terminal(
        successor_family, old_id, {"KR": "2026-08-24", "US": "2026-08-24"}
    )
    _write_family_terminal(
        successor_family,
        predecessor_id,
        {"KR": "2026-08-25", "US": "2026-08-25"},
        bound_markets=("KR",),
    )
    _write_family_terminal(
        successor_family, successor_id, {"KR": "2026-08-25", "US": "2026-08-25"}
    )
    starts = []

    def append_starts(campaign_id, operations):
        for operation, market in operations:
            starts.append(
                {
                    "seq": len(starts) + 1,
                    "campaign_id": campaign_id,
                    "operation": operation,
                    "market": market,
                }
            )

    partial_shape = (
        ("TOKEN", None),
        ("RANKINGS", "KR"),
        ("STOCKS", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("STOCK_CANDLES", "KR"),
        ("RANKINGS", "US"),
        ("STOCKS", "US"),
    )
    append_starts(old_id, partial_shape)
    append_starts(predecessor_id, partial_shape)
    append_starts(
        successor_id, partial_shape + (("STOCK_CANDLES", "US"), ("STOCK_CANDLES", "US"))
    )
    (successor_family / "family-ledger.json").write_text(
        json.dumps(
            {
                "policy_revision": "quant-g2-preview-family-rev-20260831",
                "family_cap": 106,
                "sessions": {
                    "KR:2026-08-24": old_id,
                    "US:2026-08-24": old_id,
                    "KR:2026-08-25": predecessor_id,
                    "US:2026-08-25": predecessor_id,
                },
                "starts": starts,
                "revalidations": {
                    predecessor_id: {
                        "repair_commit": repair_commit,
                        "baseline_starts": 14,
                        "successors": [successor_id],
                    }
                },
            }
        )
    )
    successor_preview = build_local_preview(
        report_root=successor_family,
        registry=load_peer_registry(registry_path),
        personal_symbols=(),
        pension_symbols=(),
        as_of=date(2026, 8, 25),
        target_completed_sessions=2,
        peer_review_max_age_days=90,
    )
    assert successor_preview["markets"] == {
        "KR": {"completed": 2, "remaining": 0, "ready": True},
        "US": {"completed": 2, "remaining": 0, "ready": True},
    }

    # Later normal sessions must not count against the historical successor-only
    # repair budget. This production-shaped sequence exceeds baseline+45.
    from tokmo.programs.quant_preview import _family_ledger_valid

    later = json.loads((successor_family / "family-ledger.json").read_text())
    full_shape = partial_shape + (("STOCK_CANDLES", "US"), ("STOCK_CANDLES", "US"))
    for offset in range(5):
        campaign_id = "quant-g2-session-" + f"{offset + 10:024x}"
        day = date(2026, 8, 26 + offset).isoformat()
        later["sessions"].update({f"KR:{day}": campaign_id, f"US:{day}": campaign_id})
        for operation, market in full_shape:
            later["starts"].append(
                {
                    "seq": len(later["starts"]) + 1,
                    "campaign_id": campaign_id,
                    "operation": operation,
                    "market": market,
                }
            )
    assert len(later["starts"]) > 14 + 45
    assert _family_ledger_valid(later) is True
