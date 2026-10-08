from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from tokmo.facts.quant_membership import (
    SECURITY_NAME_POLICY_VERSION,
    classify_security_name,
    parse_nasdaqlisted,
    parse_otherlisted,
    parse_reconstructed_sic_snapshot,
    parse_sec_issuer,
    parse_sec_registry,
)
from tokmo.values.official_membership_observation import (
    ListingVenueV1,
    OfficialSicSnapshotV1,
    SecurityClassV1,
)


FIXTURES = Path(__file__).parent / "fixtures" / "quant_membership"
NOW = datetime(2026, 8, 13, 1, 30, tzinfo=timezone.utc)
# The fixture footer reads 08/13/2026 01:30 as a bare Eastern wall clock;
# 2026-08-13 is EDT (UTC-4), so the canonical instant is 05:30Z.
EXPECTED_DIRECTORY_CREATION_TIME = datetime(
    2026,
    8,
    13,
    5,
    30,
    tzinfo=timezone.utc,
)
# Exact footer evidence from the durable official capture of 2026-08-13
# (campaign starts 4 and 5).  Private response bodies remain outside Git.
CAPTURED_NASDAQLISTED_FOOTER = "File Creation Time: 0813202621:31|||||||"
CAPTURED_OTHERLISTED_FOOTER = "File Creation Time: 0813202621:31||||||"
# 2026-08-13 21:31 ET (EDT, UTC-4) is 2026-08-14 01:31Z.
CAPTURED_DIRECTORY_CREATION_TIME = datetime(2026, 8, 14, 1, 31, tzinfo=timezone.utc)
DENY_TOKENS = (
    "ADR",
    "ADS",
    "AMERICAN DEPOSITARY",
    "DEPOSITARY",
    "PREFERRED",
    "WARRANT",
    "RIGHT",
    "UNIT",
    "NOTE",
    "DEBENTURE",
    "BOND",
    "FUND",
    "ETF",
    "ETN",
    "TRUST",
    "ACQUISITION",
    "BLANK CHECK",
    "SPAC",
    "WHEN ISSUED",
)


def test_exact_headers_footer_and_supported_rows_parse():
    nasdaq_rows = parse_nasdaqlisted(
        (FIXTURES / "nasdaqlisted_sample.txt").read_bytes(),
        observed_at=NOW,
        expected_symbols=("AAPL",),
    )
    nyse_rows = parse_otherlisted(
        (FIXTURES / "otherlisted_sample.txt").read_bytes(),
        observed_at=NOW,
        expected_symbols=("IBM",),
    )

    assert [row.symbol for row in nasdaq_rows] == ["AAPL"]
    assert nasdaq_rows[0].venue is ListingVenueV1.NASDAQ
    assert nasdaq_rows[0].directory_creation_time == EXPECTED_DIRECTORY_CREATION_TIME
    assert [row.symbol for row in nyse_rows] == ["IBM"]
    assert nyse_rows[0].venue is ListingVenueV1.NYSE
    assert nyse_rows[0].directory_creation_time == EXPECTED_DIRECTORY_CREATION_TIME

    bad_header = (
        FIXTURES / "nasdaqlisted_sample.txt"
    ).read_text().replace("Symbol|Security Name", "Ticker|Security Name", 1)
    with pytest.raises(ValueError, match="header"):
        parse_nasdaqlisted(
            bad_header.encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("AAPL",),
        )
    bad_footer = (
        FIXTURES / "otherlisted_sample.txt"
    ).read_text().replace("File Creation Time:", "Creation Time:", 1)
    with pytest.raises(ValueError, match="footer"):
        parse_otherlisted(
            bad_footer.encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("IBM",),
        )
    official_footer_colon = (
        FIXTURES / "nasdaqlisted_sample.txt"
    ).read_text().replace("081320260130|||||||", "0813202601:30|||||||", 1)
    colon_rows = parse_nasdaqlisted(
        official_footer_colon.encode("utf-8"),
        observed_at=NOW,
        expected_symbols=("AAPL",),
    )
    assert [row.symbol for row in colon_rows] == ["AAPL"]
    assert colon_rows[0].directory_creation_time == EXPECTED_DIRECTORY_CREATION_TIME
    bad_footer_trailing = (
        FIXTURES / "otherlisted_sample.txt"
    ).read_text().replace("081320260130|||||||", "081320260130|x||||||", 1)
    with pytest.raises(ValueError, match="footer"):
        parse_otherlisted(
            bad_footer_trailing.encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("IBM",),
        )
    bad_market_token = (
        FIXTURES / "nasdaqlisted_sample.txt"
    ).read_text().replace("|Q|N|N|100|N|N", "|X|N|N|100|N|N", 1)
    with pytest.raises(ValueError, match="venue_token"):
        parse_nasdaqlisted(
            bad_market_token.encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("AAPL",),
        )
    bad_exchange_token = (
        FIXTURES / "otherlisted_sample.txt"
    ).read_text().replace("|N|IBM|N|100|N|IBM", "|X|IBM|N|100|N|IBM", 1)
    with pytest.raises(ValueError, match="venue_token_unknown"):
        parse_otherlisted(
            bad_exchange_token.encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("IBM",),
        )
    with pytest.raises(ValueError, match="venue_token_unsupported"):
        parse_otherlisted(
            (FIXTURES / "otherlisted_sample.txt").read_bytes(),
            observed_at=NOW,
            expected_symbols=("ZZZ",),
        )


