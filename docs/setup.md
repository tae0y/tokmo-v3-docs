# 설치와 모듈 설정

이 문서는 원작자 환경(macOS와 Hermes 스케줄러)이 아닌 곳에 tokmo-v3를 설치하는 방법을 설명합니다. 모듈은 서로 독립적이므로 필요한 것만 켜면 됩니다.

## 지원 환경

| 환경 | 상태 |
|---|---|
| macOS (Apple Silicon·Intel), Python 3.12 | 원작자가 운영하는 환경이며 검증했습니다 |
| Linux, WSL2 (Ubuntu) | 코드는 POSIX 기능(`fcntl.flock`, 0600 권한 검사)만 씁니다. 설치 절차는 같지만 원작자가 직접 실행해 보지는 않았습니다. launchd 대신 cron을 씁니다 |
| Docker | [Docker로 실행](setup-docker.md)을 참고합니다. Windows에서는 이 방법을 권장합니다 |
| Windows 네이티브 | 지원하지 않습니다. 파일 잠금, 권한 검사, 심링크 정책이 모두 POSIX를 전제로 합니다 |
| Hermes 스케줄러 없음 | 지원합니다. `ops/cron/crontab.example`과 `ops/cron/fallback/`의 대체 도구를 씁니다 |
| AI 구독 1개 (Z.AI, 코덱스, 클로드 중 하나) | 지원합니다. 아래 [모델 선택](#모델-선택)을 참고합니다 |

## 설치

```bash
git clone https://github.com/tae0y/tokmo-v3-docs.git ~/tokmo-v3 && cd ~/tokmo-v3
python3.12 -m venv .venv && .venv/bin/pip install -e .     # 의존성: openpyxl, defusedxml, et-xmlfile
mkdir -p var/data var/runtime var/state .secrets/hermes-credentials && chmod 700 var .secrets
cp -R config/env.example/config .secrets/config
cp config/env.example/env.toss.example .env.toss
cp config/env.example/env.kis.example .env.kis
chmod -R go-rwx .secrets .env.toss .env.kis
.venv/bin/python tools/check_api_keys.py                   # 값을 채운 뒤 실행합니다
```

템플릿에 없는 파일이 두 가지 있습니다.

- `~/.hermes/.env`: GLM 키, 그룹 봇 토큰, 유튜브 키를 넣습니다. `config/env.example/hermes.env`를 복사해서 만들거나, 도구마다 `--model-env` 옵션으로 다른 파일을 지정합니다.
- `.secrets/hermes-credentials/*.env`: 텔레그램 알림과 매크로용 파일입니다. 템플릿이 없으므로 직접 만듭니다. 파일 이름과 키는 [자격증명 안내](api-keys.md)의 표에 있습니다.

```bash
cat > .secrets/hermes-credentials/news_decision_stock_telegram.env <<'EOF'
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
chmod 600 .secrets/hermes-credentials/*.env
```

## 디렉터리 구성

| 경로 | 내용 | git |
|---|---|---|
| `src/tokmo/` | 코드. 계층별 설명은 [구조](architecture.md)에 있습니다 | 추적 |
| `tools/` | 실행 진입점 (`run_*.py`, 설치·점검 도구) | 추적 |
| `ops/hermes-cron/` | 스케줄러가 부르는 래퍼. 자기 위치에서 프로젝트 루트를 찾습니다 | 추적 |
| `ops/cron/` | 일반 cron용 예시와 Hermes 대체 도구 | 추적 |
| `docker/` | 컨테이너 실행 구성 | 추적 |
| `watchlists/` | 추적할 종목 목록. 배포판은 비어 있습니다 | 추적 |
| `config/quant_peer_registry.json` | 퀀트 비교 종목 그룹. 배포판은 비어 있으며, 자기 보유 종목에 맞춰 채웁니다 | 추적 |
| `var/data/` | 상태 DB와 설정 JSON. 데이터 루트입니다 | 제외 |
| `var/runtime/`, `var/state/` | 실행 로그, 회전매매 장부 | 제외 |
| `.secrets/`, `.env.toss`, `.env.kis` | 자격증명 | 제외 |

## 모듈 켜고 끄기

| 모듈 | 켜는 조건 | 끄는 방법 |
|---|---|---|
| 토스 수집 (펄스·캐던스) | `.env.toss`, cron 2줄 | cron 줄을 지웁니다 |
| 뉴스 수집 | `news.env`, 그룹 a·b·c와 fast primary cron | cron 줄을 지웁니다 |
| 뉴스 판단·알림 | `var/data/news_decision.json`의 `enabled: true`, `delivery_enabled: true`, 그리고 `var/data/news_activation.json`, `var/data/news_delivery_route.json` | `enabled: false`로 바꿉니다. 수집은 계속됩니다 |
| 선택 후속 점검 | `var/data/investment_followup.json`의 `enabled: true` | `enabled: false`로 바꿉니다 |
| 회전매매 (실주문) | `var/state/hyundai-rotation/live.flag`, 회전 cron 5줄 | `live.flag`를 지우면 드라이런으로 돌아갑니다 |
| 모의투자 (PAPER) | 승인 파일 발급, `tools/run_opportunity_rotation.py init --costs ops/opportunity-rotation-costs.json`, cron 1줄(평일 08-16시 매분) | cron 줄을 지웁니다. 상태는 `var/data/opportunity-rotation/`, 실행 로그는 `var/runtime/opportunity-rotation/`에 남습니다 |
| 공시 (OpenDART·SEC) | 키, cron 2줄 | cron 줄을 지웁니다 |
| 매크로·유튜브 | 선택 사항이며 기본으로 꺼져 있습니다 | |

설정 예시는 `examples/news_decision.json`에 있습니다. 처음에는 **뉴스 판단만 켜고**, 토스 수집과 회전매매는 나중에 붙이는 순서를 권장합니다.

### 모델 선택

단계별 모델은 `var/data/models.json`에서 정합니다(예시 `examples/models.json`). 구독이 하나뿐이어도 됩니다. 그 공급자의 모델만 적으면 다른 공급자는 호출하지 않습니다.

- 뉴스 판단은 단계마다 모델 목록을 적어 전환 순서를 정할 수 있습니다. GLM 게이트는 Z.AI 모델에만 적용됩니다.
- 모의투자(`paper.decide`)와 회전 진입(`quant.rotation_entry`)은 단계마다 모델을 하나만 지정합니다. 코덱스나 클로드 모델 중에서 고릅니다.

자세한 표기법과 구독별 예시는 [자격증명 안내의 모델 레지스트리](api-keys.md#모델-레지스트리)에 있습니다.

## AI 호출 승인 (제품 게이트)

뉴스 판단, 모의투자, 퀀트 분석은 모델을 부르기 직전에 이 설치본의 **승인 파일**을 검사합니다. 승인 파일에는 설치 경로, 진입점, 검토한 코드 파일, 게이트와 해석기, 운영 계약 문서의 sha256이 들어 있습니다. 승인 파일이 없거나 해시가 하나라도 다르면 모델을 호출하지 않습니다. 원작자 환경과 같은 코드로 검사하며, 배포판에서는 설치하는 사람이 승인 파일을 직접 발급합니다.

```bash
.venv/bin/python tools/install_product_gate.py install   # ~/.hermes/scripts에 게이트·해석기, 계약, 모델 목록, 쿼터 heartbeat를 설치합니다
chmod 700 .                                              # 퀀트는 프로젝트 루트 권한이 0700이어야 합니다
.venv/bin/python tools/install_product_gate.py issue     # 뉴스·모의투자·퀀트 승인 파일 3개(0600)를 발급합니다
.venv/bin/python tools/install_product_gate.py check     # 게이트로 실제 판정합니다(모델 호출 없음). 모두 ADMITTED면 끝입니다
```

- 모델은 `var/data/models.json`의 `news.*`, `paper.decide`, `quant.rotation_entry`에서 읽습니다. 모델을 바꾸면 `install --force-routing`을 실행한 뒤 `issue --reissue`로 다시 발급합니다.
- `git pull`로 코드를 갱신하면 해시가 달라져 모델 호출이 멈춥니다. 의도한 동작이며, `issue --reissue`로 다시 발급하면 됩니다.
- 쿼터 스냅샷은 45분 안에 갱신돼야 합니다. 그래서 crontab에 `*/30 * * * * ... install_product_gate.py heartbeat` 한 줄이 필요하며, `crontab.example`에 들어 있습니다.
- 원작자 환경에서는 Hermes가 실제 남은 쿼터를 측정합니다. 배포판의 heartbeat는 "살아 있음"만 기록하므로, 한도 소진은 모델 오류로 드러나고 뉴스 판단의 전환 순서가 처리합니다.
- 이 발급은 자기 설치본에 대한 자기 승인이며 독립 검토가 아닙니다. 승인 파일의 `review` 항목에도 그렇게 기록됩니다. 자세한 내용은 [제품 모델 게이트](../ops/product-gate/README.md)에 있습니다.

## Hermes 없이 실행

이 배포판은 원작자가 쓰는 에이전트(Hermes)를 설치하지 않습니다. `~/.hermes/`는 제품 게이트 코드가 고정으로 읽는 폴더 이름일 뿐이며, `install_product_gate.py install`이 만듭니다. 모델 호출은 코덱스 CLI(`codex login`), 클로드 CLI(`claude login`), Z.AI HTTP(API 키)로 프로세스가 직접 합니다.

래퍼가 부르는 Hermes 보조 도구 3개는 환경변수로 대체합니다. `crontab.example`에 이미 들어 있습니다.

| 환경변수 | 대체 도구 | 역할 |
|---|---|---|
| `TOKMO_COMPLETION_SAFE_CLI` | `ops/cron/fallback/completion-safe-cli` | 타임아웃과 실행 기록(manifest) |
| `TOKMO_SYSTEM_ALERT_JOB` | `ops/cron/fallback/run_system_alert_job.py` | 실패했을 때만 알림 |
| `TOKMO_SYSTEM_ALERT_SCRIPT` | `ops/cron/fallback/send_system_alert.sh` | 텔레그램 발송 |

- 모의투자 결과 요약은 래퍼의 표준 출력으로 나옵니다. 일반 cron에서는 `crontab.example`처럼 `send_system_alert.sh --stdin`으로 넘기며, 출력이 비어 있으면 아무것도 보내지 않습니다. 받는 곳은 `TELEGRAM_ALERT_CHAT_ID`입니다.
- 라우팅 상태 파일(`~/.hermes/MODEL_ROUTING_STATE.md`)과 쿼터 스냅샷(`~/.hermes/cron/state/model-quota-latest.json`)은 `install`이 만들고 `heartbeat`가 갱신합니다. 뉴스 GLM, 모의투자, 퀀트 호출에 꼭 필요합니다.
- 뉴스 확정 단계의 `codex:gpt-5.6-sol@high`만 Hermes 런처가 있으면 런처를 거칩니다. 런처가 허용하는 조합이 이것 하나이기 때문입니다. 런처가 없거나, 다른 모델·강도·클로드를 쓰거나, `"transport": "direct"`를 적으면 CLI를 직접 실행합니다. 따로 설정할 것은 없으며, `patches/news-sol-*.patch`는 런처용 참고 자료입니다.
- `ops/hermes-cron/*.json`과 `ops/launchd/*.plist`는 원작자 환경의 스케줄 템플릿이므로 참고용입니다. macOS에서 launchd를 쓰려면 경로를 바꿔서 쓰거나 `tools/install_*_agent.py`로 생성합니다.

## 스케줄 등록

`ops/cron/crontab.example`의 `PROJECT` 경로를 바꾼 뒤 `crontab -e`에 붙여 넣습니다. 켜지 않을 모듈의 줄은 지웁니다. 주기와 각 잡의 의미는 [운영 안내](operations.md#스케줄)에 있습니다.

## 첫 실행 확인

```bash
.venv/bin/python -m pytest tests -q -x --deselect tests/test_shadow_48h_runner_cli.py
ops/hermes-cron/tokmo_news_group.sh A                              # 뉴스 수집 1회
.venv/bin/python ops/hermes-cron/tokmo_news_classifier_worker.py   # 뉴스 판단 1회 → var/data/news/decision-worker/latest.json
```

`.env.toss`가 없으면 일부 CLI 테스트가 실패합니다.
