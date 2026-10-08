#!/usr/bin/env python3
"""'12시에 만나요' 신규 영상 감지 + 자막 추출 (EXT-YT-001 Task 0).

헤르메스 agent cron의 script로 쓴다: stdout이 에이전트 프롬프트에 주입된다.
출력 프로토콜 (cron 소비자 e7254690397b 계약 유지):
  (빈 출력)                 — 알림/분석 불필요.
  STATUS: NEW_TRANSCRIPT    — 제목·자막 전문 뒤따름 → 분석·전달
  {"wakeAgent":false,...}   — 수집은 됐으나 분석층 상한(INPUT_CAP_EXCEEDED)에
                              걸려 넘기지 못한 tick. agent를 깨우지 않으면서
                              (토큰·전달 0), scheduler가 이 tick을 'ok'로 기록해
                              앞선 COLLECTED && !ANALYZED 실패를 지우지 못하게
                              하는 typed 근거 한 줄. unresolved_gate_line 참조.

Lifecycle 분리 (2026-08-21 incident 수리 + 독립 리뷰 FAIL 폐쇄): 수집 완료
(COLLECTED)와 분석 완료(ANALYZED)·전달 완료(DELIVERED)는 별개 상태다.
collection_state_v1.json이 video_id+source_revision 정체성으로 영속한다.
  - 모든 lifecycle/아카이브 read-modify-write는 per-job OS 파일 잠금
    (runtime/tokmo-yt/collection_v1.lock) 하나로 프로세스 단위 직렬화된다.
    잠금 timeout은 typed fail-closed(state_lock_timeout — 무변이·무출력).
    영속은 잠금 하에서도 (video_id, source_revision, prior_status) CAS로만
    이뤄지며 상태는 단조다: replay attempt 갱신이 ANALYZED/DELIVERED를
    절대 되돌리지 못한다.
  - 기존 canonical v1 파일의 모든 이상(비파싱/비가독/스키마 위반/모드/
    symlink/hardlink)은 CORRUPT이지 절대 '부재'가 아니다: typed dedupe 알림
    + fail-closed(수집·replay·legacy migration·discovery 전부 금지),
    기존 바이트 보존. legacy migration은 정확히 ENOENT일 때만.
  - 아카이브 쌍(transcript+meta)은 same-dir temp 0600 + fsync + 원자 발행
    이며, 두 파일을 read-back해 canonical 정체성이 일치한 뒤에만 COLLECTED를
    선언한다. 메타 정체성은 부분집합(video_id+해시)이 아니라 pipeline이
    실제 생산하는 source field 전부(video_id/url/title/published(aware)/
    playlist/channel/sha/extraction + schema v2)에 실제 선택된 caption
    track에서 관측된 provenance(languageCode/kind/vssId 원문 — 부재
    optional field는 일관된 typed null, 언어에서 kind 추론·상수 날조 금지)
    를 더한 것이며, 결정적 JSON 위 identity_digest로 meta에 저장된다.
    기존-meta 수용·같은 attempt partial 수리·replay·최종 read-back은 전부
    단일 helper _validate_archive_meta로 판정하고, 어떤 field 불일치도
    (같은 자막의 다른 관측 provenance 포함) typed archive conflict
    fail-closed(덮어쓰기/재조회/payload/상태 변이 없음)다.
    pre-v2(digest 부재) legacy meta는 무기한 수용되지 않는다: 구 live
    writer의 진짜 형태(정확한 key 집합·writer 상수 표식·aware fetched_at)
    일 때만, 관측 있는 수집 attempt에서는 전 source field가 현재 관측과
    정확 일치할 때, 관측 없는 replay에서는 저장 transcript/meta/state/경로
    가 하나의 일관 tuple(정규 0600 단일링크, 해시·revision·경로 재검증)로
    증명될 때 job_lock 하 정확히 1회 원자 migration(source 값·fetched_at
    불변, 관측 없던 caption field는 명시 null, migration_origin/migrated_at
    증거 + digest 증강)으로 v2 승격한 뒤에만 payload 근거로 쓴다. 성공한
    migration 후 digest-less legacy의 재등장은 payload 근거로 재수용하지
    않는다(state의 내구 archive_meta_migrated_at 기록). COLLECTED 이후
    로컬 전용 tick은 수리 목적으로도 절대 재조회하지 않는다.
  - COLLECTED && !ANALYZED: 이후 tick은 로컬 아카이브에서 동일 NEW_TRANSCRIPT
    payload를 network 0으로 재방출한다(17:45 cutoff 이후 포함 — cutoff는
    source discovery만 지배). YouTube/자막 재조회·아카이브 중복 없음.
  - ANALYZED/DELIVERED 전이는 스크립트가 관측할 수 없으므로 명시적 ack로만.
    운영 경로는 확인된 전달 후의 원자 --ack-completed VID --revision REV
    1회다(Hermes scheduler의 내구 delivery-receipt 통합이 호출 — 별도
    리뷰 게이트, 그 전까지 CUTOVER_BLOCKED). --ack-analyzed/--ack-delivered
    는 수동 진단 전용이며 cron 프롬프트에 절대 넣지 않는다. false green
    날조 금지 — ack 없이는 상태가 COLLECTED에 진실하게 머문다.
  - legacy last_delivered.json은 '수집 영수증'으로만 migration되며 절대
    ANALYZED/DELIVERED로 승격되지 않는다. 롤백 호환을 위해 수집 성공 시
    legacy 파일도 같은 포맷으로 계속 쓴다.
  - 분석 입력에는 실측에서 유도한 상한이 있다(report v3 계약 §8):
    ANALYSIS_INPUT_CAP_BYTES = 2 × 2026-08-27 실패 표본 113,340 bytes
    = 226,680 stdout payload bytes(≈148분 분량, 120분 지원 상한 대비 약
    23% 여유). 경계 포함(≤)으로 admit하며, 초과 입력은 transcript를 자르지도
    성공으로 기록하지도 않는다 — 아카이브·수집 영수증을 보존한 채 payload를
    방출하지 않고 typed INPUT_CAP_EXCEEDED(stage=analysis) 알림 + wake gate
    한 줄(unresolved_gate_line)만 남긴다. 상태는 진실하게 COLLECTED &&
    !ANALYZED에 머문다.
  - 모델 fallback은 자동 활성화되지 않는다: load_model_fallback_policy()
    seam은 기본 OFF, 유효한 명시 opt-in만 enabled(fail-closed).

Task 0 (2026-08-15) typed observations:
  ALREADY_COLLECTED / TODAY_VIDEO_NOT_FOUND / TODAY_VIDEO_TITLE_MISMATCH /
  VIDEO_FOUND_CAPTIONS_PENDING / CAPTION_TRACK_FOUND_TRANSCRIPT_PENDING /
  TRANSCRIPT_READY / SOURCE_ERROR(stage, error_category) /
  PUBLIC_HOLIDAY_SKIP / HOLIDAY_DATA_UNAVAILABLE (Task 0.2)

자동 discovery authority는 YouTube Data API v3 playlistItems(지정 프로그램 재생목록)
하나뿐이다. 2026-08-19 RSS 404 장애 당시 API로 교체했다(사용자 승인
2026-08-19, 기록: ~/.hermes/shared/claude-code/2026-08-19_12man-discovery-api.md).
API 키는 env YOUTUBE_API_KEY 또는 ~/.hermes/.env에서 읽고 절대 로그에 남기지
않는다. 17:45 KST가 마지막 자동 network attempt이며
이후 자동 tick은 network 0으로 종료한다. --force-once는 cutoff를 정확히 한
invocation만 우회한다(내부 retry 없음).

전부 read-only(유튜브 공개 데이터). 상태 갱신은 atomic write, mode 0600.
레시피 정본: projects/tokmo-v3/.ai/BACKLOG.md EXT-YT-001 (2026-08-07 실측).
"""
import argparse
import errno
import fcntl
import hashlib
import os
import json
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PLAYLIST = "PLpDZdhM6kelSHHNdphTwAWuxxwbI4kGyX"  # 12시에 만나요 지정 재생목록
CHANNEL_ID = "UCAAvO0ehWox1bbym3rXKBZw"