def _nasdaq_body(footer_timestamp, *, padding="|||||||"):
    return (
        FIXTURES / "nasdaqlisted_sample.txt"
    ).read_text().replace(
        "File Creation Time: 081320260130|||||||",
        f"File Creation Time: {footer_timestamp}{padding}",
        1,
    ).encode("utf-8")


def _otherlisted_body(footer_timestamp, *, padding="|||||||"):
    return (
        FIXTURES / "otherlisted_sample.txt"
    ).read_text().replace(
        "File Creation Time: 081320260130|||||||",
        f"File Creation Time: {footer_timestamp}{padding}",
        1,
    ).encode("utf-8")


def _body_with_footer(fixture_name, footer_line):
    return (FIXTURES / fixture_name).read_text().replace(
        "File Creation Time: 081320260130|||||||", footer_line, 1,
    ).encode("utf-8")


def test_both_official_footer_clock_forms_normalize_to_one_utc_instant():
    legacy = parse_nasdaqlisted(
        _nasdaq_body("081320260130"), observed_at=NOW,
        expected_symbols=("AAPL",),
    )
    current = parse_nasdaqlisted(
        _nasdaq_body("0813202601:30"), observed_at=NOW,
        expected_symbols=("AAPL",),
    )
    assert legacy[0].directory_creation_time == current[0].directory_creation_time
    assert current[0].directory_creation_time == EXPECTED_DIRECTORY_CREATION_TIME
    assert current[0].directory_creation_time.tzinfo is timezone.utc
    other_legacy = parse_otherlisted(
        _otherlisted_body("081320260130"), observed_at=NOW,
        expected_symbols=("IBM",),
    )
    other_current = parse_otherlisted(
        _otherlisted_body("0813202601:30"), observed_at=NOW,
        expected_symbols=("IBM",),
    )
    assert (other_legacy[0].directory_creation_time
            == other_current[0].directory_creation_time
            == EXPECTED_DIRECTORY_CREATION_TIME)


def test_official_footer_clock_accepts_exact_midnight_and_last_minute():
    for timestamp, expected in (
        ("081320260000", datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)),
        ("0813202600:00", datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)),
        ("081320262359", datetime(2026, 8, 14, 3, 59, tzinfo=timezone.utc)),
        ("0813202623:59", datetime(2026, 8, 14, 3, 59, tzinfo=timezone.utc)),
        ("122920261501", datetime(2026, 12, 29, 20, 1, tzinfo=timezone.utc)),
        ("1229202615:01", datetime(2026, 12, 29, 20, 1, tzinfo=timezone.utc)),
    ):
        rows = parse_nasdaqlisted(
            _nasdaq_body(timestamp), observed_at=NOW,
            expected_symbols=("AAPL",),
        )
        assert rows[0].directory_creation_time == expected


