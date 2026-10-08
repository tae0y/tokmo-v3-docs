# BLS release-calendar ICS fixture (RECONSTRUCTED_CONTRACT)

Classification and required metadata are governed by Delta SDS rev D §4.10.
This file is **not** a direct-origin capture and must never be described as
official wire bytes.

<!--BLS-FIXTURE-META-BEGIN-->
- status: RECONSTRUCTED_CONTRACT
- provider_request: none
- modelled_resource_url: https://www.bls.gov/schedule/news_release/bls.ics
- corroborating_source_url: https://www.bls.gov/schedule/news_release/bls.ics
- corroborating_cache_path: ~/.hermes/cache/web/www.bls.gov-18ad707fc4.md
- corroborating_capture_provider: hermes-web-tool via firecrawl (transformed source)
- corroborating_capture_instants: 2026-08-12T22:03:31+09:00; 2026-08-12T22:28:33+09:00
- corroborating_limitations: normalized-line-endings; folding-not-preserved; no-origin-http-metadata; not-a-wire-capture
- repository_storage: canonical-LF
- wire_reconstruction: LF-to-CRLF at the parser boundary via tools/fred_ondemand_canary.read_pinned_ics_fixture
- stored_bytes_sha256: fb1b2ebb459819c2e6480a1a2ddb05c7d509dfe8b0865e1f7e20b64d6d140799
- stored_bytes_length: 1021
- reconstructed_wire_sha256: 161f471df43de2cf9f206ba21ff21685bb382ec96bef339e6e2aae71f44010bb
- reconstructed_wire_length: 1064
- modelled_summaries: Employment Situation; Consumer Price Index
- modelled_local_dates: 2026-01-09; 2026-08-07; 2026-08-12
- modelled_vtimezone_offsets: STANDARD -0500; DAYLIGHT -0400
- official_path_component: historical-placement-only; carries no provenance claim (SDS rev D section 6)
<!--BLS-FIXTURE-META-END-->

## Why this fixture is reconstructed

No direct-origin, byte-preserving capture of the BLS ICS exists locally, and
obtaining one requires live retrieval from `www.bls.gov`, which is forbidden
until the D2 gate. The calendar below was therefore assembled without any
provider request, modelling the relevant events exactly.

## Corroboration and its limits

The cached capture named in the metadata above confirms that the real calendar
carries `DTSTART;TZID=US-Eastern:20260807T083000` and
`DTSTART;TZID=US-Eastern:20260109T083000` under `SUMMARY:Employment Situation`,
with `X-WR-TIMEZONE:US-Eastern` and a `VTIMEZONE` declaring `-0500` / `-0400`
— the exact shape modelled below.

That capture cannot stand as wire bytes and is deliberately not vendored here:
it was retrieved through a third-party scraping intermediary rather than from
the origin; its line endings were normalized and no folded line survives, so
the wire form cannot be recovered from it; and no HTTP status, `Content-Length`
or `ETag` was retained.

## Storage form versus wire form

The body between the two ICS markers is stored with canonical LF, so the
repository text carries no trailing-whitespace artefact. RFC 5545 requires
CRLF on the wire and the parser's unfolding rule is defined on CRLF, so the
reader reconstructs CRLF deterministically at the parser boundary; the
reconstructed wire digest above is what reaches `parse_bls_calendar_v1`. The
stored form contains no CR byte, and a CR-bearing stored fixture is rejected
outright — it would make the reconstruction a no-op and hide whether CRLF
handling works. Acceptance items A40 and A41 pin this chain in
`tests/test_bls_release_calendar.py`.

## What this fixture proves, and what it does not

It proves the parser, the join, the sidecar and the store contracts — the §4.3
rejection matrix, the §4.4 derivation, §4.6 persistence and reload, and the
§4.7 guard. It does **not** prove origin byte fidelity, live HTTP behaviour,
redirect behaviour, source response headers or live schedule acquisition, and
it may not be cited as evidence of direct-origin retrieval. Only the D2-gated
live canary closes that gap (§8.3 C2, D6).

The two `Employment Situation` events below are deliberately one summer (EDT,
UTC-4) and one winter (EST, UTC-5) release, so a fixed `-05:00` resolution is
disproved by the fixture itself.

---

<!--BLS-ICS-BEGIN-->
BEGIN:VCALENDAR
PRODID:-//U.S. Bureau of Labor Statistics//Release Calendar//EN
VERSION:2.0
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:BLS Release Calendar
X-WR-TIMEZONE:US-Eastern
BEGIN:VTIMEZONE
TZID:US-Eastern
BEGIN:STANDARD
DTSTART:20251102T020000
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:20260308T020000
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:20260109T083000-employment-situation@bls.gov
DTSTAMP:20251215T120000Z
DTSTART;TZID=US-Eastern:20260109T083000
SUMMARY:Employment Situation
END:VEVENT
BEGIN:VEVENT
UID:20260807T083000-employment-situation@bls.gov
DTSTAMP:20260701T120000Z
DTSTART;TZID=US-Eastern:20260807T083000
SUMMARY:Employment Situation
END:VEVENT
BEGIN:VEVENT
UID:20260812T083000-consumer-price-index@bls.gov
DTSTAMP:20260701T120000Z
DTSTART;TZID=US-Eastern:20260812T083000
SUMMARY:Consumer Price Index
END:VEVENT
END:VCALENDAR
<!--BLS-ICS-END-->