def hermes_home():
    # 호출 시점마다 재해석한다 — 격리 HOME 테스트가 env만 바꾸면 되도록.
    override = os.environ.get("HERMES_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".hermes"


def state_path():
    # legacy `last_delivered.json` — 포맷({id,title,published})은 그대로 유지하며
    # 의미만 '오늘 수집 완료(collection receipt)'로 쓴다(스킴 버전 없음).
    return hermes_home() / "runtime/tokmo-yt/last_delivered.json"


def alerts_path():
    return hermes_home() / "runtime/tokmo-yt/alerts_v1.json"


def holidays_path():
    # 대한민국 공휴일 동기화 cron이 쓰는 schema v2 공유 파일(읽기 전용).
    # 수집기는 이 파일을 절대 쓰지 않고 KASI/Google을 직접 호출하지도 않는다.
    return hermes_home() / "shared/korean_holidays.json"


def inbox_path():
    # 톡모 FILE_IMPORT landing 구역 (정본 아님 — 톡모의 승인된 import 프로그램이
    # 나중에 검증하며 video_contents로 승격한다. EXT-YT-001 참조).
    return Path.home() / ".local/share/tokmo-v3/inbox/youtube/12man"


def collection_state_path():
    # lifecycle 정본. DISCOVERED는 tick 내부 관측으로만 존재하고 영속 상태는
    # COLLECTED → ANALYZED → DELIVERED 뿐이다.
    return hermes_home() / "runtime/tokmo-yt/collection_state_v1.json"


def model_fallback_policy_path():
    return hermes_home() / "runtime/tokmo-yt/model_fallback_policy_v1.json"


def lock_path():
    # per-job OS 잠금 하나가 lifecycle/아카이브 read-modify-write 전부를
    # 프로세스 단위로 직렬화한다 (2026-08-21 독립 리뷰 BLOCKER 2 폐쇄).
    return hermes_home() / "runtime/tokmo-yt/collection_v1.lock"


DEFAULT_LOCK_TIMEOUT_SECONDS = 120.0  # 최악 fetch(3×30s)를 품는 여유
LOCK_POLL_SECONDS = 0.05


class StateLockTimeout(Exception):
    """per-job 잠금 획득 실패 — typed fail-closed(무변이·무출력)."""

    category = "state_lock_timeout"


def _lock_timeout_seconds():
    raw = os.environ.get("TOKMO_YT_LOCK_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass  # 비정상 override는 기본값 fail-closed
    return DEFAULT_LOCK_TIMEOUT_SECONDS


@contextmanager
def job_lock(timeout=None):
    """per-job flock. timeout 내 미획득이면 StateLockTimeout(무변이)."""
    path = lock_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + (
            _lock_timeout_seconds() if timeout is None else timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout()
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(fd)  # close가 flock을 해제한다


UA = {"User-Agent": "Mozilla/5.0"}

SEOUL = ZoneInfo("Asia/Seoul")
# YouTube Data API v3 — 2026-08-19 RSS 404 장애 당시 discovery 교체.
# 지정 재생목록의 최신 10건만 조회한다(전체 과거 목록 순회 없음).
DISCOVERY_API_URL = "https://www.googleapis.com/youtube/v3/playlistItems"

TITLE_MARKER = "12시에 만나요"
ALERT_FROM_HOUR = 14
ALERT_FROM_MINUTE = 30
# 17:45 KST가 마지막 자동 network attempt. (hh, mm) <= (17, 45) 허용.
AUTOMATIC_CUTOFF = (17, 45)
TRANSCRIPT_MIN_CHARS = 500


def get(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


class NetworkFetcher:
    """프로덕션 fetcher — 단위 테스트는 FakeFetcher로 대체한다."""

    def fetch(self, url, data=None, headers=None):
        return get(url, data=data, headers=headers)


def should_alert_failure(now=None):
    """Alert only from 14:30 KST; earlier polling failures stay silent."""
    observed = datetime.now(timezone.utc) if now is None else now
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("aware_now_required")
    local = observed.astimezone(SEOUL)
    return (local.hour, local.minute) >= (ALERT_FROM_HOUR, ALERT_FROM_MINUTE)


def automatic_window_open(now):
    """마지막 자동 network attempt는 17:45 KST. 17:46부터 자동 network 0."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_now_required")
    local = now.astimezone(SEOUL)
    return (local.hour, local.minute) <= AUTOMATIC_CUTOFF


def _aware(now):
    return datetime.now(timezone.utc) if now is None else now


def kst_date(now):
    return _aware(now).astimezone(SEOUL).date()


SKIP_HOLIDAY_TYPES = frozenset({"public_holiday", "substitute_holiday"})
VALID_HOLIDAY_TYPES = frozenset(
    {"public_holiday", "substitute_holiday", "observance"})
# Task 0.2: 자동 producer(kasi 기본)가 쓰는 정확한 canonical source만 신뢰한다.
KASI_CANONICAL_SOURCE = "KASI SpcdeInfoService/getRestDeInfo"


def _holiday_cache(today):
    """shared/korean_holidays.json 건강성 판정. (state, is_holiday) 반환.

    state: "healthy" | "unavailable". Google Calendar 등 비-KASI source·
    어제 이전 last_fetch·형식 이상은 전부 unavailable(fail-closed)이다.
    healthy가 되려면 holidays map 전체(모든 날짜)가 구조적으로 유효해야
    한다(producer validate와 동일 기준). 수집기는 이 파일만 읽으며
    KASI/Google API를 직접 호출하지 않는다.
    """
    try:
        data = json.loads(holidays_path().read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unavailable", False
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        return "unavailable", False
    if data.get("source") != KASI_CANONICAL_SOURCE:
        return "unavailable", False
    last_fetch = data.get("last_fetch")
    if not isinstance(last_fetch, str):
        return "unavailable", False
    try:
        fetched = datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))
    except ValueError:
        return "unavailable", False
    if fetched.tzinfo is None or fetched.utcoffset() is None:
        return "unavailable", False  # naive timestamp
    if fetched.astimezone(SEOUL).date() != today:
        return "unavailable", False  # stale — 오늘 KASI 동기화 아님
    holidays = data.get("holidays")
    if not isinstance(holidays, dict):
        return "unavailable", False
    # 전체 map 구조 검증 — 오늘 항목만이 아니라 모든 날짜가 유효해야 한다.
    for day, items in holidays.items():
        if not isinstance(day, str):
            return "unavailable", False
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            return "unavailable", False
        if parsed_day.isoformat() != day:  # canonical round-trip
            return "unavailable", False
        if not isinstance(items, list) or not items:  # 비었으면 불건강
            return "unavailable", False
        for i in items:
            if (not isinstance(i, dict)
                    or not isinstance(i.get("name"), str)
                    or not i.get("name")
                    or i.get("type") not in VALID_HOLIDAY_TYPES):
                return "unavailable", False
    items = holidays.get(today.isoformat())
    if items is None:
        return "healthy", False  # 오늘 항목 없음 = 평일
    is_holiday = any(i.get("type") in SKIP_HOLIDAY_TYPES for i in items)
    return "healthy", is_holiday


def holiday_preflight(now):
    """공휴일 preflight. skip이면 PUBLIC_HOLIDAY_SKIP, 캐시 불신뢰면
    HOLIDAY_DATA_UNAVAILABLE, 정상 평일이면 None."""
    state, is_holiday = _holiday_cache(kst_date(now))
    if state == "unavailable":
        return Observation(status="HOLIDAY_DATA_UNAVAILABLE")
    if is_holiday:
        return Observation(status="PUBLIC_HOLIDAY_SKIP")
    return None


# ---------------------------------------------------------------------------
# typed observations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    status: str                      # ALREADY_COLLECTED / TODAY_VIDEO_NOT_FOUND /
                                     # TODAY_VIDEO_TITLE_MISMATCH /
                                     # VIDEO_FOUND_CAPTIONS_PENDING /
                                     # CAPTION_TRACK_FOUND_TRANSCRIPT_PENDING /
                                     # TRANSCRIPT_READY / SOURCE_ERROR
    video_id: str = ""
    title: str = ""
    published: str = ""
    # TODAY_VIDEO_TITLE_MISMATCH 수동 RCA용 후보 메타데이터(선택은 하지 않는다)
    candidates: tuple = ()
    transcript: str = ""
    # 실제 선택된 caption track에서 관측된 provenance — 부재는 typed null.
    caption_language: str | None = None
    caption_kind: str | None = None
    caption_vss_id: str | None = None
    stage: str = ""                  # SOURCE_ERROR 전용
    error_category: str = ""         # SOURCE_ERROR 전용
    error_detail: str = ""           # SOURCE_ERROR 전용(알림 본문 비노출)

    @property
    def fingerprint(self):
        if self.status == "SOURCE_ERROR":
            return (self.status, self.stage, self.error_category)
        return (self.status, self.video_id)


REDACTED = "[redacted]"
# API key는 query string으로 전달되므로 transport 예외 문자열이 요청 URL을
# 그대로 실어오면 secret이 detail에 남는다. 값 치환(원문·percent-encoded)과
# 파라미터 단위 치환을 함께 건다 — 어느 한쪽만으로는 구멍이 남는다.
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:key|api_?key|access_token|token|password)=)[^&\s\"'<>]+",
    re.IGNORECASE)


def _redact(text, secret=None):
    """secret과 URL의 key 파라미터를 지운 문자열. 원문은 절대 반환하지 않는다."""
    out = str(text)
    if secret:
        for form in (secret, urllib.parse.quote_plus(secret),
                     urllib.parse.quote(secret, safe="")):
            if form:
                out = out.replace(form, REDACTED)
    return _SECRET_QUERY_RE.sub(r"\1" + REDACTED, out)


def _classify_error(exc, stage, secret=None):
    if isinstance(exc, (ET.ParseError, ValueError)):
        category = "bad_payload"
    elif isinstance(exc, urllib.error.HTTPError):
        category = "http_error"
    elif isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        category = "network_error"
    else:
        category = type(exc).__name__
    return Observation(
        status="SOURCE_ERROR", stage=stage, error_category=category,
        error_detail=_redact(f"{type(exc).__name__}: {exc}", secret))


def _load_youtube_api_key():
    """env 우선, 없으면 ~/.hermes/.env 파싱. 키는 절대 출력·로그 금지."""
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if key:
        return key
    env_file = hermes_home() / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "YOUTUBE_API_KEY":
                value = value.strip()
                if value:
                    return value
    except OSError:
        pass
    raise RuntimeError("youtube_api_key_missing")


def _discovery_url(key):
    query = urllib.parse.urlencode({
        "part": "snippet,contentDetails", "playlistId": PLAYLIST,
        "maxResults": 10, "key": key})
    return f"{DISCOVERY_API_URL}?{query}"


def _optional_object(container, name, error):
    """container[name]이 없으면 {} , 있으면 반드시 object여야 한다."""
    value = container.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(error)
    return value


def _parse_discovery(json_bytes):
    """playlistItems 응답 → RSS 시절과 동일한 entry dict 목록.

    container shape(payload/items/item/snippet/resourceId/contentDetails)는 명시 검증해
    비정상 payload를 전부 ValueError로 수렴시킨다 — 호출자가 이를
    SOURCE_ERROR(discovery, bad_payload)로 분류한다. 반면 shape가 유효한
    item의 videoId/videoPublishedAt 누락은 그 item만 건너뛴다.
    snippet.publishedAt은 목록 추가시각이므로 영상 게시시각을 대체하지 않는다.
    """
    payload = json.loads(json_bytes)
    if not isinstance(payload, dict):
        raise ValueError("discovery_payload_not_object")
    items = payload.get("items", [])
    if not isinstance(items, list):  # dict/scalar/string/null 전부 비정상
        raise ValueError("discovery_items_not_array")
    entries = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("discovery_item_not_object")
        snippet = _optional_object(item, "snippet", "discovery_snippet_not_object")
        resource = _optional_object(snippet, "resourceId",
                                    "discovery_resource_id_not_object")
        details = _optional_object(item, "contentDetails",
                                   "discovery_content_details_not_object")
        vid = resource.get("videoId")
        published = details.get("videoPublishedAt")
        if not isinstance(vid, str) or not isinstance(published, str):
            continue  # 누락·비문자열 leaf는 그 item만 건너뛴다(기존 의미)
        if not vid or not published:
            continue
        title = snippet.get("title")
        entries.append({"id": vid,
                        "title": title if isinstance(title, str) else "",
                        "published": published})
    return entries


def _today_marker_entries(entries, today):
    """entry.published를 aware KST date로 환산해 오늘+제목 마커만 남긴다."""
    out = []
    for e in entries:
        try:
            published = datetime.fromisoformat(e["published"].replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if published.tzinfo is None or published.utcoffset() is None:
            continue
        if published.astimezone(SEOUL).date() == today and TITLE_MARKER in e["title"]:
            out.append(e)
    return out


def _today_entries(entries, today):
    out = []
    for e in entries:
        try:
            published = datetime.fromisoformat(e["published"].replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if published.tzinfo is None or published.utcoffset() is None:
            continue
        if published.astimezone(SEOUL).date() == today:
            out.append(e)
    return out


def observe(fetcher, now=None, already_collected=False):
    """한 사이클의 순수 관측·분류. 네트워크는 fetcher 주입, 내부 retry 없음.

    discovery는 Data API playlistItems(지정 프로그램 재생목록) 한 번뿐이다.
    """
    if already_collected:
        return Observation(status="ALREADY_COLLECTED")
    today = kst_date(now)
    # key 로드는 별도 경계다: 실패 시 URL도 없고 redact할 secret도 없다.
    try:
        api_key = _load_youtube_api_key()
    except Exception as exc:  # noqa: BLE001 - 키 부재도 typed discovery 오류
        return _classify_error(exc, "discovery")
    try:
        entries = _parse_discovery(fetcher.fetch(_discovery_url(api_key)))
    except Exception as exc:  # noqa: BLE001 - 분류 후 typed 상태로 반환
        # 예외 문자열에 요청 URL(=key)이 실려올 수 있다 — detail에서 지운다.
        return _classify_error(exc, "discovery", secret=api_key)
    today_any = _today_entries(entries, today)
    marker = _today_marker_entries(entries, today)
    if not marker:
        status = ("TODAY_VIDEO_TITLE_MISMATCH" if today_any
                  else "TODAY_VIDEO_NOT_FOUND")
        return Observation(status=status, candidates=tuple(today_any))
    entry = max(marker, key=lambda x: datetime.fromisoformat(
        x["published"].replace("Z", "+00:00")))
    vid = entry["id"]
    try:
        track = caption_track(vid, fetcher)
    except Exception as exc:  # noqa: BLE001
        return _classify_error(exc, "player")
    if not track or "baseUrl" not in track:
        return Observation(status="VIDEO_FOUND_CAPTIONS_PENDING",
                           video_id=vid, title=entry["title"],
                           published=entry["published"])
    try:
        text = transcript(track["baseUrl"], fetcher)
    except Exception as exc:  # noqa: BLE001
        return _classify_error(exc, "timedtext")
    if len(text) < TRANSCRIPT_MIN_CHARS:
        return Observation(status="CAPTION_TRACK_FOUND_TRANSCRIPT_PENDING",
                           video_id=vid, title=entry["title"],
                           published=entry["published"])
    return Observation(status="TRANSCRIPT_READY", video_id=vid,
                       title=entry["title"], published=entry["published"],
                       transcript=text, **_track_provenance(track))


def caption_track(video_id, fetcher=None):
    fetcher = fetcher or NetworkFetcher()
    body = json.dumps({
        "context": {"client": {"clientName": "ANDROID",
                               "clientVersion": "20.10.38",
                               "androidSdkVersion": 30}},
        "videoId": video_id}).encode()
    d = json.loads(fetcher.fetch("https://www.youtube.com/youtubei/v1/player",
                                 data=body,
                                 headers={"Content-Type": "application/json"}))
    tracks = (d.get("captions", {})
               .get("playerCaptionsTracklistRenderer", {})
               .get("captionTracks") or [])
    ko = [t for t in tracks if t.get("languageCode") == "ko"]
    return (ko or tracks or [None])[0]


def _track_provenance(track):
    """실제 선택된 caption track에서 관측된 provenance만 원문 그대로 취한다.

    innertube track의 kind는 ASR track에만 존재하고("asr") manual track은
    field 자체가 없으므로 부재는 typed null이 진실이다 — 언어에서 kind를
    추론하거나 상수를 날조하지 않는다. baseUrl은 서명 파라미터가 실리므로
    절대 정체성/메타에 싣지 않는다."""
    out = {}
    for field, key in (("caption_language", "languageCode"),
                       ("caption_kind", "kind"),
                       ("caption_vss_id", "vssId")):
        value = track.get(key)
        out[field] = value if isinstance(value, str) and value else None
    return out


def transcript(base_url, fetcher=None):
    fetcher = fetcher or NetworkFetcher()
    root = ET.fromstring(fetcher.fetch(base_url))
    out = []
    for p in root.iter("p"):
        sec = int(p.get("t", 0)) // 1000
        txt = "".join(s.text or "" for s in p.iter("s")).strip()
        if txt:
            out.append(f"[{sec // 60:02d}:{sec % 60:02d}] {txt}")
    return "\n".join(out)


class ArchiveConflict(Exception):
    """아카이브 쌍을 신뢰/발행할 수 없다 — 호출자는 COLLECTED를 선언하지
    않고 typed 알림으로 fail-closed한다."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


# 아카이브 메타 canonical 정체성 (2026-08-21 재폐쇄 리뷰 MATERIAL + 재재리뷰
# MATERIAL 1): 현재 fetch pipeline이 실제로 생산하는 source field 전부에
# 실제 관측된 caption provenance를 더한 것이 정체성이다 — video_id+해시
# 부분집합만 비교하면 같은 자막의 stale/충돌 provenance가 수용되고,
# provenance를 상수로 날조하면 fallback 비한국어/manual track의 정체성이
# 거짓이 된다. 기존-meta 수용·같은 attempt 수리·replay·최종 read-back은
# 전부 단일 helper _validate_archive_meta 하나로 판정한다.
ARCHIVE_META_SCHEMA_VERSION = 2
EXTRACTION_ID = "innertube-android-srv3-v1"
# 구 live writer(pre-v2)가 관측 없이 상수로 기록한 caption_kind 표식 — 새
# meta는 절대 이 값을 생산하지 않으며, genuine legacy 판정과 migration
# origin 증거 보존에만 쓴다.
LEGACY_CAPTION_KIND = "ko-asr"
LEGACY_META_FIELDS = frozenset({
    "video_id", "url", "title", "published", "playlist_id", "channel_id",
    "fetched_at", "transcript_sha256", "extraction", "caption_kind"})
MIGRATION_ORIGIN = {"schema": "pre-v2-12man-meta", "version": 1}
ARCHIVE_IDENTITY_FIELDS = (
    "meta_schema_version", "video_id", "url", "title", "published",
    "playlist_id", "channel_id", "transcript_sha256", "extraction",
    "caption_language", "caption_kind", "caption_vss_id")


def archive_identity(video_id, title, published, transcript_sha256, *,
                     caption_language=None, caption_kind=None,
                     caption_vss_id=None):
    """canonical 불변 정체성 dict. published는 aware ISO 정책(naive 거부).

    수집(현재 관측)과 replay(영속 state)가 같은 인자 형태로 이 하나를
    호출한다. 존재하지 않는 authority는 날조하지 않는다 — playlist/channel/
    extraction은 이 스크립트가 쓰는 고정 소스 정체성이고, caption
    provenance는 실제 선택된 track에서 관측된 값 또는 typed null뿐이다."""
    if not (isinstance(video_id, str) and video_id):
        raise ValueError("identity_video_id")
    if not isinstance(title, str):
        raise ValueError("identity_title")
    if not (isinstance(transcript_sha256, str) and transcript_sha256):
        raise ValueError("identity_transcript_sha256")
    if not isinstance(published, str):
        raise ValueError("identity_published")
    _parse_aware_iso(published)
    for value in (caption_language, caption_kind, caption_vss_id):
        if value is not None and not (isinstance(value, str) and value):
            raise ValueError("identity_caption_provenance")
    return {
        "meta_schema_version": ARCHIVE_META_SCHEMA_VERSION,
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "published": published,
        "playlist_id": PLAYLIST,
        "channel_id": CHANNEL_ID,
        "transcript_sha256": transcript_sha256,
        "extraction": EXTRACTION_ID,
        "caption_language": caption_language,
        "caption_kind": caption_kind,
        "caption_vss_id": caption_vss_id,
    }


def identity_digest(identity):
    """결정적 canonical 직렬화(정렬 key + 고정 separator) 위 SHA-256 —
    입력 dict의 key 삽입 순서와 무관하다."""
    basis = json.dumps({k: identity[k] for k in ARCHIVE_IDENTITY_FIELDS},
                       ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _is_genuine_legacy_meta(meta, identity):
    """구 live writer가 실제로 남긴 pre-v2 meta인지 — 정확한 key 집합,
    저장 source field 전부 일치(canonical URL 포함), writer 상수 표식
    caption_kind, aware fetched_at. 그 외 형태는 전부 위조/변형으로 본다."""
    if set(meta) != LEGACY_META_FIELDS:
        return False
    for field in ("video_id", "url", "title", "published", "playlist_id",
                  "channel_id", "transcript_sha256", "extraction"):
        if meta[field] != identity[field]:
            return False
    if meta["caption_kind"] != LEGACY_CAPTION_KIND:
        return False
    fetched = meta["fetched_at"]
    if not (isinstance(fetched, str) and fetched):
        return False
    try:
        _parse_aware_iso(fetched)
    except ValueError:
        return False
    return True


def _validate_archive_meta(meta, identity):
    """단일 메타 판정 지점 — "ok" | "legacy" | "conflict".

    "ok"      v2 정체성 전 field + identity_digest 완전 일치.
    "legacy"  digest 도입(v2) 전 구 live writer의 진짜 meta일 때만
              (_is_genuine_legacy_meta). 호출자는 이 verdict를 그대로
              수용하지 못한다 — 즉시 1회 migration(수집 attempt 또는
              tuple 증명된 replay)하거나 fail-closed한다. 무기한 legacy
              수용 금지.
    "conflict" 그 외 전부(field 불일치, malformed/missing/orphan digest,
              비-dict, 위조 legacy) — 호출자는 fail-closed한다."""
    if not isinstance(meta, dict):
        return "conflict"
    version = meta.get("meta_schema_version")
    stored = meta.get("identity_digest")
    if version is None and stored is None:
        return "legacy" if _is_genuine_legacy_meta(meta, identity) else "conflict"
    for field in ARCHIVE_IDENTITY_FIELDS:
        if field == "meta_schema_version":
            continue  # version은 digest와 함께 아래에서 판정
        if meta.get(field) != identity[field]:
            return "conflict"
    if version != ARCHIVE_META_SCHEMA_VERSION:
        return "conflict"
    if stored != identity_digest(identity):
        return "conflict"
    return "ok"


def _migration_evidence(legacy_meta):
    """legacy→v2 승격의 명시 증거 — 정체성 밖(비정체성·휘발) field.
    구 writer가 관측 없이 기록한 caption_kind 상수 주장은 버리지 않고
    origin에 사실 그대로 보존한다(관측값으로 승격하지는 않는다)."""
    return {
        "migration_origin": {**MIGRATION_ORIGIN,
                             "legacy_caption_kind": legacy_meta["caption_kind"]},
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }


def _archive_meta_bytes(identity, fetched_at, migration=None):
    """발행할 meta 바이트 — 정체성 전 field + fetched_at + digest.
    migration은 legacy 승격 증거(비정체성 field)로 digest에 들어가지 않는다."""
    payload = {**identity, "fetched_at": fetched_at}
    if migration:
        payload.update(migration)
    payload["identity_digest"] = identity_digest(identity)
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def _fsync_dir(directory):
    try:
        dfd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _publish_bytes(path, data):
    """same-dir temp 0600 + fsync(file/dir) + 원자 no-follow 발행."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _read_regular_file(path, missing_category, conflict_category,
                       unreadable_category):
    """(bytes|None, category|None). O_NOFOLLOW로 열어 fd 기준으로 정규
    단일링크만 신뢰한다 — symlink/hardlink/특수파일 교체 공격 fail-closed."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, missing_category
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return None, conflict_category  # symlink 목적지
        return None, unreadable_category
    try:
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                return None, conflict_category
            return f.read(), None
    except OSError:
        return None, unreadable_category


def _archive_target_error(path):
    """발행 목적지 사전 검증: ENOENT는 정상, 그 외엔 정규 단일링크만."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return "archive_unreadable"
    if stat.S_ISLNK(st.st_mode):
        return "archive_symlink"
    if not stat.S_ISREG(st.st_mode):
        return "archive_not_regular"
    if st.st_nlink > 1:
        return "archive_hardlink"
    return None


def archive(entry, text):
    """자막 전문 + provenance 사이드카를 crash-safe 원자 쌍으로 남긴다.

    호출자는 job_lock 보유. append-only: 기존 유효 반쪽은 재작성하지 않고,
    partial 쌍만 같은 attempt의 in-memory payload로 결정적으로 수리한다.
    두 파일을 read-back해 해시가 일치해야만 성공 반환 — 그때만 호출자가
    COLLECTED를 영속한다. 그 외 전부 typed ArchiveConflict fail-closed.
    반환값은 legacy meta migration이 일어났는지 여부다."""
    try:
        return _archive_pair(entry, text)
    except ArchiveConflict:
        raise
    except OSError as exc:
        raise ArchiveConflict("archive_io") from exc


def _archive_pair(entry, text):
    INBOX = inbox_path()
    INBOX.mkdir(mode=0o700, parents=True, exist_ok=True)
    stem = f"{entry['published'][:10]}_{entry['id']}"
    txt_path = INBOX / f"{stem}.txt"
    meta_path = INBOX / f"{stem}.meta.json"
    digest = hashlib.sha256(text.encode()).hexdigest()
    try:
        identity = archive_identity(
            entry["id"], entry["title"], entry["published"], digest,
            caption_language=entry.get("caption_language"),
            caption_kind=entry.get("caption_kind"),
            caption_vss_id=entry.get("caption_vss_id"))
    except ValueError:
        raise ArchiveConflict("archive_identity")
    txt_bytes = (text + "\n").encode("utf-8")

    for path in (txt_path, meta_path):
        category = _archive_target_error(path)
        if category:
            raise ArchiveConflict(category)

    # transcript 반쪽: 있으면 바이트 일치 필수(append-only), 없으면 발행.
    existing, err = _read_regular_file(
        txt_path, "archive_missing", "archive_conflict", "archive_unreadable")
    if err == "archive_missing":
        _publish_bytes(txt_path, txt_bytes)
    elif err is not None:
        raise ArchiveConflict(err)
    elif existing != txt_bytes:
        raise ArchiveConflict("archive_conflict")
    else:
        os.chmod(txt_path, 0o600)  # crash 잔재 모드 수리

    # meta 반쪽: canonical 정체성 전 field 일치("ok") → 보존, torn write
    # 잔재(비파싱) → 같은 attempt 정체성으로 결정적 재발행, legacy(pre-v2,
    # digest 부재) → 전 source field 정확 일치일 때만 1회 migration, 그 외
    # 어떤 field 불일치·malformed digest도 전부 conflict fail-closed.
    raw, err = _read_regular_file(
        meta_path, "archive_missing", "archive_conflict", "archive_unreadable")
    publish_meta = err == "archive_missing"
    if err is not None and err != "archive_missing":
        raise ArchiveConflict(err)
    fetched_at = datetime.now(timezone.utc).isoformat()
    migration = None
    migrated = False
    if not publish_meta:
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            publish_meta = True
        else:
            verdict = _validate_archive_meta(meta, identity)
            if verdict == "conflict":
                raise ArchiveConflict("archive_conflict")
            if verdict == "legacy":
                # bounded 1회 migration(호출자 job_lock 보유): genuine legacy
                # source 값 전부가 현재 관측·자막 해시와 정확 일치함이 증명된
                # 경우만 여기 도달한다. 현재 관측 provenance의 정체성으로
                # digest+migration 증거를 증강해 원자 재발행하며, fetched_at은
                # 원본 관측 기록이라 보존한다(genuine 판정이 aware str 보증).
                fetched_at = meta["fetched_at"]
                migration = _migration_evidence(meta)
                publish_meta = True
                migrated = True
            else:
                os.chmod(meta_path, 0o600)  # crash 잔재 모드 수리
    if publish_meta:
        _publish_bytes(meta_path,
                       _archive_meta_bytes(identity, fetched_at, migration))

    # read-back 검증: 두 파일이 디스크에서 그대로 읽히고 transcript 바이트와
    # meta canonical 정체성 전체(digest 포함)가 일치해야 '수집 완료'다 — 이
    # 검증을 통과해야만 호출자가 COLLECTED를 영속한다.
    back_txt, err = _read_regular_file(
        txt_path, "archive_conflict", "archive_conflict", "archive_unreadable")
    if err is not None or back_txt != txt_bytes:
        raise ArchiveConflict(err or "archive_conflict")
    back_meta, err = _read_regular_file(
        meta_path, "archive_conflict", "archive_conflict", "archive_unreadable")
    if err is not None:
        raise ArchiveConflict(err)
    try:
        parsed = json.loads(back_meta.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ArchiveConflict("archive_conflict")
    if _validate_archive_meta(parsed, identity) != "ok":
        raise ArchiveConflict("archive_conflict")
    return migrated


def _atomic_write_json(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    _publish_bytes(path, data)


def delivered_today(now=None):
    """Return True once a transcript was delivered for the current KST date.

    legacy `last_delivered.json` reader compatibility: 파일 포맷({id, title,
    published})은 그대로 유지하며 의미만 '오늘 수집 완료'로 쓴다. 이 guard는
    YouTube 조회 전에 동작하므로 성공일의 나머지 15분 틱은 network 0 no-op다.
    """
    return collected_today(now)


def collected_today(now=None):
    state = state_path()
    if not state.exists():
        return False
    try:
        prev = json.loads(state.read_text())
        published = datetime.fromisoformat(prev["published"].replace("Z", "+00:00"))
        if published.tzinfo is None or published.utcoffset() is None:
            return False
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    observed = _aware(now)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("aware_now_required")
    return published.astimezone(SEOUL).date() == observed.astimezone(SEOUL).date()


# ---------------------------------------------------------------------------
# collection lifecycle state (schema v1) — 수집/분석/전달 분리
# ---------------------------------------------------------------------------

STATE_SCHEMA_VERSION = 1
LIFECYCLE_STATUSES = ("COLLECTED", "ANALYZED", "DELIVERED")
MAX_ATTEMPT_RECEIPTS = 20


def _source_revision(video_id, published, transcript_sha256):
    """exactly-once 정체성: video_id + 게시 시각 + 자막 내용 해시."""
    basis = f"{video_id}|{published}|{transcript_sha256 or ''}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _parse_aware_iso(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("aware_timestamp_required")
    return parsed


def _validate_state(data):
    """schema v1 검증 + 정규화. 위반은 전부 None(corrupt 취급)."""
    if not isinstance(data, dict) or data.get("schema_version") != STATE_SCHEMA_VERSION:
        return None
    if data.get("status") not in LIFECYCLE_STATUSES:
        return None
    video_id = data.get("video_id")
    revision = data.get("source_revision")
    published = data.get("published")
    if not (isinstance(video_id, str) and video_id):
        return None
    if not (isinstance(revision, str) and revision):
        return None
    if not (isinstance(published, str) and published):
        return None
    try:
        _parse_aware_iso(published)
        for field in ("collected_at", "analyzed_at", "delivered_at",
                      "archive_meta_migrated_at"):
            if data.get(field) is not None:
                _parse_aware_iso(data[field])
    except ValueError:
        return None
    sha = data.get("transcript_sha256")
    if sha is not None and not (isinstance(sha, str) and sha):
        return None
    caption = {}
    for field in ("caption_language", "caption_kind", "caption_vss_id"):
        value = data.get(field)
        if value is not None and not (isinstance(value, str) and value):
            return None
        caption[field] = value
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        return None
    title = data.get("title")
    stem = data.get("archive_stem")
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "video_id": video_id,
        "title": title if isinstance(title, str) else "",
        "published": published,
        "source_revision": revision,
        "transcript_sha256": sha,
        **caption,
        "archive_stem": (stem if isinstance(stem, str) and stem
                         else f"{published[:10]}_{video_id}"),
        "status": data["status"],
        "collected_at": data.get("collected_at"),
        "analyzed_at": data.get("analyzed_at"),
        "delivered_at": data.get("delivered_at"),
        "archive_meta_migrated_at": data.get("archive_meta_migrated_at"),
        "migrated_from_legacy": bool(data.get("migrated_from_legacy")),
        "attempts": attempts[-MAX_ATTEMPT_RECEIPTS:],
    }


def _read_v1_state():
    """(state|None, corrupt|None). ENOENT만 부재(None, None)다.

    그 외 모든 이상 — symlink/hardlink/비정규 파일/그룹·타인 노출 모드/
    읽기 실패/JSON 위반/schema 위반 — 은 typed CORRUPT이며 호출자는
    fail-closed한다(legacy migration/discovery/payload 금지, 기존 바이트
    보존: 이 함수는 절대 쓰지 않는다)."""
    path = collection_state_path()
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return None, "state_symlink"
        return None, "state_unreadable"
    try:
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None, "state_not_regular"
            if st.st_nlink > 1:
                return None, "state_hardlink"
            if stat.S_IMODE(st.st_mode) & 0o077:
                return None, "state_mode"  # 0600 계약 위반 = 무결성 불신
            raw = f.read()
    except OSError:
        return None, "state_unreadable"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, "state_corrupt"
    validated = _validate_state(data)
    if validated is None:
        return None, "state_schema"
    return validated, None


def _legacy_receipt(now):
    """legacy last_delivered.json이 오늘 수집을 주장하면 COLLECTED 초안으로
    migration한다. 분석/전달을 관측한 적 없으므로 절대 그 이상을 선언하지
    않는다(collected_at은 미상 → None)."""
    try:
        prev = json.loads(state_path().read_text(encoding="utf-8"))
        published_raw = prev["published"]
        published = _parse_aware_iso(published_raw)
        video_id = prev["id"]
    except (OSError, UnicodeError, KeyError, TypeError, ValueError,
            json.JSONDecodeError):
        return None
    if not (isinstance(video_id, str) and video_id):
        return None
    if published.astimezone(SEOUL).date() != kst_date(now):
        return None
    stem = f"{published_raw[:10]}_{video_id}"
    sha = None
    caption = {"caption_language": None, "caption_kind": None,
               "caption_vss_id": None}
    try:
        meta = json.loads((inbox_path() / f"{stem}.meta.json").read_text(
            encoding="utf-8"))
        if isinstance(meta, dict) and meta.get("video_id") == video_id:
            candidate = meta.get("transcript_sha256")
            if isinstance(candidate, str) and candidate:
                sha = candidate
            # caption provenance 관측 기록은 v2 meta에만 있다 — pre-v2의
            # caption_kind는 writer 상수 주장이라 state로 승격하지 않는다.
            if meta.get("meta_schema_version") == ARCHIVE_META_SCHEMA_VERSION:
                for field in caption:
                    value = meta.get(field)
                    if isinstance(value, str) and value:
                        caption[field] = value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    title = prev.get("title")
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "video_id": video_id,
        "title": title if isinstance(title, str) else "",
        "published": published_raw,
        "source_revision": _source_revision(video_id, published_raw, sha),
        "transcript_sha256": sha,
        **caption,
        "archive_stem": stem,
        "status": "COLLECTED",
        "collected_at": None,
        "analyzed_at": None,
        "delivered_at": None,
        "archive_meta_migrated_at": None,
        "migrated_from_legacy": True,
        "attempts": [],
    }


def _state_is_today(state, now):
    try:
        published = _parse_aware_iso(state["published"])
    except ValueError:
        return False
    return published.astimezone(SEOUL).date() == kst_date(now)


def load_collection_state(now):
    """(오늘의 state|None, corrupt|None, origin).

    origin: "v1"(디스크 canonical) | "legacy"(ENOENT에서만의 migration 초안)
    | None. corrupt면 fail-closed — legacy migration/discovery 진입 금지.
    유효하지만 오늘이 아닌 v1은 corrupt가 아니라 정상 rollover(discovery
    허용)이며, 이때도 legacy migration은 하지 않는다(ENOENT 전용)."""
    state, corrupt = _read_v1_state()
    if corrupt:
        return None, corrupt, None
    if state is not None:
        if _state_is_today(state, now):
            return state, None, "v1"
        return None, None, None
    legacy = _legacy_receipt(now)
    if legacy is not None:
        return legacy, None, "legacy"
    return None, None, None


_STATUS_RANK = {"COLLECTED": 0, "ANALYZED": 1, "DELIVERED": 2}


def _cas_persist(state, prior_status):
    """job_lock 보유 전제의 CAS 영속. 디스크를 재독해 정확히
    (video_id, source_revision, prior_status)일 때만 교체한다.

    prior_status None = '디스크에 같은 정체성 선행 상태가 없다'(fresh
    collect/legacy migration). 반환 "written"|"stale". "stale"은 무변이 —
    디스크가 이미 더 높은 status로 진행됐거나 기대 선행 상태가 사라진
    경우로, replay attempt 갱신이 ANALYZED/DELIVERED를 절대 되돌리지
    못하게 하는 단조성 장치다. corrupt/IO 실패는 OSError."""
    disk, corrupt = _read_v1_state()
    if corrupt:
        raise OSError(corrupt)
    new_rank = _STATUS_RANK[state["status"]]
    if (disk is not None and disk["video_id"] == state["video_id"]
            and disk["source_revision"] == state["source_revision"]):
        if _STATUS_RANK[disk["status"]] > new_rank:
            return "stale"
        if prior_status is None:
            if _STATUS_RANK[disk["status"]] > 0:
                return "stale"  # fresh write가 진행 상태를 덮지 않는다
        elif disk["status"] != prior_status:
            return "stale"
    elif prior_status is not None:
        return "stale"  # 기대한 선행 상태가 디스크에서 사라졌다/교체됐다
    _atomic_write_json(collection_state_path(), state)
    return "written"


def _persist_state_best_effort(state, prior_status):
    """"written"|"stale"|"io_error". IO 실패가 tick을 죽이지 않는다(다음
    tick 재시도) — 단 "stale"은 반드시 무출력으로 존중해야 한다."""
    try:
        return _cas_persist(state, prior_status)
    except OSError:
        return "io_error"


def _record_attempt(state, now, kind, outcome, error=None):
    receipt = {"at": _aware(now).isoformat(), "kind": kind, "outcome": outcome}
    if error:
        receipt["error"] = error
    state["attempts"] = (list(state.get("attempts") or [])
                         + [receipt])[-MAX_ATTEMPT_RECEIPTS:]


# ---------------------------------------------------------------------------
# 분석 입력 상한 (report v3 계약 §8) — 실측 표본에서 유도한 bounded 예산
# ---------------------------------------------------------------------------
# 2026-08-27 실패 run(cron e7254690397b, 13:58:50)의 실측치:
#   방송 약 74분 → 이 스크립트 stdout payload 99,407 bytes, 그 위에 고정
#   cron prompt+skill overhead 13,933 bytes를 더한 agent 입력 기록
#   113,340 bytes. 그 run은 gateway 비스트리밍 stale threshold 90s에 걸려
#   FAILED로 끝났고 리포트는 전달되지 않았다.
# 계약 §8은 지원 상한을 약 2시간으로, 회귀시험 하한을 그 표본의 정확히
# 2배로 못박는다. 그래서 상한을 임의로 키우지 않고 표본에서 유도한다:
#   ANALYSIS_INPUT_CAP_BYTES = 2 × 113,340 = 226,680 stdout payload bytes.
# 실측 밀도(113,340 bytes ≈ 74분)로 환산하면 약 148분 분량이라 120분 지원
# 상한 대비 약 23% 여유이며, §8의 2배 회귀 입력을 경계 포함(≤)으로 admit
# 한다. 초과 입력은 자르거나 성공으로 기록하지 않고 typed로 거절한다.
LONGFORM_SAMPLE_INPUT_BYTES = 113_340
ANALYSIS_INPUT_CAP_BYTES = 2 * LONGFORM_SAMPLE_INPUT_BYTES
INPUT_CAP_EXCEEDED = "INPUT_CAP_EXCEEDED"


def analysis_input_refusal(payload):
    """상한 초과면 typed 거절 category, 아니면 None. 절대 자르지 않는다.

    수집(COLLECTED)은 이미 성립했고 분석만 시작할 수 없는 상태이므로
    호출자는 아카이브·수집 영수증을 보존한 채 payload만 방출하지 않는다.
    """
    if len(payload.encode("utf-8")) > ANALYSIS_INPUT_CAP_BYTES:
        return INPUT_CAP_EXCEEDED
    return None


UNRESOLVED_LIFECYCLE = "COLLECTED_NOT_ANALYZED"


def unresolved_gate_line(category):
    """수집됐지만 이번 tick에 분석으로 넘기지 못한 상태의 stdout 계약 한 줄.

    payload 대신 나가므로 자막은 한 byte도 방출되지 않고, cron wake gate
    (`wakeAgent:false`)라 agent도 깨우지 않는다 — 토큰·전달 0은 그대로다.
    동시에 scheduler가 이 무음 tick을 'ok'로 기록해 앞선 COLLECTED &&
    !ANALYZED typed 실패를 지우지 못하게 하는 근거가 된다(진실한 top-level
    status). 자막·경로·API 키·사용자명은 싣지 않고 typed category만 싣는다.
    """
    return json.dumps({"wakeAgent": False, "unresolved": True,
                       "detail": f"{UNRESOLVED_LIFECYCLE}:{category}"},
                      separators=(",", ":"))


def transcript_payload(video_id, title, published, transcript):
    """수집·replay가 공유하는 확정 stdout 계약 — byte 동일해야 admission
    후보 replay가 수렴한다."""
    return ("STATUS: NEW_TRANSCRIPT\n"
            f"VIDEO: https://www.youtube.com/watch?v={video_id}\n"
            f"TITLE: {title}\n"
            f"PUBLISHED: {published}\n"
            "---TRANSCRIPT---\n"
            f"{transcript}")


def _read_archive_transcript(state):
    """(transcript|None, error_category|None, migrated). 같은 파일 재사용 전
    무결성 검증: 정규 단일링크 비symlink 파일이어야 하고, meta는 state가
    주장하는 canonical 정체성 전체와 단일 helper(_validate_archive_meta)로
    일치해야 한다 — video_id+sha 부분집합 비교 금지. digest-less legacy
    (pre-v2)는 무기한 수용하지 않는다: 저장 tuple이 스스로 증명될 때만
    _migrate_legacy_archive로 v2 승격한 뒤 쓰고(migrated=True), 정상 운영에서는
    상태 receipt로 재승격을 막는다. 다만 meta 교체 뒤 state receipt 전 crash와
    운영자 수동 legacy rollback이 결합된 경우에는 같은 검증을 거쳐 idempotent
    재승격할 수 있다. 이미 승격 기록이 영속된 정체성에 legacy가 재등장하면
    typed fail-closed다.
    재조회/날조 없음."""
    stem = state["archive_stem"]
    txt_path = inbox_path() / f"{stem}.txt"
    meta_path = inbox_path() / f"{stem}.meta.json"
    raw_bytes, err = _read_regular_file(
        txt_path, "archive_missing", "archive_conflict", "archive_unreadable")
    if err is not None:
        return None, err, False
    meta_bytes, err = _read_regular_file(
        meta_path, "archive_missing", "archive_conflict", "archive_unreadable")
    if err is not None:
        return None, err, False
    try:
        raw = raw_bytes.decode("utf-8")
        meta = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, "archive_unreadable", False
    text = raw[:-1] if raw.endswith("\n") else raw  # archive()가 붙인 개행 제거
    digest = hashlib.sha256(text.encode()).hexdigest()
    if state["transcript_sha256"] is not None and state["transcript_sha256"] != digest:
        return None, "archive_conflict", False
    try:
        identity = archive_identity(
            state["video_id"], state["title"], state["published"], digest,
            caption_language=state["caption_language"],
            caption_kind=state["caption_kind"],
            caption_vss_id=state["caption_vss_id"])
    except ValueError:
        return None, "archive_conflict", False
    verdict = _validate_archive_meta(meta, identity)
    if verdict == "conflict":
        return None, "archive_conflict", False
    if len(text) < TRANSCRIPT_MIN_CHARS:
        return None, "archive_conflict", False
    if verdict == "legacy":
        if state["archive_meta_migrated_at"] is not None:
            # 이미 1회 승격된 정체성에 digest-less legacy 재등장 — 되돌림/
            # 위조다. payload 근거로 재수용하지 않는다.
            return None, "archive_legacy_reverted", False
        err = _migrate_legacy_archive(state, identity, meta, txt_path,
                                      meta_path, digest)
        if err is not None:
            return None, err, False
        return text, None, True
    return text, None, False


def _migrate_legacy_archive(state, identity, meta, txt_path, meta_path, digest):
    """digest-less legacy meta의 replay-시 idempotent 로컬 migration.

    호출자는 job_lock 보유·meta는 genuine legacy 판정을 통과했다. 현재
    관측이 없으므로 재조회/날조 없이 저장 tuple 스스로의 일관성 — state의
    해시·revision·경로 정체성 재검증, 두 파일 모두 정규 0600 단일링크 —
    이 증명될 때만 저장 불변값 그대로(관측 없던 caption field는 명시 null)
    digest+migration 증거를 증강해 원자 재발행하고 read-back으로 검증한다.
    실패는 전부 typed error 반환(payload 금지·기존 바이트 보존)이며,
    replace 전 crash는 다음 tick이 같은 결정으로 완료하고 replace 후
    crash는 다음 tick이 유효한 v2를 본다. None 반환이 성공이다."""
    if state["transcript_sha256"] != digest:  # None 포함 — 저장 해시 증거 필수
        return "archive_legacy_state"
    if state["source_revision"] != _source_revision(
            state["video_id"], state["published"], digest):
        return "archive_legacy_state"
    if state["archive_stem"] != f"{state['published'][:10]}_{state['video_id']}":
        return "archive_legacy_state"
    for path in (txt_path, meta_path):
        try:
            st = os.lstat(path)
        except OSError:
            return "archive_unreadable"
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink > 1
                or stat.S_IMODE(st.st_mode) != 0o600):
            return "archive_legacy_mode"
    try:
        _publish_bytes(meta_path,
                       _archive_meta_bytes(identity, meta["fetched_at"],
                                           _migration_evidence(meta)))
    except OSError:
        return "archive_io"
    back, err = _read_regular_file(meta_path, "archive_conflict",
                                   "archive_conflict", "archive_unreadable")
    if err is not None:
        return err
    try:
        parsed = json.loads(back.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return "archive_conflict"
    if _validate_archive_meta(parsed, identity) != "ok":
        return "archive_conflict"
    return None


DEFAULT_MODEL_FALLBACK_POLICY = {
    "schema_version": 1,
    "enabled": False,
    "approved_models": (),
    "max_alternates": 1,
    "require_fresh_quota_route": True,
    "allow_parallel_duplicate": False,
}


def load_model_fallback_policy():
    """모델 fallback policy seam — 기본 OFF, fail-closed. 수집기는 이 값을
    소비하지 않으며 리포트 단계가 읽는 계약 지점이다. enabled가 되려면
    명시 opt-in 파일이 전 조건을 만족해야 한다: 승인 모델 정확히 1개,
    대안 최대 1, fresh quota/route 요구 유지, 병렬 중복 금지. 그 외
    (부재·corrupt·schema 불일치·조건 위반)는 전부 disabled 기본값."""
    default = dict(DEFAULT_MODEL_FALLBACK_POLICY)
    try:
        data = json.loads(model_fallback_policy_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return default
    models = data.get("approved_models")
    if not (isinstance(models, list) and len(models) == 1
            and isinstance(models[0], str) and models[0]):
        return default
    if data.get("enabled") is not True:
        return default
    if data.get("max_alternates") != 1:
        return default
    if data.get("require_fresh_quota_route") is not True:
        return default
    if data.get("allow_parallel_duplicate") is not False:
        return default
    return {
        "schema_version": 1,
        "enabled": True,
        "approved_models": tuple(models),
        "max_alternates": 1,
        "require_fresh_quota_route": True,
        "allow_parallel_duplicate": False,
    }


def _acknowledge(kind, video_id, revision, now):
    """ack seam: 'analyzed'|'delivered'|'completed'. (exit_code, receipt_line).

    호출자는 job_lock 보유. 정체성(video_id+source_revision) 불일치·순서
    위반·상태 부재·corrupt는 전부 비零 exit + 무변이. 이미 도달한 상태로의
    ack는 idempotent 성공이며 최초 타임스탬프를 보존한다. 'completed'는
    확인된 전달 후 scheduler가 호출하는 단일 원자 전이(→DELIVERED)다;
    'analyzed'/'delivered'는 수동 진단 전용이다."""
    state, corrupt = _read_v1_state()
    if corrupt:
        return 1, f"ACK_CONFLICT: {corrupt}"
    prior_status = state["status"] if state is not None else None
    if state is None:
        state = _legacy_receipt(now)
    if state is None:
        return 1, "ACK_CONFLICT: state_not_found"
    if state["video_id"] != video_id:
        return 1, "ACK_CONFLICT: video_id_mismatch"
    if state["source_revision"] != revision:
        return 1, "ACK_CONFLICT: source_revision_mismatch"
    rank = _STATUS_RANK[state["status"]]
    stamp = _aware(now).isoformat()
    if kind == "analyzed":
        if rank >= 1:
            return 0, f"ACK_OK: idempotent status={state['status']}"
        state["status"] = "ANALYZED"
        state["analyzed_at"] = stamp
    elif kind == "delivered":
        if rank == 0:
            return 1, "ACK_CONFLICT: not_analyzed"
        if rank >= 2:
            return 0, "ACK_OK: idempotent status=DELIVERED"
        state["status"] = "DELIVERED"
        state["delivered_at"] = stamp
    else:  # completed — 전달 확인 후 단일 원자 전이
        if rank >= 2:
            return 0, "ACK_OK: idempotent status=DELIVERED"
        if state["analyzed_at"] is None:
            state["analyzed_at"] = stamp
        state["status"] = "DELIVERED"
        state["delivered_at"] = stamp
    _record_attempt(state, now, f"ack_{kind}", "ok")
    try:
        outcome = _cas_persist(state, prior_status)
    except OSError as exc:
        return 1, f"ACK_CONFLICT: {exc}"
    if outcome != "written":
        return 1, "ACK_CONFLICT: state_changed"
    return 0, (f"ACK_OK: status={state['status']} video_id={video_id} "
               f"revision={revision}")


def _show_state(now):
    """경계 읽기: 비밀·경로·자막 없는 bounded JSON 한 줄."""
    state, conflict = _read_v1_state()
    if conflict:
        print(json.dumps({"state": "TARGET_CONFLICT", "error": conflict}))
        return 1
    if state is None:
        state = _legacy_receipt(now)
    if state is None:
        print(json.dumps({"state": "ABSENT"}))
        return 0
    attempts = state.get("attempts") or []
    print(json.dumps({
        "schema_version": state["schema_version"],
        "video_id": state["video_id"],
        "source_revision": state["source_revision"],
        "status": state["status"],
        "published": state["published"],
        "collected_at": state["collected_at"],
        "analyzed_at": state["analyzed_at"],
        "delivered_at": state["delivered_at"],
        "migrated_from_legacy": state["migrated_from_legacy"],
        "attempt_count": len(attempts),
        "last_attempt": attempts[-1] if attempts else None,
    }, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# CLI / orchestration
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="tokmo_yt_12man_fetch.py",
        description="'12시에 만나요' 자막 수집 (지정 재생목록 공식 API, "
                    "17:45 KST 자동 종료)",
        add_help=True)
    parser.add_argument(
        "--force-once", action="store_true",
        help="17:45 KST cutoff를 정확히 이 한 invocation만 우회해 1사이클 수행"
             "(내부 retry 없음, 자동 창 재개 없음)")
    parser.add_argument(
        "--ack-completed", metavar="VIDEO_ID", default=None,
        help="확인된 최종 전달 후 단일 원자 전이 →DELIVERED (--revision 필수, "
             "idempotent). Hermes scheduler의 내구 delivery-receipt 통합이 "
             "호출하는 운영 경로 — cron 프롬프트에 넣지 않는다")
    parser.add_argument(
        "--ack-analyzed", metavar="VIDEO_ID", default=None,
        help="[수동 진단 전용 — cron 프롬프트 금지] COLLECTED→ANALYZED 전이"
             "(--revision 필수, idempotent, 정체성 불일치는 conflict)")
    parser.add_argument(
        "--ack-delivered", metavar="VIDEO_ID", default=None,
        help="[수동 진단 전용 — cron 프롬프트 금지] ANALYZED→DELIVERED 전이"
             "(--revision 필수)")
    parser.add_argument(
        "--revision", metavar="SOURCE_REVISION", default=None,
        help="ack 대상 source_revision (--show-state로 조회)")
    parser.add_argument(
        "--show-state", action="store_true",
        help="lifecycle 상태 bounded JSON 출력(비밀/경로/자막 비노출)")
    return parser


def parse_args(argv):
    return build_parser().parse_args(argv)


# 알림 대상 typed 상태 (source error는 별도 임계치 정책)
ALERTABLE_PENDING = frozenset({
    "TODAY_VIDEO_NOT_FOUND",
    "TODAY_VIDEO_TITLE_MISMATCH",
    "VIDEO_FOUND_CAPTIONS_PENDING",
    "CAPTION_TRACK_FOUND_TRANSCRIPT_PENDING",
})


def alert_message(obs, today):
    """전송 본문 — typed 상태 + video/date + (SOURCE_ERROR면 stage·category).

    transcript·secret·상세 오류 원문은 절대 포함하지 않는다.
    """
    head = f"⚠️ 12시에 만나요 수집 대기 ({today.isoformat()})\n상태: {obs.status}"
    if obs.video_id:
        head += f" · video_id={obs.video_id}"
    if obs.status == "TODAY_VIDEO_TITLE_MISMATCH" and obs.candidates:
        # 수동 RCA용 후보 메타데이터 요약(선택은 하지 않는다)
        lines = [f"  후보 {i + 1}: {c['id']} | {c['title'][:80]} | {c['published']}"
                 for i, c in enumerate(obs.candidates[:3])]
        head += "\n오늘 게시 영상 중 제목 마커 불일치:\n" + "\n".join(lines)
    if obs.status == "SOURCE_ERROR":
        head += f"\nstage: {obs.stage}\ncategory: {obs.error_category}"
    return head


def load_alert_dedupe(now):
    """alerts_v1.json 읽기 — {schema_version, alerted: {fingerprint: iso_date}}.

    반복 normal polling을 위해 쓰지는 않고, 알림 후보 판정 시에만 읽는다.
    파일·root·alerted의 형태가 기대와 다르면(corruption 포함) 빈 dedupe로
    취급한다. 절대 raise하지 않는다.
    """
    path = alerts_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return {}  # wrong schema — 빈 dedupe로 취급
    alerted = data.get("alerted")
    if not isinstance(alerted, dict):
        return {}  # scalar/list alerted — corruption, 빈 dedupe
    today = kst_date(now).isoformat()
    return {k: v for k, v in alerted.items() if v == today}


def _fingerprint_key(obs, today):
    # dedupe identity = KST date + fingerprint의 전체 성분. SOURCE_ERROR는
    # (status, stage, error_category) 전부, 나머지는 (status, video_id) 전부.
    # 성분이 늘어나도 key에 반영되도록 전체를 순회해 안정적으로 직렬화한다.
    parts = "|".join(str(c) for c in obs.fingerprint)
    return f"{today.isoformat()}|{parts}"


def maybe_alert(obs, now, alerter):
    """typed 상태 알림 1회 + fingerprint dedupe. True면 알림 전송했다.

    - pending 상태: 17:45 KST 최종 자동 관측(또는 그 이후 force-once 관측)에서만.
    - SOURCE_ERROR: 14:30 이후 관측 시 fingerprint별 1회, 동일 반복 silent.
      (새 fingerprint 자체가 '첫 상태전이' 판정이다.)
    """
    today = kst_date(now)
    if obs.status == "SOURCE_ERROR":
        if not should_alert_failure(now):
            return False
    elif obs.status in ALERTABLE_PENDING:
        # 17:45 이전 통상 pending은 무음. 최종 시점 이후 관측만 알림.
        if not should_alert_failure(now):
            return False
        # 최종 자동 시각(17:45) 또는 그 이후 관측만: (hh,mm) >= (17,45)
        local = _aware(now).astimezone(SEOUL)
        if (local.hour, local.minute) < AUTOMATIC_CUTOFF:
            return False
    else:
        return False
    dedupe = load_alert_dedupe(now)
    key = _fingerprint_key(obs, today)
    if key in dedupe:
        return False
    sent = alerter(alert_message(obs, today))
    if sent:
        save_alert_dedupe(now, {**dedupe, key: today.isoformat()})
    return sent


def save_alert_dedupe(now, alerted):
    payload = {
        "schema_version": 1,
        "updated_at": _aware(now).astimezone(SEOUL).isoformat(),
        "alerted": alerted,
    }
    _atomic_write_json(alerts_path(), payload)


def _alert_holiday_unavailable(obs, now, alerter):
    """HOLIDAY_DATA_UNAVAILABLE 알림: 당일 첫 자동 tick에 즉시 1회.

    dedupe는 date+status fingerprint로 alerts_v1에 저장. alerter·dedupe
    저장 예외는 이 경계에서 흡수한다(raw error는 본문에 넣지 않는다).
    전송·저장이 완전히 성공한 경우에만 dedupe가 남아 다음 tick이 silent가
    되고, 그 외에는 다음 tick이 알림을 재시도한다. 어떤 경우에도 collection
    차단(fail-closed)은 유지된다.
    """
    today = kst_date(now)
    # 의도적 초기화: loader가 raise해도 빈 dedupe + 유효 key로 동작한다.
    dedupe = {}
    key = _fingerprint_key(obs, today)
    try:
        dedupe = load_alert_dedupe(now)
        if key in dedupe:
            return False
    except Exception:  # noqa: BLE001 — dedupe 읽기 실패는 빈 취급
        pass
    message = (
        f"⚠️ 12시에 만나요 자동 수집 중단 ({today.isoformat()})\n"
        "상태: 공휴일 정보(KASI)를 신뢰할 수 없어 오늘 '12시에 만나요' "
        "영상을 자동 수집하지 않았습니다."
    )
    try:
        sent = alerter(message)
    except Exception:  # noqa: BLE001 — 알림 채널 예외 흡수
        return False
    if not sent:
        return False
    try:
        save_alert_dedupe(now, {**dedupe, key: today.isoformat()})
    except Exception:  # noqa: BLE001 — 저장 실패 시 다음 tick 재알림 허용
        pass
    return True


def _send_alert_via_system_bot(message):
    import subprocess
    delivered = subprocess.run(
        [str(hermes_home() / "scripts" / "send_system_alert.py"), message],
        capture_output=True, text=True, timeout=20,
    )
    return delivered.returncode == 0


def _replay_tick(state, observed, alert, prior_status):
    """COLLECTED 상태의 로컬 replay — network 0. ANALYZED/DELIVERED면 무음.

    호출자는 job_lock 보유. prior_status는 디스크에서 읽은 선행 상태
    ("COLLECTED") 또는 legacy migration 초안이면 None — 영속은 그
    (video_id, source_revision, prior_status) CAS로만 한다."""
    if state["status"] != "COLLECTED":
        return 0  # 분석 완료 이후 재분석 유발 금지 — silent
    payload = None
    text, error, migrated = _read_archive_transcript(state)
    if migrated:
        # legacy→v2 1회 전이의 내구 기록 — 이후 digest-less legacy 재등장은
        # payload 근거로 재수용되지 않는다. 아래 분석 상한 거절과 무관하게
        # 디스크에서 이미 일어난 사실이므로 그대로 남긴다.
        state["archive_meta_migrated_at"] = _aware(observed).isoformat()
        _record_attempt(state, observed, "legacy_meta_migration", "ok")
    if error is None:
        payload = transcript_payload(state["video_id"], state["title"],
                                     state["published"], text)
        # 상한 초과는 아카이브 실패가 아니라 분석층의 typed 거절이다.
        error = analysis_input_refusal(payload)
    if error is None:
        _record_attempt(state, observed, "replay", "ok")
        if _persist_state_best_effort(state, prior_status) == "stale":
            return 0  # 그 사이 ANALYZED/DELIVERED — 재분석 유발 payload 금지
        print(payload)
        return 0
    # 수집됐다는 영수증 + 로컬 원천 불가(또는 분석 상한 초과): 수리 목적
    # 재조회는 중복 위험이라 절대 하지 않고, 자르지도 않는다. 진실한 실패
    # 영수증 + typed 알림(14:30부터, fingerprint dedupe)만 남긴다.
    _record_attempt(state, observed, "replay", "error", error)
    if _persist_state_best_effort(state, prior_status) == "stale":
        return 0
    maybe_alert(Observation(status="SOURCE_ERROR",
                            stage=("analysis" if error == INPUT_CAP_EXCEEDED
                                   else "replay"),
                            error_category=error, video_id=state["video_id"]),
                observed, alert)
    if error == INPUT_CAP_EXCEEDED:
        # 아카이브는 온전하고 분석층만 시작할 수 없는 경우다: 무음이되 거짓
        # 초록은 아니다 — 상태는 COLLECTED && !ANALYZED로 남는다. 아카이브
        # 자체가 깨진 typed 실패(archive_*)는 다른 판정층이라 여기서 다루지
        # 않는다(전용 알림이 이미 나가며, gate 확장은 별도 게이트다).
        print(unresolved_gate_line(error))
    return 0


def _locked_tick(args, observed, net_fetcher, alert):
    """job_lock 보유 하의 한 tick: replay → cutoff → holiday → discovery."""
    state, state_corrupt, origin = load_collection_state(observed)
    if state_corrupt:
        # 기존 canonical 상태의 모든 이상은 CORRUPT — 부재가 아니다.
        # legacy migration/discovery/payload 전부 금지, 바이트 보존,
        # typed 알림은 14:30부터 fingerprint dedupe 1회 (fail-closed).
        maybe_alert(Observation(status="SOURCE_ERROR", stage="state",
                                error_category=state_corrupt),
                    observed, alert)
        return 0
    if state is not None:
        prior_status = state["status"] if origin == "v1" else None
        return _replay_tick(state, observed, alert, prior_status)

    if not args.force_once and not automatic_window_open(observed):
        return 0  # 17:45 이후 자동 tick — discovery 금지, network 0, silent

    if not args.force_once:
        # 공휴일 preflight (Task 0.2: fresh KASI-only canonical, fail-closed).
        # healthy+공휴일 → 전부 0 silent skip. unhealthy → 수집 중단 + 알림 1회.
        # --force-once는 예외 방송/수동 RCA를 위해 이 preflight 전체를 우회한다.
        hol = holiday_preflight(observed)
        if hol is not None:
            if hol.status == "HOLIDAY_DATA_UNAVAILABLE":
                _alert_holiday_unavailable(hol, observed, alert)
            return 0

    obs = observe(net_fetcher, now=observed, already_collected=False)
    if obs.status == "TRANSCRIPT_READY":
        try:
            meta_migrated = archive(
                {"id": obs.video_id, "title": obs.title,
                 "published": obs.published,
                 "caption_language": obs.caption_language,
                 "caption_kind": obs.caption_kind,
                 "caption_vss_id": obs.caption_vss_id}, obs.transcript)
        except ArchiveConflict as exc:
            # 쌍이 read-back 검증을 통과하지 못하면 COLLECTED를 선언하지
            # 않는다 — payload/legacy 영수증 없이 typed 알림 fail-closed.
            maybe_alert(Observation(status="SOURCE_ERROR", stage="archive",
                                    error_category=exc.category,
                                    video_id=obs.video_id),
                        observed, alert)
            return 0
        digest = hashlib.sha256(obs.transcript.encode()).hexdigest()
        new_state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "video_id": obs.video_id,
            "title": obs.title,
            "published": obs.published,
            "source_revision": _source_revision(obs.video_id, obs.published,
                                                digest),
            "transcript_sha256": digest,
            "caption_language": obs.caption_language,
            "caption_kind": obs.caption_kind,
            "caption_vss_id": obs.caption_vss_id,
            "archive_stem": f"{obs.published[:10]}_{obs.video_id}",
            "status": "COLLECTED",
            "collected_at": observed.isoformat(),
            "analyzed_at": None,
            "delivered_at": None,
            "archive_meta_migrated_at": (observed.isoformat()
                                         if meta_migrated else None),
            "migrated_from_legacy": False,
            "attempts": [],
        }
        if meta_migrated:
            _record_attempt(new_state, observed, "legacy_meta_migration", "ok")
        payload = transcript_payload(obs.video_id, obs.title, obs.published,
                                     obs.transcript)
        # 분석 상한 초과: 수집은 성립했으므로 아카이브와 수집 영수증을 그대로
        # 남기되, transcript를 자르거나 분석 성공으로 기록하지 않는다.
        refusal = analysis_input_refusal(payload)
        _record_attempt(new_state, observed, "collect",
                        "ok" if refusal is None else "error", refusal)
        if _persist_state_best_effort(new_state, None) == "stale":
            return 0  # 같은 정체성이 이미 분석/전달됨 — 중복 payload 금지
        # legacy 포맷 영수증은 롤백 호환용으로 유지한다(의미: 수집 완료).
        # crash 복구 경로이기도 하다: v1 영속이 실패해도 다음 tick이
        # ENOENT+legacy migration으로 network 0 replay에 수렴한다.
        try:
            _atomic_write_json(state_path(), {
                "id": obs.video_id,
                "title": obs.title,
                "published": obs.published,
            })
        except OSError:
            pass
        if refusal is not None:
            maybe_alert(Observation(status="SOURCE_ERROR", stage="analysis",
                                    error_category=refusal,
                                    video_id=obs.video_id),
                        observed, alert)
            # 수집 영수증은 남았고 분석만 시작할 수 없다 — 같은 typed 근거를
            # scheduler에 넘겨 이 tick이 'ok'로 기록되지 않게 한다.
            print(unresolved_gate_line(refusal))
            return 0
        print(payload)
        return 0
    maybe_alert(obs, observed, alert)
    return 0


def run(argv=None, now=None, fetcher=None, alerter=None):
    """한 invocation. 자동이면 17:45 KST cutoff, --force-once면 1회 우회.

    순서: show-state(잠금 불요 경계 읽기) → ack(잠금 하 CAS) → lifecycle
    replay(network 0, cutoff·공휴일 무관 — 로컬 파일만) → cutoff → holiday
    preflight → source discovery. 상태를 변이할 수 있는 경로는 전부 per-job
    잠금 하에서만 돌고, 잠금 timeout은 typed fail-closed다."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    observed = _aware(now)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("aware_now_required")
    net_fetcher = fetcher or NetworkFetcher()
    alert = alerter or _send_alert_via_system_bot

    if args.show_state:
        return _show_state(observed)
    acks = [(kind, target) for kind, target in
            (("analyzed", args.ack_analyzed),
             ("delivered", args.ack_delivered),
             ("completed", args.ack_completed)) if target]
    if acks:
        if len(acks) > 1:
            print("ACK_CONFLICT: choose_one_ack")
            return 2
        if not args.revision:
            print("ACK_CONFLICT: revision_required")
            return 2
        kind, target = acks[0]
        try:
            with job_lock():
                rc, line = _acknowledge(kind, target, args.revision, observed)
        except StateLockTimeout:
            rc, line = 1, "ACK_CONFLICT: state_lock_timeout"
        print(line)
        return rc

    try:
        with job_lock():
            return _locked_tick(args, observed, net_fetcher, alert)
    except StateLockTimeout:
        # 잠금 미획득 tick은 아무것도 변이/출력하지 않는다(fail-closed).
        maybe_alert(Observation(status="SOURCE_ERROR", stage="state",
                                error_category=StateLockTimeout.category),
                    observed, alert)
        return 0


def main(argv=None):
    return run(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # 게시 직후 YouTube API/player/timed-text 동기화 오류는 14:29 KST까지
        # 예상 가능한 pending으로 취급한다. 14:30 틱부터 실패가 지속될 때만
        # 전용 시스템 봇으로 알린다.
        if not should_alert_failure():
            sys.exit(0)
        import subprocess
        message = (
            "🚨 시스템 작업 오류\n"
            "작업: 12시에 만나요 자막 수집\n"
            # 마지막 경계에서도 URL query의 key는 지운다(방어 심층화).
            f"예외: {_redact(f'{type(exc).__name__}: {exc}')}"
        )
        delivered = subprocess.run(
            [str(hermes_home() / "scripts" / "send_system_alert.py"), message],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if delivered.returncode != 0:
            raise
        # 오류는 전용 시스템 봇으로 이미 전달했다. 빈 stdout + 성공 종료로
        # Hermes home bot의 중복 오류 메시지와 불필요한 agent 호출을 막는다.
        sys.exit(0)