def test_directory_footer_wall_clock_is_eastern_and_never_stamped_naive_utc():
    """The footer clock is U.S. Eastern; the stored instant is its UTC image."""
    summer = parse_nasdaqlisted(
        _nasdaq_body("0813202621:31"), observed_at=NOW,
        expected_symbols=("AAPL",),
    )[0].directory_creation_time
    assert summer == datetime(2026, 8, 14, 1, 31, tzinfo=timezone.utc)
    assert summer == CAPTURED_DIRECTORY_CREATION_TIME
    # Naive-as-UTC would have produced 2026-08-13 21:31Z; the offset is real.
    assert summer != datetime(2026, 8, 13, 21, 31, tzinfo=timezone.utc)
    assert summer - datetime(2026, 8, 13, 21, 31, tzinfo=timezone.utc) == timedelta(
        hours=4)

    winter = parse_nasdaqlisted(
        _nasdaq_body("0113202621:31"), observed_at=NOW,
        expected_symbols=("AAPL",),
    )[0].directory_creation_time
    assert winter == datetime(2026, 1, 14, 2, 31, tzinfo=timezone.utc)
    assert winter - datetime(2026, 1, 13, 21, 31, tzinfo=timezone.utc) == timedelta(
        hours=5)


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    (
        # 2026 spring forward: 02:00 ET on 2026-03-08.
        ("0308202601:59", datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)),
        ("030820260159", datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)),
        ("0308202603:00", datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)),
        ("030820260300", datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)),
        # 2026 fall back: 02:00 ET on 2026-11-01.
        ("1101202600:30", datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)),
        ("110120260030", datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)),
        ("1101202602:00", datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)),
        ("110120260200", datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)),
        # The ambiguous repeated hour resolves deterministically to the first
        # (daylight) occurrence, i.e. fold=0.
        ("1101202601:30", datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)),
        ("110120260130", datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)),
    ),
)
def test_directory_footer_covers_both_dst_transitions(timestamp, expected):
    rows = parse_nasdaqlisted(
        _nasdaq_body(timestamp), observed_at=NOW, expected_symbols=("AAPL",),
    )
    assert rows[0].directory_creation_time == expected
    assert rows[0].directory_creation_time.tzinfo is timezone.utc


def test_captured_official_footers_parse_to_the_evidenced_utc_instant():
    """Regression pinned to the durable 2026-08-13 capture, without its body.

    The two exact footer lines are pinned here, so this runs in CI with no
    runtime-store dependency or false claim that a constant validates itself.
    """
    # The captured nasdaqlisted footer pads to the full header width; the
    # captured otherlisted footer carries exactly one delimiter fewer.
    assert len(CAPTURED_NASDAQLISTED_FOOTER.split("|")) == 8
    assert len(CAPTURED_OTHERLISTED_FOOTER.split("|")) == 7

    nasdaq_rows = parse_nasdaqlisted(
        _body_with_footer("nasdaqlisted_sample.txt", CAPTURED_NASDAQLISTED_FOOTER),
        observed_at=NOW, expected_symbols=("AAPL",),
    )
    other_rows = parse_otherlisted(
        _body_with_footer("otherlisted_sample.txt", CAPTURED_OTHERLISTED_FOOTER),
        observed_at=NOW, expected_symbols=("IBM",),
    )
    assert nasdaq_rows[0].directory_creation_time == CAPTURED_DIRECTORY_CREATION_TIME
    assert other_rows[0].directory_creation_time == CAPTURED_DIRECTORY_CREATION_TIME
    # The pre-repair contract failed both of these on the footer alone.
    assert nasdaq_rows[0].venue is ListingVenueV1.NASDAQ
    assert other_rows[0].venue is ListingVenueV1.NYSE


@pytest.mark.parametrize(
    "timestamp",
    (
        "08132026013:0",
        "081320260:130",
        "08132026:0130",
        "0813202601:3",
        "0813202601:300",
        "081320260 :30",
        "08:1320260130",
        "0813202601:30:",
        ":081320260130",
        "081320260130:",
        "08:132026:0130",
        "08:13202601:30",
        "0813202601:3O",
        "0813202601-30",
        "0813202601.30",
        "0813202601 30",
        "08132026013",
        "0813202601300",
        "0813202601:30 ",
        " 0813202601:30",
        "0813202601:30x",
        "０８１３２０２６０１:３０",
        "١٣٠٨٢٠٢٦٠١:٣٠",
        "131320260130",
        "1332202601:30",
        "0813202624:00",
        "0813202601:60",
        "081320262400",
        "081320260160",
        "0230202601:30",
        "",
    ),
)
def test_official_footer_clock_rejects_every_other_shape(timestamp):
    with pytest.raises(ValueError, match="footer"):
        parse_nasdaqlisted(
            _nasdaq_body(timestamp), observed_at=NOW,
            expected_symbols=("AAPL",),
        )


