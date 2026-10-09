"""G2 Delta SDS §4.3 / §8.1 acceptance A6-A15: the pure BLS ICS boundary.

The parser is a pure function over bytes: it opens no socket, holds no URL
literal, reads no clock and no environment. Every rejection in this file is
fail-closed — the caller may only translate it into
``UNAVAILABLE_RELEASE_SCHEDULE`` with FRED observations wire 0, never into a
guess, a fallback instant or a deduplicated pick.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.facts.bls_release_calendar import (
    BLS_TZID,
    IANA_ZONE,
    MAX_ICS_BYTES,
    BlsCalendarRejection,
    BlsCalendarV1,
    parse_bls_calendar_v1,
    select_release_instant_v1,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/bls/official/bls-ics.md"
SUMMARY = "Employment Situation"

VTIMEZONE = (
    "BEGIN:VTIMEZONE",
    "TZID:US-Eastern",
    "BEGIN:STANDARD",
    "DTSTART:20251102T020000",
    "TZOFFSETFROM:-0400",
    "TZOFFSETTO:-0500",
    "TZNAME:EST",
    "END:STANDARD",
    "BEGIN:DAYLIGHT",
    "DTSTART:20260308T020000",
    "TZOFFSETFROM:-0500",
    "TZOFFSETTO:-0400",
    "TZNAME:EDT",
    "END:DAYLIGHT",
    "END:VTIMEZONE",
)


def _calendar(*, events, timezone_line="X-WR-TIMEZONE:US-Eastern", vtimezone=VTIMEZONE):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    if timezone_line is not None:
        lines.append(timezone_line)
    lines.extend(vtimezone)
    for event in events:
        lines.extend(("BEGIN:VEVENT", *event, "END:VEVENT"))
    lines.extend(("END:VCALENDAR", ""))
    return "\r\n".join(lines).encode("utf-8")


def _event(*, summary=SUMMARY, dtstart="DTSTART;TZID=US-Eastern:20260807T083000"):
    return (f"UID:{summary}-{dtstart}@bls.gov", dtstart, f"SUMMARY:{summary}")


def stored_ics_region() -> bytes:
    """The body exactly as committed: canonical LF, never CRLF."""
    raw = FIXTURE.read_bytes()
    start = raw.index(b"<!--BLS-ICS-BEGIN-->\n") + len(b"<!--BLS-ICS-BEGIN-->\n")
    return raw[start : raw.index(b"<!--BLS-ICS-END-->")]


def pinned_ics_bytes() -> bytes:
    """The RFC 5545 wire form the parser is actually handed."""
    return stored_ics_region().replace(b"\n", b"\r\n")


# --- A6: envelope rejections -------------------------------------------------


def test_a6_an_oversized_or_undecodable_body_rejects():
    with pytest.raises(BlsCalendarRejection, match="body_too_large"):
        parse_bls_calendar_v1(b"x" * (MAX_ICS_BYTES + 1))
    with pytest.raises(BlsCalendarRejection, match="invalid_encoding"):
        parse_bls_calendar_v1(b"BEGIN:VCALENDAR\r\n\xff\xfe\r\nEND:VCALENDAR\r\n")
    with pytest.raises(BlsCalendarRejection, match="invalid_body"):
        parse_bls_calendar_v1("BEGIN:VCALENDAR")


def test_a6_the_body_size_bound_is_the_declared_half_megabyte():
    assert MAX_ICS_BYTES == 524_288


@pytest.mark.parametrize(
    "timezone_line",
    [
        None,
        "X-WR-TIMEZONE:US/Eastern",
        "X-WR-TIMEZONE:America/New_York",
        "X-WR-TIMEZONE:us-eastern",
        "X-WR-TIMEZONE:UTC",
    ],
)
def test_a6_an_absent_or_unlisted_calendar_timezone_rejects(timezone_line):
    with pytest.raises(BlsCalendarRejection, match="calendar_timezone"):
        parse_bls_calendar_v1(_calendar(events=[_event()], timezone_line=timezone_line))


def test_a6_an_absent_vtimezone_rejects():
    with pytest.raises(BlsCalendarRejection, match="vtimezone"):
        parse_bls_calendar_v1(_calendar(events=[_event()], vtimezone=()))


@pytest.mark.parametrize(
    "standard,daylight",
    [
        ("-0600", "-0400"),
        ("-0500", "-0500"),
        ("-0500", "+0400"),
        ("-0500", "-0430"),
    ],
)
def test_a6_a_vtimezone_declaring_other_offsets_rejects(standard, daylight):
    vtimezone = tuple(
        line.replace("TZOFFSETTO:-0500", f"TZOFFSETTO:{standard}").replace(
            "TZOFFSETTO:-0400", f"TZOFFSETTO:{daylight}"
        )
        for line in VTIMEZONE
    )
    with pytest.raises(BlsCalendarRejection, match="vtimezone"):
        parse_bls_calendar_v1(_calendar(events=[_event()], vtimezone=vtimezone))


def test_a6_a_vtimezone_missing_one_subcomponent_rejects():
    without_daylight = VTIMEZONE[: VTIMEZONE.index("BEGIN:DAYLIGHT")] + (
        "END:VTIMEZONE",
    )
    with pytest.raises(BlsCalendarRejection, match="vtimezone"):
        parse_bls_calendar_v1(_calendar(events=[_event()], vtimezone=without_daylight))


# --- A7: unfolding happens before property matching --------------------------


def test_a7_rfc5545_folding_is_unfolded_before_any_property_is_matched():
    folded = (
        "UID:folded@bls.gov",
        "DTSTART;TZID=US-Eastern:20260807T0830\r\n 00",
        "SUMMARY:Employment Si\r\n tuation",
    )
    calendar = parse_bls_calendar_v1(_calendar(events=[folded]))
    assert [event.summary for event in calendar.events] == [SUMMARY]
    assert select_release_instant_v1(
        calendar,
        summary=SUMMARY,
        local_date=date(2026, 8, 7),
    ) == datetime(2026, 8, 7, 12, 30, tzinfo=UTC)


def test_a7_a_tab_continuation_is_also_unfolded():
    folded = (
        "UID:tab@bls.gov",
        "DTSTART;TZID=US-Eastern:20260807T083000",
        "SUMMARY:Employment\r\n\t Situation",
    )
    calendar = parse_bls_calendar_v1(_calendar(events=[folded]))
    assert [event.summary for event in calendar.events] == [SUMMARY]


# --- A8: exact summary allowlist ---------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "Employment Situation (Annual)",
        "employment situation",
        "EMPLOYMENT SITUATION",
        "The Employment Situation",
        "Employment Situation ",
        " Employment Situation",
        "Employment  Situation",
    ],
)
def test_a8_only_an_exactly_equal_summary_matches(summary):
    calendar = parse_bls_calendar_v1(_calendar(events=[_event(summary=summary)]))
    with pytest.raises(BlsCalendarRejection, match="no_matching_event"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


# --- A9: DTSTART grammar -----------------------------------------------------


def test_a9_the_seconds_omitted_spelling_normalizes_to_zero_seconds():
    calendar = parse_bls_calendar_v1(
        _calendar(events=[_event(dtstart="DTSTART;TZID=US-Eastern:20260807T0830")])
    )
    assert select_release_instant_v1(
        calendar,
        summary=SUMMARY,
        local_date=date(2026, 8, 7),
    ) == datetime(2026, 8, 7, 12, 30, 0, tzinfo=UTC)


def test_a9_the_full_spelling_keeps_its_seconds():
    calendar = parse_bls_calendar_v1(
        _calendar(events=[_event(dtstart="DTSTART;TZID=US-Eastern:20260807T083045")])
    )
    assert select_release_instant_v1(
        calendar,
        summary=SUMMARY,
        local_date=date(2026, 8, 7),
    ) == datetime(2026, 8, 7, 12, 30, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    "dtstart",
    [
        "DTSTART:20260807T083000",  # floating, no TZID
        "DTSTART:20260807T123000Z",  # UTC form
        "DTSTART;TZID=US-Eastern:20260807T123000Z",  # TZID plus Z
        "DTSTART;VALUE=DATE:20260807",  # all-day
        "DTSTART;TZID=US-Eastern;VALUE=DATE:20260807",  # all-day with TZID
        "DTSTART;TZID=America/New_York:20260807T083000",  # unlisted TZID
        "DTSTART;TZID=US-Eastern:20260807T083000.500",  # fractional seconds
        "DTSTART;TZID=US-Eastern:20260807T083000 ",  # trailing character
        "DTSTART;TZID=US-Eastern:20260807T08300",  # short
        "DTSTART;TZID=US-Eastern:20260807T0830000",  # long
        "DTSTART;TZID=US-Eastern:2026-08-07T08:30:00",  # extended form
    ],
)
def test_a9_every_other_dtstart_spelling_rejects(dtstart):
    calendar = parse_bls_calendar_v1(_calendar(events=[_event(dtstart=dtstart)]))
    with pytest.raises(BlsCalendarRejection, match="dtstart"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


def test_a9_a_matching_summary_with_no_dtstart_rejects():
    calendar = parse_bls_calendar_v1(
        _calendar(events=[("UID:no-start@bls.gov", f"SUMMARY:{SUMMARY}")])
    )
    with pytest.raises(BlsCalendarRejection, match="dtstart"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


# --- A10/A11: DST correctness and zone canonicalization ----------------------


def test_a10_summer_resolves_at_utc_minus_four_and_winter_at_utc_minus_five():
    calendar = parse_bls_calendar_v1(pinned_ics_bytes())
    summer = select_release_instant_v1(
        calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
    )
    winter = select_release_instant_v1(
        calendar, summary=SUMMARY, local_date=date(2026, 1, 9)
    )
    assert summer == datetime(2026, 8, 7, 12, 30, tzinfo=UTC)
    assert winter == datetime(2026, 1, 9, 13, 30, tzinfo=UTC)
    # A fixed UTC-5 resolution is disproved by the summer counter-example.
    assert summer != datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
    assert summer.utcoffset() == timedelta(0)
    assert summer - datetime(2026, 8, 7, 8, 30, tzinfo=UTC) == timedelta(hours=4)
    assert winter - datetime(2026, 1, 9, 8, 30, tzinfo=UTC) == timedelta(hours=5)


def test_a10_the_resolved_offset_is_cross_checked_against_the_vtimezone():
    # A VTIMEZONE declaring only the two legal offsets is required, so an
    # instant resolving outside them cannot be reached: prove the guard is
    # live by declaring a calendar whose event resolves to neither.
    calendar = parse_bls_calendar_v1(pinned_ics_bytes())
    narrowed = BlsCalendarV1(
        calendar.timezone_id,
        (timedelta(hours=-5),),
        calendar.events,
        calendar.body_sha256,
        calendar.body_bytes,
    )
    with pytest.raises(BlsCalendarRejection, match="dst_crosscheck"):
        select_release_instant_v1(
            narrowed, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


def test_a11_the_zone_is_canonicalized_through_an_exact_allowlist():
    assert BLS_TZID == "US-Eastern"
    assert IANA_ZONE == "America/New_York"
    source = (ROOT / "src/tokmo/facts/bls_release_calendar.py").read_text()
    for forbidden in (
        "localtime",
        "tzlocal",
        "astimezone()",
        "time.time",
        "EST",
        "-05:00",
        "08:30",
    ):
        assert forbidden not in source, forbidden


def test_a11_an_unresolvable_zone_rejects_without_falling_back(monkeypatch):
    import tokmo.facts.bls_release_calendar as module

    def broken(_name):
        raise KeyError("no tz database")

    monkeypatch.setattr(module, "ZoneInfo", broken)
    calendar = parse_bls_calendar_v1(pinned_ics_bytes())
    with pytest.raises(BlsCalendarRejection, match="unresolvable_zone"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


# --- A12/A13/A14: cardinality of the join ------------------------------------


def test_a12_zero_matching_events_rejects():
    calendar = parse_bls_calendar_v1(_calendar(events=[]))
    with pytest.raises(BlsCalendarRejection, match="no_matching_event"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


def test_a13_byte_identical_duplicate_events_on_one_local_date_reject():
    calendar = parse_bls_calendar_v1(_calendar(events=[_event(), _event()]))
    assert len(calendar.events) == 2
    with pytest.raises(BlsCalendarRejection, match="ambiguous_matching_events"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


def test_a13_duplicates_differing_only_in_time_reject_with_no_earliest_wins():
    calendar = parse_bls_calendar_v1(
        _calendar(
            events=[
                _event(dtstart="DTSTART;TZID=US-Eastern:20260807T083000"),
                _event(dtstart="DTSTART;TZID=US-Eastern:20260807T100000"),
            ]
        )
    )
    with pytest.raises(BlsCalendarRejection, match="ambiguous_matching_events"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )
    source = (ROOT / "src/tokmo/facts/bls_release_calendar.py").read_text()
    for forbidden in ("min(", "max(", "sorted(", "dedup", "first"):
        assert forbidden not in source, forbidden


def test_a14_an_event_on_a_neighbouring_local_date_rejects():
    calendar = parse_bls_calendar_v1(pinned_ics_bytes())
    for neighbour in (date(2026, 8, 6), date(2026, 8, 8)):
        with pytest.raises(BlsCalendarRejection, match="no_matching_event"):
            select_release_instant_v1(calendar, summary=SUMMARY, local_date=neighbour)


def test_a14_the_local_date_is_the_eastern_date_not_the_utc_date():
    # 20:30 Eastern on 2026-08-07 is 00:30Z on 2026-08-08: the event must be
    # selected by its Eastern date, never by the UTC date.
    calendar = parse_bls_calendar_v1(
        _calendar(events=[_event(dtstart="DTSTART;TZID=US-Eastern:20260807T203000")])
    )
    assert select_release_instant_v1(
        calendar,
        summary=SUMMARY,
        local_date=date(2026, 8, 7),
    ) == datetime(2026, 8, 8, 0, 30, tzinfo=UTC)
    with pytest.raises(BlsCalendarRejection, match="no_matching_event"):
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 8)
        )


# --- A15: every rejection is one closed, fail-closed outcome -----------------


def test_a15_every_rejection_is_the_single_closed_rejection_type():
    assert issubclass(BlsCalendarRejection, ValueError)
    bodies = [
        b"x" * (MAX_ICS_BYTES + 1),
        b"BEGIN:VCALENDAR\r\n\xff\r\nEND:VCALENDAR\r\n",
        _calendar(events=[_event()], timezone_line=None),
        _calendar(events=[_event()], vtimezone=()),
    ]
    for body in bodies:
        with pytest.raises(BlsCalendarRejection):
            parse_bls_calendar_v1(body)
    calendars = [
        (_calendar(events=[]), date(2026, 8, 7)),
        (_calendar(events=[_event(), _event()]), date(2026, 8, 7)),
        (
            _calendar(events=[_event(dtstart="DTSTART:20260807T083000")]),
            date(2026, 8, 7),
        ),
        (_calendar(events=[_event()]), date(2026, 8, 6)),
    ]
    for body, day in calendars:
        with pytest.raises(BlsCalendarRejection):
            select_release_instant_v1(
                parse_bls_calendar_v1(body), summary=SUMMARY, local_date=day
            )


def test_the_parser_records_the_body_digest_and_length_without_the_contents():
    import hashlib

    body = pinned_ics_bytes()
    calendar = parse_bls_calendar_v1(body)
    assert calendar.body_sha256 == hashlib.sha256(body).hexdigest()
    assert calendar.body_bytes == len(body)
    assert calendar.timezone_id == BLS_TZID
    assert set(calendar.declared_offsets) == {timedelta(hours=-5), timedelta(hours=-4)}


# Frozen at the base of this delta. The wire digest is what a fetched body's
# ``bls_body_sha256`` would be; the stored digest is what git holds.
STORED_SHA256 = "fb1b2ebb459819c2e6480a1a2ddb05c7d509dfe8b0865e1f7e20b64d6d140799"
STORED_BYTES = 1021
WIRE_SHA256 = "161f471df43de2cf9f206ba21ff21685bb382ec96bef339e6e2aae71f44010bb"
WIRE_BYTES = 1064


def test_the_fixture_is_stored_with_canonical_lf_and_no_trailing_whitespace():
    """R1: repository text carries no CR, so ``git diff --check`` stays clean."""
    import hashlib

    whole = FIXTURE.read_bytes()
    assert b"\r" not in whole, "the committed fixture must be LF-only"
    stored = stored_ics_region()
    assert b"\r" not in stored
    assert hashlib.sha256(stored).hexdigest() == STORED_SHA256
    assert len(stored) == STORED_BYTES
    for line in whole.decode("utf-8").split("\n"):
        assert line == line.rstrip(" \t"), repr(line)


def test_the_wire_reconstruction_is_exactly_lf_to_crlf_and_nothing_else():
    """The one transformation between storage and the parser boundary."""
    import hashlib

    stored = stored_ics_region()
    wire = pinned_ics_bytes()
    assert wire == stored.replace(b"\n", b"\r\n")
    assert wire.replace(b"\r\n", b"\n") == stored, "round trip is lossless"
    assert wire.count(b"\r\n") == stored.count(b"\n")
    assert b"\r" not in wire.replace(b"\r\n", b"")
    assert hashlib.sha256(wire).hexdigest() == WIRE_SHA256
    assert len(wire) == WIRE_BYTES
    assert len(wire) - len(stored) == stored.count(b"\n")
    assert wire.endswith(b"END:VCALENDAR\r\n")


def test_the_bytes_handed_to_the_parser_are_the_wire_form_and_digest_them():
    """The parser sees CRLF bytes, and reports their digest and length."""
    import hashlib

    wire = pinned_ics_bytes()
    seen = []

    class _Recording:
        def __call__(self, body):
            seen.append(body)
            return parse_bls_calendar_v1(body)

    calendar = _Recording()(wire)
    assert seen == [wire]
    assert seen[0].count(b"\r\n") == 43
    assert hashlib.sha256(seen[0]).hexdigest() == WIRE_SHA256
    # The digest the parser reports is the digest of the wire bytes, which is
    # what ``bls_body_sha256`` carries into the schedule provenance.
    assert calendar.body_sha256 == WIRE_SHA256
    assert calendar.body_bytes == WIRE_BYTES
    # The LF storage form is NOT what gets digested, and is not parseable as
    # a folded RFC 5545 body.
    assert calendar.body_sha256 != STORED_SHA256


def test_the_tool_reader_and_this_module_agree_byte_for_byte():
    """One normative reconstruction, cross-checked from the shipping reader."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fred_ondemand_tool_fixture_reader", ROOT / "tools/fred_ondemand_canary.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.read_pinned_ics_fixture(FIXTURE) == pinned_ics_bytes()


