# 자격증명 안내

tokmo-v3는 모듈마다 자기 자격증명 파일을 하나씩 읽습니다. 파일은 모두 `.secrets/` 아래나 프로젝트 루트의 `.env.*`에 둡니다. 디렉터리 권한은 700, 파일 권한은 600이어야 하며, 심링크 파일은 읽지 않습니다. 이 파일들은 `.gitignore`에 들어 있어 커밋되지 않습니다.

무엇이 있고 무엇이 빠졌는지는 다음 명령으로 확인합니다. 값은 출력하지 않습니다.

```bash
.venv/bin/python tools/check_api_keys.py
```

빈 템플릿은 `config/env.example/`에 있습니다. 복사하는 순서는 [설치와 모듈 설정](setup.md#설치)에 있습니다.

## 모듈별 필요 항목

경로는 프로젝트 루트 기준입니다.

| 모듈 | 파일 | 키 | 발급처와 비고 |
|---|---|---|---|
| **토스증권** (보유·체결·주문·펄스·회전매매) | `.env.toss` | `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_NUMBER` | 토스증권 오픈API. 주문 모듈을 쓰려면 주문 권한이 있어야 합니다 |
| **뉴스 수집** | `.secrets/config/tokmo/news.env` | `FINNHUB_API_KEY`, `NAVER_NEWS_CLIENT_ID`, `NAVER_NEWS_CLIENT_SECRET` | Finnhub 무료 요금제, 네이버 개발자센터 |
| 뉴스 수집 (선택) | `.secrets/config/tokmo-v3/marketaux.env`, `thenewsapi.env` | `MARKETAUX_API_KEY`, `THENEWSAPI_API_KEY` | 없으면 해당 소스만 건너뜁니다 |
| **공시** (뉴스·퀀트 공용) | `.secrets/config/tokmo-v3/opendart.env` | `OPENDART_API_KEY` | 금융감독원 OpenDART |
| 미국 공시 | `.secrets/config/tokmo-v3/sec.env` | `SEC_USER_AGENT` | 키가 아니라 "이름 이메일" 형식의 연락처입니다. SEC가 요구합니다 |
| **뉴스 판단·후속 점검 AI** | `~/.hermes/.env` 또는 `--model-env`로 지정한 파일 | `GLM_API_KEY` 또는 `ZAI_API_KEY` | Z.AI GLM. `models.json`에 `zai` 모델이 하나라도 있으면 필요합니다 |
| 뉴스 판단 (코덱스·클로드) | CLI 로그인 | 키 없음 | `codex login`, `claude login`. `models.json`에 `codex:`·`claude:` 모델이 있을 때만 필요합니다 |
| 퀀트 정밀 분석 (선택) | CLI 로그인 | 키 없음 | `investment_followup.json`의 `analysis_model` |
| **알림 (텔레그램)** | `.secrets/hermes-credentials/news_decision_stock_telegram.env`, `news_health_telegram.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 봇 하나를 두 파일에 같이 써도 됩니다. 템플릿이 없으므로 직접 만듭니다 |
| 알림 대상 그룹 | `var/data/news_delivery_route.json` | `chat_id`, `credential_file` | 비밀 값이 아니며, 알림을 받을 그룹 id입니다 |
| 그룹 봇 (퀀트 카드) | `~/.hermes/.env` | `TELEGRAM_BOT_TOKEN` | |
| 스톡봇 대화 (펄스) | 셸 환경변수 | `TELEGRAM_STOCK_BOT`, `TELEGRAM_CHAT_ID` | macOS 래퍼는 `~/.zshrc`를 읽어 export합니다. cron에서는 crontab 맨 위에, Docker에서는 `docker/tokmo.env`에 적습니다 |
| **ETF 시세** (퀀트) | `.env.kis` | `KIS_APP_KEY`, `KIS_APP_SECRET` | 한국투자증권 오픈API |
| KRX 구성종목 | `.secrets/config/tokmo-v3/krx.env` | `KRX_API_KEY` | |
| 매크로 (선택) | `.secrets/hermes-credentials/fred.env`, `bls_ua.env`, `.secrets/config/tokmo-v3/tokmo.env` | `FRED_API_KEY`, `BLS_USER_AGENT`, `EIA_API_KEY` | 매크로 런타임을 켤 때만 필요합니다 |
| 유튜브 추적 (선택) | `~/.hermes/.env` | `YOUTUBE_API_KEY` | 자막 수집을 켤 때만 필요합니다 |

## 최소 구성 (뉴스 알림만)

토스 계좌 없이 뉴스 알림만 받으려면 다음이 필요합니다.

- `news.env` (Finnhub·네이버)
- `opendart.env`
- 모델 하나: `GLM_API_KEY`, 또는 코덱스·클로드 CLI 로그인
- 텔레그램 봇 파일 2개
- `var/data/news_delivery_route.json`

AI 구독은 하나면 됩니다. `var/data/models.json`에 그 공급자의 모델만 적습니다.

## 모델 레지스트리

`var/data/models.json` 한 파일에서 각 단계가 어떤 모델을 쓸지 정합니다. 예시는 `examples/models.json`에 있습니다. 이 파일이 없으면 `news_decision.json`의 `screen_model`, `first_pass_model`, `analysis_model`을 그대로 씁니다.

```json
{ "schema": "tokmo-models-v1",
  "news": { "screen":     ["glm-5.3", "codex:gpt-5.6-terra@low"],
            "first_pass": ["glm-5.3", "codex:gpt-5.6-terra@low"],
            "confirm":    ["codex:gpt-5.6-sol@high", "claude:claude-opus-5"] } }
```

### 표기법

모델은 `[공급자:]모델[@추론강도]`로 적습니다. 공급자는 `zai`(기본값, GLM 키), `codex`(`codex login`), `claude`(`claude login`) 중 하나입니다.

### 전환 순서 (뉴스 판단만)

값을 목록으로 적으면 전환 순서가 됩니다. 첫 모델이 답하고, 그 모델이 **QUOTA**(한도 소진), **AUTH**(인증 실패), **TRANSIENT**(통신 장애)로 실패했을 때만 다음 모델로 넘어갑니다. 답이 이상하거나 요청 자체가 잘못된 경우(REQUEST)에는 넘어가지 않고 그대로 실패합니다. 목록에 없는 공급자는 호출하지 않습니다.

- 전환 상태는 워커 실행이 바뀌어도 `var/data/news/decision-worker/model-chain.json`에 남습니다.
- 원래 모델을 다시 시도하는 시점은 실패 종류에 따라 다릅니다.

| 실패 종류 | 다시 시도하는 시점 |
|---|---|
| TRANSIENT | 5분, 15분, 6시간 뒤 순서로 늘어납니다 |
| AUTH | 6시간 뒤 |
| QUOTA | 쿼터 스냅샷에 그 창의 초기화 시각이 있으면 초기화 시각 + 5분, 없으면 6시간 뒤 |

- 쿼터 스냅샷(`~/.hermes/cron/state/model-quota-latest.json`)은 원작자 환경에서 Hermes가 하루 2회 갱신합니다. 24시간 이내에 관측한 값만 쓰고, 초기화 시각은 7일 이내 값만 씁니다. 판단은 실패할 때마다 새로 하므로, 6시간 뒤 시도가 실패하면서 초기화 시각을 알게 되면 그때부터는 초기화 시각을 따릅니다.
- Hermes가 없는 설치에는 측정된 스냅샷이 없으므로 QUOTA도 항상 6시간 뒤에 다시 시도합니다.
- 전환과 복귀는 `[뉴스 판단 점검]` 알림에 한 줄로 나옵니다.
- 전환 순서가 하나라도 있으면 워커가 `model_wait_enabled`를 자동으로 켭니다. 모든 모델이 대기 중일 때 보류한 입력을 나중에 이어서 처리하려면 이 설정이 필요하기 때문입니다. 켜진 이유는 `latest.json`의 `model_wait_reason`에 나옵니다.
- 엔진은 전환 순서를 `chain:<단계>[모델1>모델2]`라는 이름 하나로 봅니다. 호출 기록과 전체 대기(ModelWait)는 이 이름을 기준으로 잠기고, 실제로 답한 모델은 기록의 `_model`과 `latest.json`의 `model_chain.events`에 남습니다. 전체 대기를 수동으로 풀 때(`--resume-model`)도 이 이름을 씁니다.

### 모델 id 검증

워커는 모델을 부르기 전에 id를 검증합니다. 모르는 id가 있으면 그 실행은 `EXCEPTION`으로 끝납니다. `models.json`뿐 아니라 `news_decision.json`의 옛 키도 함께 검사합니다.

- 코덱스 id는 CLI가 캐시한 `~/.codex/models_cache.json`과 비교합니다. 그 계정이 실제로 쓸 수 있는 목록이며, 숨김 항목은 제외합니다.
- 나머지 id는 `src/tokmo/values/model_registry.py`의 `KNOWN_MODELS`와 비교합니다.
- `tools/check_api_keys.py`의 `[models]` 항목에서 미리 확인할 수 있습니다.

2026-09-25에 확인한 id는 다음과 같습니다.

| 공급자 | 허용되는 id |
|---|---|
| 코덱스 (ChatGPT 구독) | `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`. `gpt-5.3-codex-spark`, `gpt-5.4-mini`는 거부됩니다 |
| 클로드 CLI | `claude-fable-5-1`, `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001` |

### Hermes 런처

뉴스 확정 단계의 `codex:gpt-5.6-sol@high`만 Hermes 런처가 설치돼 있으면 런처를 거칩니다. 런처가 허용하는 조합이 이것 하나이기 때문입니다. 다른 모델, 다른 강도, 클로드는 항상 CLI를 직접 실행하며, 이런 항목에 `"transport": "hermes"`를 적으면 설정 오류로 거부합니다. 런처를 끄려면 `{"model": "codex:gpt-5.6-sol", "effort": "high", "transport": "direct"}`처럼 적습니다.

### 구독별 예시

| 가진 구독 | `models.json`의 `news` |
|---|---|
| Z.AI만 | `screen`, `first_pass`, `confirm` 모두 `glm-5.3`. `news_decision.json`에 `confirm_enabled: false`도 적습니다 |
| 코덱스만 | `screen`·`first_pass`: `codex:gpt-5.6-terra@low`, `confirm`: `codex:gpt-5.6-sol@high` |
| 클로드만 | `screen`·`first_pass`: `claude:claude-haiku-4-5-20251001`, `confirm`: `claude:claude-sonnet-5` |
| Z.AI, 코덱스, 클로드 모두 | 위의 예시 JSON. GLM이 막히면 terra로, 확정 단계에서 sol이 막히면 Opus 5로 넘어갑니다 |

### 모델을 하나만 지정하는 단계

| 단계 | 쓰는 곳 | 기본값 |
|---|---|---|
| `paper.decide` | 모의투자 판단. 승인 파일이 이 id를 고정하므로, 바꾸면 `install_product_gate.py install --force-routing`과 `issue --reissue`를 실행합니다 | `codex:gpt-6-astra` |
| `quant.rotation_entry` | 현대차 회전 분석이 부르는 퀀트 진입 모델 | `codex:gpt-6-astra` |

```json
{ "schema": "tokmo-models-v1",
  "news":  { "screen": "glm-5.3", "first_pass": "glm-5.3", "confirm": "glm-5.3" },
  "paper": { "decide": "claude:claude-sonnet-5" },
  "quant": { "rotation_entry": "claude:claude-sonnet-5" } }
```

후속 점검(`investment_followup.json`)은 아직 모듈 설정에서 모델을 정하며, 전환 순서를 지원하지 않습니다. `check_api_keys.py`는 이 값도 같은 표에 보여 줍니다.

뉴스 GLM, 모의투자, 퀀트 호출에는 `tools/install_product_gate.py`로 발급한 승인 파일이 필요합니다. 모델을 바꾸면 다시 발급해야 합니다. 절차는 [AI 호출 승인](setup.md#ai-호출-승인-제품-게이트)에 있습니다.
