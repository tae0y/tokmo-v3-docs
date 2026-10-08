# 자격증명 (API 키·토큰) 안내

tokmo-v3는 모듈마다 자기 자격증명 파일을 하나씩 읽습니다. 파일은 전부 `.secrets/` 아래(또는 프로젝트 루트의 `.env.*`)에 두고, **권한은 디렉터리 700 / 파일 600**이어야 합니다. 심링크 파일은 거부됩니다. 저장소에는 절대 커밋되지 않습니다(`.gitignore`).

무엇이 있고 무엇이 빠졌는지는 한 명령으로 확인합니다(값은 출력하지 않음):

```bash
.venv/bin/python tools/check_api_keys.py
```

빈 템플릿은 `config/env.example/`에 있습니다. 통째로 복사한 뒤 값을 채우면 됩니다:

```bash
cp -R config/env.example/config .secrets/config
cp -R config/env.example/hermes-credentials .secrets/hermes-credentials
cp config/env.example/env.toss.example .env.toss && cp config/env.example/env.kis.example .env.kis
chmod -R go-rwx .secrets .env.toss .env.kis
```

## 모듈별 필요 항목

| 모듈 | 파일 (프로젝트 루트 기준) | 키 | 발급처 / 비고 |
|---|---|---|---|
| **토스증권** 보유·체결·주문·펄스·회전매매 | `.env.toss` | `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_NUMBER` | 토스증권 오픈API. 주문 모듈을 쓰려면 주문 권한 포함 |
| **뉴스 수집** | `.secrets/config/tokmo/news.env` | `FINNHUB_API_KEY`, `NAVER_NEWS_CLIENT_ID`, `NAVER_NEWS_CLIENT_SECRET` | Finnhub 무료 티어, 네이버 개발자센터 |
| 뉴스 수집 (선택) | `.secrets/config/tokmo-v3/marketaux.env`, `thenewsapi.env` | `MARKETAUX_API_KEY`, `THENEWSAPI_API_KEY` | 없으면 해당 소스만 건너뜀 |
| **공시** (뉴스·퀀트 공용) | `.secrets/config/tokmo-v3/opendart.env` | `OPENDART_API_KEY` | 금감원 OpenDART |
| 미국 공시 | `.secrets/config/tokmo-v3/sec.env` | `SEC_USER_AGENT` | 키가 아니라 "이름 이메일" 연락처 문자열 (SEC 요구사항) |
| **뉴스 판단·후속 점검 AI** | `~/.hermes/.env` (또는 `--model-env`로 지정한 파일) | `GLM_API_KEY` (또는 `ZAI_API_KEY`) | Z.AI GLM. `models.json`에 `zai` 모델이 하나라도 있을 때 필요 |
| 뉴스 판단 (코덱스/클로드) | 코덱스 또는 클로드 CLI 로그인 | 키 없음 | `codex login` / `claude login`. `models.json`에 `codex:`/`claude:` 모델이 있을 때만 필요 |
| 퀀트 정밀 분석 (선택) | 코덱스 또는 클로드 CLI 로그인 | 키 없음 | `investment_followup.json`의 `analysis_model` |
| **알림 (텔레그램)** | `.secrets/hermes-credentials/news_decision_stock_telegram.env`, `news_health_telegram.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 봇 하나를 두 파일에 같이 써도 됨 |
| 알림 대상 그룹 | `var/data/news_delivery_route.json` | `chat_id`, `credential_file` | 비밀 아님. 알림을 받을 그룹 id |
| 그룹 봇 (퀀트 카드) | `~/.hermes/.env` | `TELEGRAM_BOT_TOKEN` | |
| 스톡봇 대화 (펄스) | 셸 환경변수 | `TELEGRAM_STOCK_BOT`, `TELEGRAM_CHAT_ID` | 크론 래퍼가 `~/.zshrc`를 읽어 export. cron 환경에서는 crontab 상단에 직접 정의 |
| **ETF 시세** (퀀트) | `.env.kis` | `KIS_APP_KEY`, `KIS_APP_SECRET` | 한국투자증권 오픈API |
| KRX 구성종목 | `.secrets/config/tokmo-v3/krx.env` | `KRX_API_KEY` | |
| 매크로 (선택) | `.secrets/hermes-credentials/fred.env`, `bls_ua.env`, `.secrets/config/tokmo-v3/tokmo.env` | `FRED_API_KEY`, `BLS_USER_AGENT`, `EIA_API_KEY` | 매크로 런타임을 켤 때만 |
| 유튜브 추적 (선택) | `~/.hermes/.env` | `YOUTUBE_API_KEY` | 12man 자막 수집을 켤 때만 |

## 최소 구성 (뉴스 알림만)
토스 없이 뉴스 알림만 받으려면: `news.env`(Finnhub·네이버) + `opendart.env` + `GLM_API_KEY` + 텔레그램 봇 2파일 + `news_delivery_route.json`. AI 구독은 하나면 됩니다. 어느 것이든 `var/data/models.json`에 그 공급자의 모델만 적으면 됩니다(아래 "모델 레지스트리").

## 모델 레지스트리 (`var/data/models.json`)
뉴스 판단의 세 단계가 어떤 모델을 쓰는지 한 파일에서 정합니다. 예시는 `examples/models.json`. 없으면 `news_decision.json`의 `screen_model`/`first_pass_model`/`analysis_model`이 그대로 적용됩니다.

```json
{ "schema": "tokmo-models-v1",
  "news": { "screen":     ["glm-5.3", "codex:gpt-5.6-terra@low"],
            "first_pass": ["glm-5.3", "codex:gpt-5.6-terra@low"],
            "confirm":    ["codex:gpt-5.6-sol@high", "claude:claude-opus-5"] } }
