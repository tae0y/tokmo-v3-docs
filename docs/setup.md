# tokmo-v3 설치·운영 가이드 (배포판)

tokmo-v3는 토스증권 계좌를 기준으로 **보유·체결 수집, 뉴스 판단·알림, 선택 후속 점검, 소량 회전매매**를 스케줄러 위에서 돌리는 개인용 시스템입니다. 이 문서는 원작자 환경(macOS + Hermes 스케줄러)이 아닌 곳에 설치하는 기준입니다. 모듈은 독립적으로 켜고 끌 수 있습니다.

## 1. 지원 범위 (정직하게)

| 환경 | 상태 |
|---|---|
| macOS (Apple Silicon/Intel), Python 3.12 | 원작자 운영 환경. 검증됨 |
| Linux / WSL2 (Ubuntu) | 코드는 POSIX만 사용(`fcntl.flock`, 0600 권한 검사). **설치 절차는 동일하나 원작자가 실행 검증하지 않음**. launchd 대신 cron 사용 |
| Windows 네이티브 | **미지원**. 파일 잠금·권한 검사·심링크 정책이 POSIX 전제. WSL2를 쓰세요 |
| Hermes 스케줄러 없이 | 지원. `ops/cron/crontab.example` + `ops/cron/fallback/` 대체 도구 |
| AI 구독 1개만 (Z.AI, 코덱스, 클로드 중 하나) | 뉴스 판단은 지원(`var/data/models.json` 단계별 모델·전환 사슬). 뉴스 GLM 게이트는 Z.AI 모델에만 적용되고, 모의투자·회전 진입은 `paper.decide`/`quant.rotation_entry`에 코덱스 또는 클로드 모델 하나를 지정. `docs/API-KEYS.md` "모델 레지스트리" |

## 2. 설치

```bash
git clone <this repo> ~/tokmo-v3 && cd ~/tokmo-v3
python3.12 -m venv .venv && .venv/bin/pip install -e .          # 의존성 3개(openpyxl, defusedxml, et-xmlfile)
mkdir -p var/data var/runtime var/state .secrets && chmod 700 var .secrets
cp -R config/env.example/config .secrets/config
cp -R config/env.example/hermes-credentials .secrets/hermes-credentials
cp config/env.example/env.toss.example .env.toss; cp config/env.example/env.kis.example .env.kis
chmod -R go-rwx .secrets .env.toss .env.kis
# 값 채우기: docs/API-KEYS.md 표 참고. 그 다음:
.venv/bin/python tools/check_api_keys.py
```

`~/.hermes/.env`가 없어도 됩니다. GLM 키는 `config/env.example/hermes.env`를 `~/.hermes/.env`로 복사하거나, 각 도구의 `--model-env` 옵션으로 파일을 지정하세요.

디렉터리 역할:

| 경로 | 내용 | git |
|---|---|---|
| `src/tokmo/` | 코드 (`values`=순수 계산, `facts`=외부 API 어댑터, `records`=저장, `programs`=흐름, `composition.py`=조립) | 추적 |
| `tools/` | 실행 진입점 (`run_*.py`) | 추적 |
| `ops/hermes-cron/` | 스케줄러 래퍼. 자기 위치에서 프로젝트 루트를 찾음 | 추적 |
| `ops/cron/` | 일반 cron용 예시·대체 도구 | 추적 |
| `var/data/` | 상태 DB·설정 JSON (데이터 루트) | 무시 |
| `var/runtime/`, `var/state/` | 실행 로그·회전매매 장부 | 무시 |
| `.secrets/`, `.env.toss`, `.env.kis` | 자격증명 | 무시 |
| `watchlists/` | 추적 종목 목록(배포판은 비어 있음) | 추적 |
| `config/quant_peer_registry.json` | 퀀트 비교 종목 그룹(배포판은 비어 있음, 자기 보유 종목 기준으로 채움) | 추적 |

## 3. 모듈 켜기/끄기

| 모듈 | 켜는 조건 | 끄는 방법 |
|---|---|---|
| 토스 수집(펄스·캐던스) | `.env.toss` + 크론 2줄 | 크론 줄 제거 |
| 뉴스 수집 | `news.env` + 그룹 a/b/c·fast primary 크론 | 크론 줄 제거 |
| 뉴스 판단·알림 | `var/data/news_decision.json` `enabled: true`, `delivery_enabled: true`, `var/data/news_activation.json`, `news_delivery_route.json` | `enabled: false` (수집은 계속) |
| 선택 후속 점검 | `var/data/investment_followup.json` `enabled: true` | `enabled: false` |
| 회전매매(실주문) | `var/state/hyundai-rotation/live.flag` 존재 + 회전 크론 5줄 | `live.flag` 삭제 → 드라이런 |
| 모의투자(기회 탐색형 회전, PAPER) | `tools/install_product_gate.py issue` 뒤 `tools/run_opportunity_rotation.py init --costs ops/opportunity-rotation-costs.json` + 크론 1줄(평일 08~16시 매분). 판단창 10:05/13:30. 판단이 끝난 슬롯마다 한 줄 요약과 검토 종목을 1회 알림. 실주문 없음 | 크론 줄 제거. 상태 `var/data/opportunity-rotation/`, 실행 로그 `var/runtime/opportunity-rotation/` |
| 공시(OpenDART·SEC) | 키 + 크론 2줄 | 크론 줄 제거 |
| 매크로·유튜브 | 선택. 기본 꺼짐 | |

