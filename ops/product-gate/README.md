# 제품 모델 게이트

tokmo-v3에서 AI 모델을 호출하는 세 곳(뉴스 GLM 판단, 모의투자 판단, 퀀트 분석)은 호출 직전에 **승인 파일**을 검사합니다. 승인 파일은 0600 JSON이며 다음 값을 묶어 둡니다.

- 설치 경로와 진입점
- 검토한 코드 파일들의 sha256
- 게이트 코드와 라우팅 해석기의 sha256
- 운영 계약 문서의 sha256

이 중 하나라도 다르면 모델을 호출하지 않으며, 대체 경로는 없습니다. 원작자 환경과 배포판이 같은 코드로 검사합니다.

## 이 디렉터리의 파일

| 파일 | 역할 |
|---|---|
| `product_model_gate.py` | 공통 게이트입니다. 승인 검사, 영수증에 넣을 값, 발급 함수가 들어 있습니다. 배포판을 내보낼 때 원본의 `shared/`에서 복사합니다 |
| `model_routing.py` | 라우팅 상태 파일에서 모델 목록을 읽는 해석기입니다. 게이트가 sha256을 먼저 확인한 뒤 실행합니다 |
| `OPERATING_CONTRACT.md` | 배포판용 운영 계약입니다. 설치기가 `~/.hermes/CRITICAL_OPERATING_CONTRACT.md`로 복사합니다. 내용을 바꾸면 발급한 승인 파일이 모두 무효가 됩니다 |

## 설치와 발급

`tools/install_product_gate.py`가 설치와 발급을 맡습니다. 전체 순서는 [AI 호출 승인](../../docs/setup.md#ai-호출-승인-제품-게이트)에 있습니다.

```bash
.venv/bin/python tools/install_product_gate.py install      # 게이트, 해석기, 계약, 라우팅 상태, 쿼터 heartbeat를 설치합니다
.venv/bin/python tools/install_product_gate.py issue        # 이 설치본 기준으로 뉴스·모의투자·퀀트 승인 파일 3개를 발급합니다
.venv/bin/python tools/install_product_gate.py check        # 발급 결과를 게이트로 실제 판정합니다(모델 호출 없음)
```

게이트가 읽는 위치는 코드에 고정돼 있어 바꿀 수 없습니다. 설치하면 `~/.hermes/` 아래에 다음 파일이 생깁니다.

- `scripts/product_model_gate.py`, `scripts/model_routing.py`
- `CRITICAL_OPERATING_CONTRACT.md` (운영 계약), `MODEL_ROUTING_STATE.md` (쓸 수 있는 모델 목록)
- `cron/state/model-quota-latest.json` (쿼터 스냅샷). 45분 안에 갱신돼야 하므로 heartbeat cron 한 줄이 필요합니다

## 배포판의 한계

- 원작자 환경에서는 Hermes가 구독별 남은 쿼터를 측정해 스냅샷을 씁니다. 배포판에는 측정 도구가 없으므로 `heartbeat`는 "살아 있음"만 기록합니다(모든 창의 remaining이 100). 한도 소진은 모델 오류로 드러나며, 뉴스 판단은 전환 순서로 이를 처리합니다.
- `git pull`로 코드를 갱신하면 검토 파일의 해시가 달라져 호출이 멈춥니다. 의도한 동작이며, `issue --reissue`로 다시 발급하면 됩니다.
- 발급은 자기 설치본에 대한 자기 승인이며 독립 검토가 아닙니다. 승인 파일의 `review` 항목에도 그렇게 기록됩니다.
