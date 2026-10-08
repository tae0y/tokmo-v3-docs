# 제품 모델 게이트 (배포판 설치용)

tokmo-v3의 AI 호출 세 곳(뉴스 GLM 판단, 모의투자 PAPER 판단, 퀀트 분석)은 호출 직전에
**제품 승인 파일**을 검사합니다. 승인 파일은 이 설치본의 경로, 진입점, 검토된 코드 파일들의 sha256,
게이트 코드와 라우팅 해석기의 sha256, 운영 계약 문서의 sha256을 묶어 둔 0600 JSON입니다.
하나라도 다르면 모델을 부르지 않습니다(fallback 없음). 원작자 환경과 배포판 모두 같은 코드로 검사합니다.

이 디렉터리에는 설치에 필요한 것이 들어 있습니다.

| 파일 | 역할 |
|---|---|
| `product_model_gate.py` | 공통 게이트(승인 검사, 영수증 facts, 발급 함수). 배포 내보내기 때 `shared/`에서 복사됨 |
| `model_routing.py` | 라우팅 상태 파일의 모델 목록 해석기. 게이트가 sha256을 먼저 확인하고 실행 |
| `OPERATING_CONTRACT.md` | 배포판용 운영 계약 문서. 설치기가 `~/.hermes/CRITICAL_OPERATING_CONTRACT.md`로 복사 |

설치와 발급은 `tools/install_product_gate.py`가 합니다. 절차는 `docs/DISTRIBUTION.md` 3-1절.

```bash
.venv/bin/python tools/install_product_gate.py install      # 게이트·해석기·계약·라우팅 상태·쿼터 heartbeat 설치
.venv/bin/python tools/install_product_gate.py issue        # 뉴스·모의투자·퀀트 승인 파일 3개 발급(이 설치본 기준)
.venv/bin/python tools/install_product_gate.py check        # 발급 결과를 게이트로 실제 판정(호출 0)
```

게이트가 보는 위치는 코드에 고정돼 있어 바꿀 수 없습니다. 홈 아래 `~/.hermes/`에 다음이 생깁니다.

- `scripts/product_model_gate.py`, `scripts/model_routing.py`
- `CRITICAL_OPERATING_CONTRACT.md` (계약), `MODEL_ROUTING_STATE.md` (쓸 수 있는 모델 목록)
- `cron/state/model-quota-latest.json` (쿼터 스냅샷. 45분 안에 갱신돼야 하므로 heartbeat 크론 1줄 필요)

원작자 환경에서는 Hermes가 구독별 잔여 쿼터를 측정해 이 파일을 씁니다. 배포판에는 측정기가 없으므로
`heartbeat`는 "살아 있음"만 기록합니다(모든 창 remaining 100). 한도 소진은 모델 쪽 오류로 드러나며
뉴스 전환 사슬이 그 경우를 처리합니다. 이 점은 숨기지 않고 그대로 적어 둡니다.

코드를 갱신(git pull)하면 검토 파일 해시가 달라져 호출이 멈춥니다. 의도된 동작이며
`issue --reissue`로 다시 발급하면 됩니다. 발급은 자기 설치본에 대한 자기 승인이고 독립 검토가 아닙니다.
승인 파일 안의 `review`에 그렇게 기록됩니다.