설정 예시는 `examples/news_decision.json`, 모델 선택은 `examples/models.json` → `var/data/models.json`. 처음엔 **뉴스 판단만 켜고** 토스·회전매매는 나중에 붙이는 순서를 권합니다.

### 3-1. AI 호출 승인 (제품 게이트) — 뉴스 판단·모의투자·퀀트를 켜기 전에 필수

세 모듈의 모델 호출은 호출 직전에 이 설치본에 대한 **승인 파일**을 검사합니다(경로, 진입점, 검토 파일 sha256, 게이트·해석기 sha256,
운영 계약 sha256). 없거나 다르면 모델을 부르지 않습니다. 원작자 환경과 같은 코드이며, 배포판에서는 설치하는 사람이 직접 발급합니다.

```bash
.venv/bin/python tools/install_product_gate.py install   # ~/.hermes/scripts에 게이트·해석기, 계약, 모델 목록, 쿼터 heartbeat
chmod 700 .                                              # 퀀트는 프로젝트 루트 0700을 요구
.venv/bin/python tools/install_product_gate.py issue     # 뉴스·모의투자·퀀트 승인 파일 3개(0600) 발급
.venv/bin/python tools/install_product_gate.py check     # 게이트로 실제 판정(호출 0). 전부 ADMITTED면 끝
```

- 모델은 `var/data/models.json`에서 읽습니다(`news.*`, `paper.decide`, `quant.rotation_entry`). 모델을 바꾸면 `install --force-routing` 뒤 `issue --reissue`.
- 코드를 갱신(`git pull`)하면 해시가 달라져 호출이 멈춥니다. `issue --reissue`로 다시 발급하세요. 의도된 동작입니다.
- 쿼터 스냅샷은 45분 안에 갱신돼야 하므로 crontab에 `*/30 * * * * ... install_product_gate.py heartbeat` 한 줄이 필요합니다(crontab.example에 있음).
  원작자 환경은 Hermes가 실제 잔여 쿼터를 측정하지만 배포판은 "살아 있음"만 기록합니다. 한도 소진은 모델 오류로 드러나고 뉴스 전환 사슬이 처리합니다.
- 발급은 자기 설치본에 대한 자기 승인이며 독립 검토가 아닙니다. 승인 파일의 `review`에 그렇게 남습니다. 자세한 설명은 `ops/product-gate/README.md`.

### 3-2. Hermes가 없어도 됩니다

이 배포판은 원작자가 쓰는 에이전트(Hermes)를 설치하지 않습니다. `~/.hermes/`는 제품 게이트 코드가 고정으로 읽는 **폴더 이름**일 뿐이며
`install_product_gate.py install`이 만듭니다. 모델 호출은 코덱스 CLI(`codex login`), 클로드 CLI(`claude login`), Z.AI HTTP(API 키)를
프로세스가 직접 수행합니다. Hermes 전용 보조 스크립트(실패 알림, 타임아웃 래퍼)는 `ops/cron/fallback/`의 대체 도구가 맡고, 크론 예시의
환경변수 두 줄이 그 경로를 가리킵니다. `ops/hermes-cron/*.json`과 `ops/launchd/*.plist`는 원작자 환경의 스케줄 템플릿이라 참고용입니다.

## 4. 스케줄

`ops/cron/crontab.example`을 `crontab -e`에 붙이면 됩니다. 핵심 주기:

| 주기 | 잡 | 비고 |
|---|---|---|
| 1분 | 펄스 | 보유·체결·스톡봇 대화·뉴스 발송. 토큰은 :00~:05초에만 읽음 |
| 5분 | 캐던스, 후속 점검, 회전 평가(장중) | |
| 15분 | 뉴스 그룹 a/b/c (5분 간격 교차) | |
| 1분(자체 페이싱) | fast primary, 뉴스 판단 워커 | 워커는 한 번에 최대 ~8분 |
| 30분 | OpenDART, SEC | |
| 평일 08:40 / 10:15 / 10:40~11:10 10분마다 | 회전 정산 / 분석+핸드오프 / 보조 마감 | 분석은 10:40까지. 제안 승인은 알림 발송 뒤 30분(최대 11:10), 마감 5분 전 예고 1회. `live.flag` 있을 때만 실주문 |
| 평일 08~16시 1분 | 모의투자 step | 판단은 10:05/13:30 두 창에서만. 끝난 슬롯마다 결과 요약 1회. 실주문 없음 |
| 30분 | 제품 게이트 heartbeat | 쿼터 스냅샷 갱신(측정 아님) |