def test_official_otherlisted_admits_m_only_as_a_non_target_venue():
    # Built on the captured otherlisted footer shape, so the "M" vocabulary and
    # the seven-field footer are proven together against the same evidence.
    with_m = _body_with_footer(
        "otherlisted_sample.txt", CAPTURED_OTHERLISTED_FOOTER,
    ).replace(
        b"ZZZ|Unsupported Acquisition Corp. Warrant|A|ZZZ|N|100|N|ZZZ",
        b"MMM|Some Company Common Stock|M|MMM|N|100|N|MMM",
        1,
    )
    # A valid non-target "M" row no longer fails the whole body...
    rows = parse_otherlisted(with_m, observed_at=NOW, expected_symbols=("IBM",))
    assert [row.symbol for row in rows] == ["IBM"]
    assert rows[0].venue is ListingVenueV1.NYSE
    assert rows[0].directory_creation_time == CAPTURED_DIRECTORY_CREATION_TIME
    # ...but "M" is still not a supported venue for a cohort target.
    with pytest.raises(ValueError, match="venue_token_unsupported"):
        parse_otherlisted(with_m, observed_at=NOW, expected_symbols=("MMM",))
    # and a genuinely unknown token still fails the whole body.
    with pytest.raises(ValueError, match="venue_token_unknown"):
        parse_otherlisted(
            with_m.replace(b"|M|MMM|", b"|Q|MMM|", 1),
            observed_at=NOW, expected_symbols=("IBM",),
        )


def test_official_footer_padding_admits_only_header_width_or_one_fewer():
    # Both captured shapes: full header width (8 fields) and one fewer (7).
    for padding in ("|||||||", "||||||"):
        rows = parse_otherlisted(
            _otherlisted_body("0813202601:30", padding=padding),
            observed_at=NOW, expected_symbols=("IBM",),
        )
        assert rows[0].directory_creation_time == EXPECTED_DIRECTORY_CREATION_TIME
        nasdaq_rows = parse_nasdaqlisted(
            _nasdaq_body("0813202601:30", padding=padding),
            observed_at=NOW, expected_symbols=("AAPL",),
        )
        assert nasdaq_rows[0].directory_creation_time == (
            EXPECTED_DIRECTORY_CREATION_TIME)
    # Degenerate padding is not an official form and is no longer admitted.
    for padding in ("|||||", "||||", "|||", "||", "|", ""):
        for builder, parser, symbol in (
            (_otherlisted_body, parse_otherlisted, "IBM"),
            (_nasdaq_body, parse_nasdaqlisted, "AAPL"),
        ):
            with pytest.raises(ValueError, match="footer"):
                parser(
                    builder("0813202601:30", padding=padding),
                    observed_at=NOW, expected_symbols=(symbol,),
                )
    # Wider than the header, or any non-empty trailing field, still fails.
    with pytest.raises(ValueError, match="footer"):
        parse_otherlisted(
            _otherlisted_body("0813202601:30", padding="||||||||"),
            observed_at=NOW, expected_symbols=("IBM",),
        )
    for padding in ("|x||||||", "||||||x", "|||||| |"):
        with pytest.raises(ValueError, match="footer"):
            parse_otherlisted(
                _otherlisted_body("0813202601:30", padding=padding),
                observed_at=NOW, expected_symbols=("IBM",),
            )


@pytest.mark.parametrize(
    "name",
    (
        "Apple Inc. Common Stock",
        "A Company Common Share",
        "A Company Common Shares",
        "A Company Ordinary Share",
        "A Company Ordinary Shares",
    ),
)
def test_security_name_classifier_accepts_supported_common_equity(name):
    assert classify_security_name(
        name,
        policy_version=SECURITY_NAME_POLICY_VERSION,
    ) is SecurityClassV1.COMMON_EQUITY


@pytest.mark.parametrize("deny_token", DENY_TOKENS)
def test_security_name_classifier_rejects_all_closed_deny_tokens(deny_token):
    assert classify_security_name(
        f"Example {deny_token} Common Stock",
        policy_version=SECURITY_NAME_POLICY_VERSION,
    ) is SecurityClassV1.UNSUPPORTED


