# tokmo-v3

토스증권 계좌 기준의 개인 투자 보조 시스템. 스케줄러 위에서 다음을 돌립니다.

- **토스 수집·펄스**: 보유·체결·조건주문을 1분 단위로 읽고, 텔레그램 스톡봇 대화로 매매 사유를 기록
- **뉴스 판단·알림**: 국내외 뉴스·공시를 수집해 GLM으로 선별·1차 분석하고, 필요할 때만 상위 모델로 확정. 중요 뉴스는 즉시, 나머지는 저녁 모음으로 텔레그램 발송
- **선택 후속 점검**: 사용자가 대화로 남긴 매매 선택을 새 뉴스·시세와 대조해 변화가 있을 때만 알림
- **회전매매**: 승인된 밴드 정책 안에서 소량 자동 주문(현재 현대차 1종목), 보호수량과 실험수량을 장부로 분리
- **모의투자(PAPER)**: 실주문 없이 원장으로만 운용하는 기회 탐색형 회전. 평일 10:05/13:30에 판단하고 슬롯마다 결과 요약과 검토 종목을 알림
- 부가: 퀀트 카드, OpenDART/SEC 공시, 매크로(FRED/BLS/EIA), 유튜브 자막 추적

## 문서
- 설치·운영·모듈 on/off·스케줄: [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md)
- 필요한 API 키와 위치: [`docs/API-KEYS.md`](docs/API-KEYS.md) (`tools/check_api_keys.py`로 점검)
- 버전: `pyproject.toml`의 `YYYYMMDD.N` = 릴리스 `vYYYYMMDD_NN`(배포 날짜와 그날의 순번). 받은 릴리스는 `RELEASE.json`에서 확인
- 설계 이력은 배포판에 넣지 않습니다(원작자의 실제 보유 정보와 대화가 인용돼 있어 원본 저장소에만 둠)

## 구조
```
src/tokmo/values     순수 계산·계약 (외부 I/O 없음)
src/tokmo/facts      외부 API 어댑터 (토스, KIS, OpenDART, SEC, 뉴스 소스, 텔레그램)
src/tokmo/records    SQLite/JSON 저장소 (0600, 심링크 거부)
src/tokmo/programs   흐름 (펄스, 캐던스, 뉴스 엔진, 후속 점검, 회전매매)
src/tokmo/composition.py   라이브 전송을 조립하는 유일한 지점
tools/               실행 진입점 (run_*.py)
ops/hermes-cron      스케줄러 래퍼 (자기 위치에서 프로젝트 루트를 찾음)
ops/cron             일반 cron 예시 + Hermes 대체 도구
var/  .secrets/      상태·로그·자격증명 (git 제외)
```

## 요구사항
Python 3.12, macOS 또는 Linux/WSL2, 토스증권 오픈API, AI 구독 1개 이상(Z.AI GLM 키 / 코덱스 / 클로드 CLI 로그인), 텔레그램 봇. 어떤 단계가 어떤 모델을 쓸지는 `var/data/models.json`(예시 `examples/models.json`)에서 정합니다. 상세는 `docs/API-KEYS.md`.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m pytest tests -q
```