def test_the_tool_reader_refuses_a_fixture_stored_with_cr(tmp_path):
    import importlib.util

    import pytest as _pytest

    spec = importlib.util.spec_from_file_location(
        "fred_ondemand_tool_fixture_reader2", ROOT / "tools/fred_ondemand_canary.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"<!--BLS-ICS-BEGIN-->\nBEGIN:VCALENDAR\r\n<!--BLS-ICS-END-->\n")
    with _pytest.raises(ValueError, match="noncanonical_ics_fixture_storage"):
        module.read_pinned_ics_fixture(bad)


def test_the_frozen_digests_match_the_metadata_block_the_fixture_declares():
    """The constants frozen in this module and the fixture's own §4.10
    declaration must never drift apart."""
    meta = fixture_metadata()
    assert meta["stored_bytes_sha256"] == STORED_SHA256
    assert int(meta["stored_bytes_length"]) == STORED_BYTES
    assert meta["reconstructed_wire_sha256"] == WIRE_SHA256
    assert int(meta["reconstructed_wire_length"]) == WIRE_BYTES
    assert meta["modelled_resource_url"] == (
        "https://www.bls.gov/schedule/news_release/bls.ics"
    )
    text = FIXTURE.read_text()
    assert "Only the D2-gated" in text and "live canary closes that gap" in text


def test_the_parser_reads_no_clock_environment_or_socket(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network_forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    calendar = parse_bls_calendar_v1(pinned_ics_bytes())
    assert (
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )
        is not None
    )


def test_the_selector_refuses_untyped_arguments():
    calendar = parse_bls_calendar_v1(pinned_ics_bytes())
    with pytest.raises(BlsCalendarRejection, match="invalid_calendar"):
        select_release_instant_v1(
            "calendar", summary=SUMMARY, local_date=date(2026, 8, 7)
        )
    with pytest.raises(BlsCalendarRejection, match="invalid_summary"):
        select_release_instant_v1(
            calendar, summary=b"Employment Situation", local_date=date(2026, 8, 7)
        )
    with pytest.raises(BlsCalendarRejection, match="invalid_local_date"):
        select_release_instant_v1(calendar, summary=SUMMARY, local_date="2026-08-07")


@pytest.mark.parametrize(
    "name,body",
    [
        ("absent_timezone", None),
        ("absent_vtimezone", None),
        ("zero_events", None),
        ("duplicate_events", None),
        ("neighbouring_date", None),
        ("floating_dtstart", None),
    ],
)
def test_a15_every_calendar_rejection_reaches_the_caller_as_one_stop(name, body):
    """A6-A14 all collapse to the single closed rejection, never a downgrade."""
    bodies = {
        "absent_timezone": _calendar(events=[_event()], timezone_line=None),
        "absent_vtimezone": _calendar(events=[_event()], vtimezone=()),
        "zero_events": _calendar(events=[]),
        "duplicate_events": _calendar(events=[_event(), _event()]),
        "neighbouring_date": _calendar(
            events=[_event(dtstart="DTSTART;TZID=US-Eastern:20260806T083000")]
        ),
        "floating_dtstart": _calendar(
            events=[_event(dtstart="DTSTART:20260807T083000")]
        ),
    }
    with pytest.raises(BlsCalendarRejection):
        calendar = parse_bls_calendar_v1(bodies[name])
        select_release_instant_v1(
            calendar, summary=SUMMARY, local_date=date(2026, 8, 7)
        )


# ---------------------------------------------------------------------------
# Revision D §4.10 / §8.1 acceptance A40-A41: fixture classification and the
# storage-to-wire chain.
# ---------------------------------------------------------------------------

META_BEGIN = "<!--BLS-FIXTURE-META-BEGIN-->"
META_END = "<!--BLS-FIXTURE-META-END-->"

REQUIRED_META_KEYS = (
    "status",
    "provider_request",
    "corroborating_source_url",
    "corroborating_cache_path",
    "corroborating_capture_provider",
    "corroborating_capture_instants",
    "corroborating_limitations",
    "repository_storage",
    "wire_reconstruction",
    "stored_bytes_sha256",
    "stored_bytes_length",
    "reconstructed_wire_sha256",
    "reconstructed_wire_length",
    "modelled_summaries",
    "modelled_local_dates",
    "modelled_vtimezone_offsets",
    "official_path_component",
)


def fixture_metadata(text: str | None = None) -> dict:
    """Parse the §4.10 metadata block into exact key/value pairs."""
    body = FIXTURE.read_text() if text is None else text
    if META_BEGIN not in body or META_END not in body:
        raise AssertionError("fixture_metadata_block_missing")
    block = body[body.index(META_BEGIN) + len(META_BEGIN) : body.index(META_END)]
    fields = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def test_a40_the_storage_to_wire_chain_is_pinned_end_to_end():
    """Stored LF bytes → declared stored digest → CRLF wire → declared wire
    digest → those exact bytes into the parser, all in one test so neither
    side of the transformation can drift unnoticed."""
    import hashlib

    meta = fixture_metadata()
    stored = stored_ics_region()

    # 1. the stored form is LF-canonical and matches its declared identity.
    assert b"\r" not in stored, "stored fixture must be LF-canonical"
    assert hashlib.sha256(stored).hexdigest() == meta["stored_bytes_sha256"]
    assert len(stored) == int(meta["stored_bytes_length"])

    # 2. the reconstruction is deterministic and is exactly LF -> CRLF.
    wire = stored.replace(b"\n", b"\r\n")
    assert wire == pinned_ics_bytes()
    assert wire.replace(b"\r\n", b"\n") == stored

    # 3. the reconstructed wire form matches its declared identity, asserted
    #    BEFORE the bytes are handed to the parser.
    wire_digest = hashlib.sha256(wire).hexdigest()
    assert wire_digest == meta["reconstructed_wire_sha256"]
    assert len(wire) == int(meta["reconstructed_wire_length"])
    assert wire_digest != meta["stored_bytes_sha256"]

    # 4. those exact bytes are what the parser receives and digests.
    calendar = parse_bls_calendar_v1(wire)
    assert calendar.body_sha256 == meta["reconstructed_wire_sha256"]
    assert calendar.body_bytes == int(meta["reconstructed_wire_length"])
    assert select_release_instant_v1(
        calendar,
        summary=SUMMARY,
        local_date=date(2026, 8, 7),
    ) == datetime(2026, 8, 7, 12, 30, tzinfo=UTC)


def test_a40_the_declared_event_scope_matches_what_the_fixture_models():
    meta = fixture_metadata()
    calendar = parse_bls_calendar_v1(pinned_ics_bytes())
    summaries = {event.summary for event in calendar.events}
    assert summaries == {
        value.strip() for value in meta["modelled_summaries"].split(";")
    }
    declared_dates = {
        value.strip() for value in meta["modelled_local_dates"].split(";")
    }
    assert declared_dates == {"2026-01-09", "2026-08-07", "2026-08-12"}
    for day in sorted(declared_dates):
        assert day in FIXTURE.read_text()
    assert meta["modelled_vtimezone_offsets"] == "STANDARD -0500; DAYLIGHT -0400"
    assert set(calendar.declared_offsets) == {timedelta(hours=-5), timedelta(hours=-4)}


def test_a41_the_metadata_declares_reconstructed_status_and_no_provider_request():
    meta = fixture_metadata()
    assert set(REQUIRED_META_KEYS) <= set(meta), sorted(
        set(REQUIRED_META_KEYS) - set(meta)
    )
    assert meta["status"] == "RECONSTRUCTED_CONTRACT"
    assert meta["provider_request"] == "none"
    assert meta["repository_storage"] == "canonical-LF"
    # The historical path component carries no provenance claim (§6 rev D).
    assert meta["official_path_component"].startswith("historical-placement")
    # Corroboration is declared together with its limitations.
    for limitation in (
        "normalized-line-endings",
        "folding-not-preserved",
        "no-origin-http-metadata",
        "not-a-wire-capture",
    ):
        assert limitation in meta["corroborating_limitations"], limitation


def test_a41_a_fixture_claiming_official_or_direct_origin_status_is_rejected():
    """The check is load-bearing: a relabelled metadata block must fail."""
    text = FIXTURE.read_text()
    meta = fixture_metadata(text)
    # Authority is carried by the declared values, never by prose: the file is
    # free to *deny* official status in words, but may not declare it.
    assert meta["status"] == "RECONSTRUCTED_CONTRACT"
    assert meta["status"] not in (
        "OFFICIAL_RETRIEVAL",
        "DIRECT_ORIGIN",
        "G0_OFFICIAL_RETRIEVAL",
    )
    for key, value in meta.items():
        lowered = value.lower()
        if key in ("corroborating_limitations", "official_path_component"):
            continue
        assert "official retrieval" not in lowered, key
        assert "direct-origin capture" not in lowered, key
    assert meta["provider_request"] == "none"
    for forged in ("OFFICIAL_RETRIEVAL", "DIRECT_ORIGIN"):
        mutant = text.replace("- status: RECONSTRUCTED_CONTRACT", f"- status: {forged}")
        assert fixture_metadata(mutant)["status"] != "RECONSTRUCTED_CONTRACT"
        assert fixture_metadata(mutant)["status"] == forged
    forged_request = text.replace(
        "- provider_request: none", "- provider_request: live-origin-fetch"
    )
    assert fixture_metadata(forged_request)["provider_request"] != "none"


def test_a41_a_cr_bearing_stored_fixture_is_rejected(tmp_path):
    """A CRLF-bearing stored file would make the reconstruction a no-op."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fred_ondemand_tool_a41", ROOT / "tools/fred_ondemand_canary.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bad = tmp_path / "cr-bearing.md"
    bad.write_bytes(
        b"<!--BLS-ICS-BEGIN-->\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        b"<!--BLS-ICS-END-->\n"
    )
    with pytest.raises(ValueError, match="noncanonical_ics_fixture_storage"):
        module.read_pinned_ics_fixture(bad)
    # The committed fixture is the passing counterpart of that rejection.
    assert module.read_pinned_ics_fixture(FIXTURE) == pinned_ics_bytes()
    assert b"\r" not in FIXTURE.read_bytes()