def test_security_name_classifier_rejects_punctuation_bounded_deny_tokens_and_bad_policy():
    assert classify_security_name(
        "Example Common Stock (ADR)",
        policy_version=SECURITY_NAME_POLICY_VERSION,
    ) is SecurityClassV1.UNSUPPORTED
    with pytest.raises(ValueError, match="policy"):
        classify_security_name(
            "Example Common Stock",
            policy_version="mutated-policy-v2",
        )


def test_duplicate_missing_extra_and_registry_contract_reject():
    duplicate = (
        FIXTURES / "nasdaqlisted_sample.txt"
    ).read_text().replace(
        "File Creation Time: 081320260130|||||||",
        "AAPL|Apple Inc. Common Stock|Q|N|N|100|N|N\n"
        "File Creation Time: 081320260130|||||||",
        1,
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_nasdaqlisted(
            duplicate.encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("AAPL",),
        )
    assert parse_otherlisted(
        (FIXTURES / "otherlisted_sample.txt").read_bytes(),
        observed_at=NOW,
        expected_symbols=("MSFT",),
    ) == ()
    extra = json.loads((FIXTURES / "sec_registry_sample.json").read_text())
    extra["data"].append(["320193", "Apple Inc.", "AAPL", "Nasdaq"])
    with pytest.raises(ValueError, match="registry_duplicate"):
        parse_sec_registry(
            json.dumps(extra).encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("AAPL",),
        )
    bad_exchange = json.loads((FIXTURES / "sec_registry_sample.json").read_text())
    bad_exchange["data"][0][-1] = "NasdaqGM"
    with pytest.raises(ValueError, match="registry_exchange"):
        parse_sec_registry(
            json.dumps(bad_exchange).encode("utf-8"),
            observed_at=NOW,
            expected_symbols=("AAPL",),
        )


def test_submissions_and_reconstructed_sic_snapshot_are_fail_closed():
    issuer = parse_sec_issuer(
        (FIXTURES / "sec_submissions_aapl.json").read_bytes(),
        observed_at=NOW,
        expected_symbol="AAPL",
        expected_cik="0000320193",
        expected_exchange="NASDAQ",
    )
    snapshot = parse_reconstructed_sic_snapshot(
        (FIXTURES / "sec_sic_reconstructed.json").read_bytes()
    )

    assert issuer.sic_code == "3571"
    assert isinstance(snapshot, OfficialSicSnapshotV1)
    assert snapshot.codes == ("3571", "5990", "6000", "6799", "7000")

    bad_exchange = json.loads((FIXTURES / "sec_submissions_aapl.json").read_text())
    bad_exchange["exchanges"] = ["NYSE"]
    with pytest.raises(ValueError, match="issuer_exchange"):
        parse_sec_issuer(
            json.dumps(bad_exchange).encode("utf-8"),
            observed_at=NOW,
            expected_symbol="AAPL",
            expected_cik="0000320193",
            expected_exchange="NASDAQ",
        )
    with pytest.raises(ValueError, match="issuer_cik"):
        parse_sec_issuer(
            (FIXTURES / "sec_submissions_aapl.json").read_bytes(),
            observed_at=NOW,
            expected_symbol="AAPL",
            expected_cik="0000320000",
            expected_exchange="NASDAQ",
        )
    bad_ticker = json.loads((FIXTURES / "sec_submissions_aapl.json").read_text())
    bad_ticker["tickers"] = ["MSFT"]
    with pytest.raises(ValueError, match="issuer_exchange"):
        parse_sec_issuer(
            json.dumps(bad_ticker).encode("utf-8"),
            observed_at=NOW,
            expected_symbol="AAPL",
            expected_cik="0000320193",
            expected_exchange="NASDAQ",
        )
    bad_sic = json.loads((FIXTURES / "sec_submissions_aapl.json").read_text())
    bad_sic["sic"] = "357"
    with pytest.raises(ValueError, match="issuer_sic"):
        parse_sec_issuer(
            json.dumps(bad_sic).encode("utf-8"),
            observed_at=NOW,
            expected_symbol="AAPL",
            expected_cik="0000320193",
            expected_exchange="NASDAQ",
        )
    bad_snapshot = json.loads((FIXTURES / "sec_sic_reconstructed.json").read_text())
    bad_snapshot["codes"].append("357A")
    with pytest.raises(ValueError, match="sic_snapshot_codes"):
        parse_reconstructed_sic_snapshot(
            json.dumps(bad_snapshot).encode("utf-8"),
        )


def test_live_shapes_normalize_unpadded_cik_and_title_case_nasdaq():
    registry_body = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
    }
    registry_rows = parse_sec_registry(
        json.dumps(registry_body).encode(),
        observed_at=NOW,
        expected_symbols=("AAPL",),
    )
    issuer_body = {
        "cik": 320193,
        "sic": "3571",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
    }
    issuer = parse_sec_issuer(
        json.dumps(issuer_body).encode(),
        observed_at=NOW,
        expected_symbol="AAPL",
        expected_cik="0000320193",
        expected_exchange="NASDAQ",
    )

    assert registry_rows[0].cik == "0000320193"
    assert registry_rows[0].exchange_token == "NASDAQ"
    assert issuer.cik == "0000320193"
    assert issuer.exchange_token == "NASDAQ"


