"""Pure BLS release-calendar ICS boundary: bytes in, one aware instant out.

G2 Delta SDS §4.3. This module is the second authority of the dual-authority
release schedule: FRED owns the release identity and the release date, the
BLS calendar owns the publication instant, which no FRED endpoint returns.

It is deliberately transport-free. It holds no provider URL literal, opens no
socket, reads no clock and no environment, and touches no cache: the calendar
bytes are handed to it by the tool that owns the network boundary. Every
outcome other than a single unambiguously matched event is a
``BlsCalendarRejection`` — a STOP the caller may only translate into
``UNAVAILABLE_RELEASE_SCHEDULE`` with FRED observations wire 0. There is no
fallback to an assumed time of day, to a fixed winter offset, to the host
zone, or to an unmatched event.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

MAX_ICS_BYTES = 524_288

# Exact allowlists. The calendar identifier the provider publishes, and the
# one IANA zone it is permitted to canonicalize to.
BLS_TZID = "US-Eastern"
IANA_ZONE = "America/New_York"
ZONE_ALLOWLIST = {BLS_TZID: IANA_ZONE}

# The two offsets the VTIMEZONE must declare, keyed by subcomponent. A
# calendar declaring anything else is rejected rather than interpreted.
STANDARD_OFFSET = timedelta(hours=-5)
DAYLIGHT_OFFSET = timedelta(hours=-4)
DECLARED_SUBCOMPONENTS = {"STANDARD": STANDARD_OFFSET, "DAYLIGHT": DAYLIGHT_OFFSET}

CALENDAR_TIMEZONE_PROPERTY = "X-WR-TIMEZONE"

# RFC 5545 local date-time, in the two spellings this boundary accepts: full
# seconds, and the seconds-omitted spelling normalized to :00. Any other
# form — floating, UTC ``Z``, ``VALUE=DATE``, fractional seconds, an unlisted
# TZID or any trailing character — fails this pattern and is rejected.
DTSTART_PATTERN = re.compile(
    rf"DTSTART;TZID={re.escape(BLS_TZID)}:(\d{{8}})T(\d{{2}})(\d{{2}})(\d{{2}})?"
)
OFFSET_PATTERN = re.compile(r"([+-])(\d{2})(\d{2})")


class BlsCalendarRejection(ValueError):
    """The single closed rejection of this boundary; never a downgrade."""


@dataclass(frozen=True)
class BlsCalendarEventV1:
    """One retained VEVENT: its summary and its raw DTSTART property line."""

    summary: str | None
    dtstart: str | None


@dataclass(frozen=True)
class BlsCalendarV1:
    timezone_id: str
    declared_offsets: tuple
    events: tuple
    body_sha256: str
    body_bytes: int


def _unfold(text: str) -> str:
    """Undo RFC 5545 line folding before any property is matched."""
    return text.replace("\r\n ", "").replace("\r\n\t", "")


def _property_name(line: str) -> str:
    return line.split(":", 1)[0].split(";", 1)[0]


def _property_value(line: str) -> str:
    parts = line.split(":", 1)
    if len(parts) != 2:
        raise BlsCalendarRejection("invalid_property_grammar")
    return parts[1]


def _declared_offset(literal: str) -> timedelta:
    match = OFFSET_PATTERN.fullmatch(literal)
    if match is None:
        raise BlsCalendarRejection("vtimezone_offset_grammar_rejected")
    sign = -1 if match.group(1) == "-" else 1
    return timedelta(
        hours=sign * int(match.group(2)), minutes=sign * int(match.group(3))
    )


def parse_bls_calendar_v1(body: bytes) -> BlsCalendarV1:
    """Parse one official calendar body; reject anything not exactly known."""
    if type(body) is not bytes:
        raise BlsCalendarRejection("invalid_body")
    if len(body) > MAX_ICS_BYTES:
        raise BlsCalendarRejection("body_too_large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BlsCalendarRejection("invalid_encoding") from error

    stack = []
    calendar_zone = None
    timezone_ids = []
    subcomponent_offsets = {name: [] for name in DECLARED_SUBCOMPONENTS}
    events = []
    event = None
    for line in re.split(r"\r\n|\n", _unfold(text)):
        if not line:
            continue
        if line.startswith("BEGIN:"):
            component = _property_value(line)
            stack.append(component)
            if component == "VEVENT":
                event = {}
            continue
        if line.startswith("END:"):
            component = _property_value(line)
            if not stack or stack[-1] != component:
                raise BlsCalendarRejection("invalid_component_nesting")
            stack.pop()
            if component == "VEVENT":
                events.append(
                    BlsCalendarEventV1(event.get("SUMMARY"), event.get("DTSTART"))
                )
                event = None
            continue
        name = _property_name(line)
        if stack == ["VCALENDAR"] and name == CALENDAR_TIMEZONE_PROPERTY:
            if calendar_zone is not None:
                raise BlsCalendarRejection("duplicate_calendar_timezone")
            calendar_zone = _property_value(line)
        elif stack[-2:] == ["VCALENDAR", "VTIMEZONE"] and name == "TZID":
            timezone_ids.append(_property_value(line))
        elif (
            len(stack) == 3
            and stack[1] == "VTIMEZONE"
            and stack[2] in DECLARED_SUBCOMPONENTS
            and name == "TZOFFSETTO"
        ):
            subcomponent_offsets[stack[2]].append(
                _declared_offset(_property_value(line))
            )
        elif stack[-1:] == ["VEVENT"] and name in ("SUMMARY", "DTSTART"):
            if name in event:
                raise BlsCalendarRejection("duplicate_event_property")
            event[name] = _property_value(line) if name == "SUMMARY" else line
    if stack:
        raise BlsCalendarRejection("invalid_component_nesting")
    if calendar_zone != BLS_TZID:
        raise BlsCalendarRejection("calendar_timezone_rejected")
    if timezone_ids != [BLS_TZID]:
        raise BlsCalendarRejection("vtimezone_identity_rejected")
    for component, expected in DECLARED_SUBCOMPONENTS.items():
        declared = subcomponent_offsets[component]
        if not declared or set(declared) != {expected}:
            raise BlsCalendarRejection("vtimezone_offsets_rejected")
    return BlsCalendarV1(
        calendar_zone,
        (STANDARD_OFFSET, DAYLIGHT_OFFSET),
        tuple(events),
        hashlib.sha256(body).hexdigest(),
        len(body),
    )


def _local_datetime(raw: object) -> datetime:
    if type(raw) is not str:
        raise BlsCalendarRejection("dtstart_absent")
    match = DTSTART_PATTERN.fullmatch(raw)
    if match is None:
        raise BlsCalendarRejection("dtstart_grammar_rejected")
    day, hour, minute, second = match.groups()
    try:
        return datetime(
            int(day[:4]),
            int(day[4:6]),
            int(day[6:]),
            int(hour),
            int(minute),
            0 if second is None else int(second),
        )
    except ValueError as error:
        raise BlsCalendarRejection("dtstart_calendar_rejected") from error


def select_release_instant_v1(
    calendar: BlsCalendarV1, *, summary: str, local_date: date
) -> datetime:
    """Return the one official instant for this summary and Eastern date.

    Zero matches, two or more matches — including duplicate events carrying
    the same local date, whether byte-identical or differing in time — an
    unparseable DTSTART, an unresolvable zone or an offset the calendar never
    declared are all rejections. Deduplication and any earliest-wins or
    latest-wins pick would be a guess, so none exists here.
    """
    if not isinstance(calendar, BlsCalendarV1):
        raise BlsCalendarRejection("invalid_calendar")
    if type(summary) is not str or not summary:
        raise BlsCalendarRejection("invalid_summary")
    if type(local_date) is not date:
        raise BlsCalendarRejection("invalid_local_date")
    try:
        zone = ZoneInfo(ZONE_ALLOWLIST[calendar.timezone_id])
    except (KeyError, ValueError, OSError) as error:
        raise BlsCalendarRejection("unresolvable_zone") from error
    matched = []
    for event in calendar.events:
        if event.summary != summary:
            continue
        moment = _local_datetime(event.dtstart).replace(tzinfo=zone)
        if moment.utcoffset() not in calendar.declared_offsets:
            raise BlsCalendarRejection("dst_crosscheck_rejected")
        if moment.date() == local_date:
            matched.append(moment)
    if not matched:
        raise BlsCalendarRejection("no_matching_event")
    if len(matched) != 1:
        raise BlsCalendarRejection("ambiguous_matching_events")
    return matched[0].astimezone(UTC)