```

- 표기는 `[공급자:]모델[@추론강도]`. 공급자는 `zai`(기본, GLM 키), `codex`(`codex login`), `claude`(`claude login`).
- 값이 목록이면 **전환 사슬**입니다. 첫 모델이 답하고, 그 모델이 **QUOTA·AUTH·TRANSIENT**(한도 소진·인증·통신 장애)로 실패했을 때만 다음 모델로 넘어갑니다. 답이 이상하거나 요청 오류(REQUEST)면 넘어가지 않고 그대로 실패합니다. 목록에 없는 공급자는 절대 호출하지 않습니다.
- 전환은 워커 실행 사이에도 유지되며(`var/data/news/decision-worker/model-chain.json`), 통신 장애는 5분→15분→6시간, 인증은 6시간 뒤 원래 모델을 다시 시도합니다. 한도(QUOTA)는 Hermes quota 스냅샷(`~/.hermes/cron/state/model-quota-latest.json`, 하루 2회 갱신)에 소진된 창의 초기화 시각이 있으면 **그 시각 + 5분**에, 없으면 6시간 뒤에 다시 시도합니다. 판단은 실패할 때마다 새로 하므로 6시간 탐침이 실패하면서 초기화 시각을 알게 되면 그때부터 그 시각이 적용됩니다. 스냅샷은 24시간 이내 관측분만, 초기화 시각은 7일 이내만 믿습니다. Hermes 없는 설치에는 스냅샷이 없으니 항상 6시간 방식입니다. 전환·복귀는 `[뉴스 판단 점검]` 알림에 한 줄로 나옵니다.
- 모델 id는 워커가 모델을 부르기 전에 검증하고 모르는 id면 그 실행은 `EXCEPTION`으로 끝납니다(`models.json`뿐 아니라 `news_decision.json`의 옛 키도 같이). 코덱스 id는 CLI가 캐시한 `~/.codex/models_cache.json`(그 계정이 실제 쓸 수 있는 목록, 숨김 항목 제외)과, 나머지는 `src/tokmo/values/model_registry.py`의 `KNOWN_MODELS`와 대조합니다. `tools/check_api_keys.py`의 `[models]` 블록에서 미리 확인하세요.
- 확정 단계(`confirm`)의 `codex:gpt-5.6-sol@high`만 Hermes 런처(설치돼 있을 때)를 씁니다. 런처가 허용하는 조합이 그것뿐이기 때문입니다. 다른 모델·강도·클로드는 항상 CLI 직접 실행이고, 그런 항목에 `"transport": "hermes"`를 적으면 설정 오류로 거부됩니다. `{"model": "codex:gpt-5.6-sol", "effort": "high", "transport": "direct"}`로 런처를 끌 수 있습니다.
- 사슬이 하나라도 있으면 워커가 `model_wait_enabled`를 자동으로 켭니다(전 멤버 대기 중에 보류한 입력을 다시 이어가는 장치가 그것이기 때문). `latest.json`의 `model_wait_reason`에 표시됩니다.
- 사슬은 엔진에 하나의 이름 `chain:<단계>[모델1>모델2]`로 보입니다. 호출 기록과 전체 대기(ModelWait)는 이 이름으로 잠기고, 실제 답한 모델은 기록의 `_model`과 `latest.json`의 `model_chain.events`에 남습니다. 전체 대기를 수동 해제할 때(`--resume-model`)도 이 이름을 씁니다.

구독별 예시:

| 보유 구독 | `models.json` `news` |
|---|---|
| Z.AI만 | `screen`/`first_pass`/`confirm` 모두 `glm-5.3` (+ `news_decision.json` `confirm_enabled: false`) |
| 코덱스만 | `screen`·`first_pass`: `codex:gpt-5.6-terra@low`, `confirm`: `codex:gpt-5.6-sol@high` |
| 클로드만 | `screen`·`first_pass`: `claude:claude-haiku-4-5-20251001`, `confirm`: `claude:claude-sonnet-5` |
| Z.AI + 코덱스 + 클로드 | 위 예시 JSON (GLM이 막히면 terra, 확정은 sol이 막히면 Opus 5) |

단일 모델 단계(사슬 없음, 정확히 하나):

| 단계 | 쓰는 곳 | 기본값 |
|---|---|---|
| `paper.decide` | 모의투자(PAPER) 판단. 게이트 승인 파일이 이 id를 pin하므로 바꾸면 `install_product_gate.py install --force-routing` + `issue --reissue` | `codex:gpt-6-astra` |
| `quant.rotation_entry` | 현대차 회전 분석이 부르는 퀀트 진입 모델 | `codex:gpt-6-astra` |

```json
{ "schema": "tokmo-models-v1",
  "news":  { "screen": "glm-5.3", "first_pass": "glm-5.3", "confirm": "glm-5.3" },
  "paper": { "decide": "claude:claude-sonnet-5" },
  "quant": { "rotation_entry": "claude:claude-sonnet-5" } }
```

후속 점검(`investment_followup.json`)은 아직 모듈 설정이며 사슬을 지원하지 않습니다. `check_api_keys.py`가 이 값들도 같은 표에 보여줍니다.

제품 게이트: 뉴스 GLM, 모의투자, 퀀트 호출은 `tools/install_product_gate.py`로 발급한 승인 파일이 있어야 합니다(`docs/DISTRIBUTION.md` 3-1절). 모델을 바꾸면 재발급이 필요합니다.

확인된 id (2026-09-25): 코덱스 ChatGPT 구독은 `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`만 허용합니다(`gpt-5.3-codex-spark`, `gpt-5.4-mini`는 거부됨). 클로드 CLI는 `claude-fable-5-1`, `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`.