def test_submissions_ignores_non_target_unsupported_share_class_pairs():
    issuer_body = {
        "cik": 320193,
        "sic": "3571",
        "tickers": ["AAPL", "AAPL.W", None],
        "exchanges": ["Nasdaq", "CBOE", None],
    }
    issuer = parse_sec_issuer(
        json.dumps(issuer_body).encode(), observed_at=NOW,
        expected_symbol="AAPL", expected_cik="0000320193",
        expected_exchange="NASDAQ",
    )
    assert issuer.symbol == "AAPL"
    assert issuer.exchange_token == "NASDAQ"


@pytest.mark.parametrize("bad_cik", (True, "+320193", " 320193", "１２３", "", 12345678901))
def test_cik_normalization_rejects_noncanonical_source_values(bad_cik):
    body = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[bad_cik, "Apple Inc.", "AAPL", "Nasdaq"]],
    }
    with pytest.raises(ValueError, match="registry_cik"):
        parse_sec_registry(
            json.dumps(body).encode(), observed_at=NOW, expected_symbols=("AAPL",)
        )


def test_mixed_venue_directory_parsers_return_only_target_intersection():
    nasdaq = parse_nasdaqlisted(
        (FIXTURES / "nasdaqlisted_sample.txt").read_bytes(),
        observed_at=NOW,
        expected_symbols=("AAPL", "IBM"),
    )
    nyse = parse_otherlisted(
        (FIXTURES / "otherlisted_sample.txt").read_bytes(),
        observed_at=NOW,
        expected_symbols=("AAPL", "IBM"),
    )
    assert tuple(row.symbol for row in nasdaq) == ("AAPL",)
    assert tuple(row.symbol for row in nyse) == ("IBM",)


def test_docs_manifests_quote_official_sources_and_separate_reconstructed_choices():
    required_keys = {
        "classification",
        "official_url",
        "retrieved_at_utc",
        "quote_excerpt",
        "quote_sha256",
        "parser_fields_tokens",
        "access_fair_use_note",
        "reconstruction_procedure",
        "fixture_relpath",
        "fixture_sha256",
    }
    for manifest_path in sorted((FIXTURES / "docs").glob("*.json")):
        manifest = json.loads(manifest_path.read_text())
        assert required_keys.issubset(manifest)
        assert manifest["classification"] == "RECONSTRUCTED_CONTRACT"
        assert "payload_length" not in manifest
        assert "payload_sha256" not in manifest
        assert isinstance(manifest["parser_fields_tokens"], dict)
        assert manifest["parser_fields_tokens"]
        assert isinstance(manifest["access_fair_use_note"], str)
        assert manifest["access_fair_use_note"]
        assert "bounded synthetic" not in manifest["quote_excerpt"]
        assert "membership testing" not in manifest["quote_excerpt"]
        assert hashlib.sha256(
            manifest["quote_excerpt"].encode("utf-8")
        ).hexdigest() == manifest["quote_sha256"]
        fixture_path = FIXTURES / manifest["fixture_relpath"]
        assert hashlib.sha256(
            fixture_path.read_bytes()
        ).hexdigest() == manifest["fixture_sha256"]

    sec_submissions = json.loads(
        (FIXTURES / "docs" / "sec_submissions.json").read_text()
    )
    assert sec_submissions["quote_excerpt"].startswith("Submissions by company")
    assert sec_submissions["parser_fields_tokens"] == {
        "request_path_template": "/submissions/CIK##########.json",
        "body_fields": ["cik", "sic", "tickers", "exchanges"],
    }
    assert sec_submissions["reconstructed_contract_choices"] == {
        "body_cik_ascii_digits": 10,
        "allowed_exchange_tokens": ["NASDAQ", "NYSE"],
    }


