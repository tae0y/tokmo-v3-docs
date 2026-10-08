# Critical Operating Contract (tokmo-v3 배포판)

이 문서는 제품 모델 게이트가 sha256으로 묶어 두는 운영 계약입니다. 내용을 바꾸면 발급된 승인 파일이
모두 무효가 되며(`AUTHORITY_DRIFT`), `tools/install_product_gate.py issue --reissue`로 다시 발급해야 합니다.

## AI 모델 호출

- 모델은 `~/.hermes/MODEL_ROUTING_STATE.md`의 `model-routing` 목록에 있는 정확한 id만 부릅니다.
- 제품(뉴스 판단, 모의투자, 퀀트 분석)은 각자의 승인 파일 범위 안의 모델과 진입점, 검토된 파일로만 호출합니다.
- 호출 전 CALL 영수증, 전송 직전 재검사, 호출 후 RESULT 영수증을 남깁니다. 영수증을 못 쓰면 호출하지 않습니다.
- 한도 소진, 인증 실패, 통신 장애에만 다음 모델로 넘어갑니다. 답이 이상하다고 넘어가지 않습니다.

## 주문

- 실주문은 `var/state/hyundai-rotation/live.flag`가 있고 증권사 주문 권한이 있을 때만 냅니다.
- 모의투자(PAPER)는 실제 주문을 내지 않습니다.

## 개인정보

- 알림과 영수증에 자격증명, 계좌번호, 대화 원문을 쓰지 않습니다.