macOS에서 launchd를 쓰려면 `ops/launchd/*.plist`를 참고하되 경로를 바꾸세요(설치기 `tools/install_*_agent.py`가 생성).

### Hermes 없이 돌릴 때
래퍼가 부르는 Hermes 도구 3개는 환경변수로 대체합니다(crontab 예시에 포함):
- `TOKMO_COMPLETION_SAFE_CLI` → `ops/cron/fallback/completion-safe-cli` (타임아웃·manifest)
- `TOKMO_SYSTEM_ALERT_JOB`, `TOKMO_SYSTEM_ALERT_SCRIPT` → `ops/cron/fallback/run_system_alert_job.py`, `send_system_alert.sh` (실패 알림)
- 모의투자 결과 요약은 래퍼의 stdout으로 나옵니다. 원작자 환경은 Hermes 스케줄러가 stdout을 텔레그램으로 보내고, 일반 cron에서는 crontab 예시처럼 `send_system_alert.sh --stdin`으로 넘깁니다(빈 출력이면 아무것도 안 보냄). 받는 곳은 `TELEGRAM_ALERT_CHAT_ID`.
- 라우팅 상태 파일(`~/.hermes/MODEL_ROUTING_STATE.md`)과 쿼터 스냅샷(`~/.hermes/cron/state/model-quota-latest.json`)은 `tools/install_product_gate.py install`이 만들고 `heartbeat`가 갱신합니다(3-1절). 뉴스 GLM·모의투자·퀀트 호출에 필수입니다.

뉴스 확정 단계(`confirm`)의 `codex:gpt-5.6-sol@high`만 Hermes 런처가 있으면 그것을 씁니다(런처가 허용하는 유일한 조합). 런처가 없거나 다른 모델·강도·클로드이거나 `"transport": "direct"`를 명시하면 CLI를 직접 실행합니다. 별도 설정은 필요 없습니다(`patches/news-sol-*.patch`는 런처용 참고).

## 5. 첫 실행 확인

```bash
.venv/bin/python -m pytest tests -q -x --deselect tests/test_shadow_48h_runner_cli.py   # .env.toss 없으면 일부 CLI 테스트는 red
ops/hermes-cron/tokmo_news_group.sh A                       # 수집 1회
.venv/bin/python ops/hermes-cron/tokmo_news_classifier_worker.py   # 판단 1회 → var/data/news/decision-worker/latest.json
```

## 6. 각 모듈의 현재 완성도 (2026-10-08 기준)

| 모듈 | 상태 |
|---|---|
| 토스 수집·펄스·주문 | 안정. 09-15부터 실전 회전매매 운용 |
| 뉴스 판단·알림 | 동작. 09-26 대기·오전 묶음 수리, 09-29 수집 진단, 09-30 GLM 제품 게이트 연결, 알람 한국어 문체. 자연 2주기 확인했으나 수집 건강은 DEGRADED가 남아 있음 |
| 선택 후속 점검 | 동작. 사용자 대화 기록에 의존(스톡봇 필요) |
| 회전매매 | 현대차 1종목·소량. 밴드 정책·AI 예외판단·보호수량 분류. 10-02부터 제안 승인 창은 알림 발송 뒤 30분 |
| 모의투자(PAPER) | 카나리. 09-28 운영 시작, 10-01 첫 실제 판단 성립(현금 200만, 종목당 100만, 최대 5종목), 10-02 결과 요약 알림. 매매 품질은 미검증 |
| 퀀트 카드 | 카나리. 09-26 일봉 달력 1년 확장(종목당 거래일마다 호출 3회: 메타 1 + 페이지 2). 텔레그램 카드 전달 부분 실험 단계 |
| 매크로·유튜브·OpenDART 대량 | 부가 기능. 기본 꺼짐 |

알려진 한계: 회전매매 US 시장은 구조만 있음. 모델 전환 사슬은 뉴스 판단에만 있고 모의투자·회전 진입은 단일 모델(`models.json`에서 선택만), 후속 점검은 모듈 설정. 쿼터 스냅샷은 배포판에서 측정이 아닌 heartbeat. 테스트 9,000여 개 중 10여 개는 원작자 환경 전제로 실패할 수 있음(격리 워크트리 자격증명, 정본 경로 검사).

## 7. 개인정보·안전
- 배포판은 원작자의 플레이북·계좌·대화·평가 이력을 포함하지 않습니다. `DIST-EXPORT-REPORT.json`에 제외·치환 내역이 있습니다.
- 주문 코드는 `live.flag`와 토스 주문 권한 둘 다 있어야 실주문을 냅니다. 처음엔 `live.flag` 없이 드라이런으로 시작하세요.
- 자격증명 파일이 600이 아니거나 심링크면 해당 어댑터가 거부합니다(의도된 동작).