# --- SEC share-class ticker normalization (plan 2026-08-18 v0.1) -------------
# SEC registry/submissions publish share-class tickers in hyphen form
# (BRK-B) while the cohort and nasdaqtrader directories use the dot form
# (BRK.B).  The SEC boundary alone normalizes.

def test_registry_matches_hyphen_form_for_dot_cohort_symbol():
    body = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
        [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
        [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
        [1045810, "NVIDIA CORP", "NVDA", "Nasdaq"],
        [80424, "PEPSICO INC", "PEP", "Nasdaq"],
    ]}).encode("utf-8")
    rows = parse_sec_registry(
        body, observed_at=NOW,
        expected_symbols=("BRK.B", "GOOGL", "NVDA", "PEP"))
    assert [row.symbol for row in rows] == ["BRK.B", "GOOGL", "NVDA", "PEP"]
    berkshire = rows[0]
    assert berkshire.cik == "0001067983"
    assert berkshire.exchange_token == "NYSE"
    # evidence bytes preserved on the raw row, not the normalized symbol
    row_json = json.dumps([1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
                          separators=(",", ":"), ensure_ascii=True)
    assert berkshire.row_sha256 == hashlib.sha256(
        row_json.encode("utf-8")).hexdigest()


def test_issuer_matches_hyphen_ticker_pair_for_dot_cohort_symbol():
    body = json.dumps({
        "cik": 1067983, "sic": "6021",
        "tickers": ["BRK-A", "BRK-B"], "exchanges": ["NYSE", "NYSE"],
    }).encode("utf-8")
    issuer = parse_sec_issuer(
        body, observed_at=NOW, expected_symbol="BRK.B",
        expected_cik="0001067983", expected_exchange="NYSE")
    assert issuer.symbol == "BRK.B"
    assert issuer.exchange_token == "NYSE"


def test_registry_rejects_dot_and_hyphen_duplicates():
    body = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
        [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
        [1067983, "BERKSHIRE HATHAWAWAY INC", "BRK.B", "NYSE"],
    ]}).encode("utf-8")
    with pytest.raises(ValueError, match="registry_duplicate"):
        parse_sec_registry(body, observed_at=NOW,
                           expected_symbols=("BRK.B",))


def test_registry_plain_symbols_unchanged():
    body = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
        [320193, "Apple Inc.", "AAPL", "Nasdaq"],
        [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
    ]}).encode("utf-8")
    rows = parse_sec_registry(body, observed_at=NOW,
                              expected_symbols=("AAPL", "MSFT"))
    assert [row.symbol for row in rows] == ["AAPL", "MSFT"]


def test_issuer_exchange_conflict_still_rejected():
    body = json.dumps({
        "cik": 1067983, "sic": "6021",
        "tickers": ["BRK-B"], "exchanges": ["Nasdaq"],
    }).encode("utf-8")
    with pytest.raises(ValueError, match="issuer_exchange"):
        parse_sec_issuer(
            body, observed_at=NOW, expected_symbol="BRK.B",
            expected_cik="0001067983", expected_exchange="NYSE")


def test_nasdaqtrader_parsers_do_not_use_sec_ticker_form():
    import inspect
    import tokmo.facts.quant_membership as module
    for parser_name in ("parse_nasdaqlisted", "parse_otherlisted"):
        source = inspect.getsource(getattr(module, parser_name))
        assert "sec_ticker_form" not in source, parser_name


def test_registry_rejects_dot_and_hyphen_conflict_in_expected_symbols():
    body = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
        [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
    ]}).encode("utf-8")
    with pytest.raises(ValueError, match="expected_symbols_sec_conflict"):
        parse_sec_registry(body, observed_at=NOW,
                           expected_symbols=("BRK.B", "BRK-B"))
