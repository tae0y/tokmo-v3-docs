# Tokmo V3 — Pulse G2 Shadow Runbook

## 개요

이 문서는 60초 Pulse G2 shadow harness 운영 절차를 정의한다. G2는 isolated root에서
sanitized fixture로 30+ tick을 실행하여 receipt/latency/ceiling을 검증하는 단계다.

## Rolling pause marker (자동 알림 없음)

Pulse는 rolling 5분 wire ceiling을 초과하면 **자동으로 pause marker 파일을 생성**한다.
단, **시스템 알림은 발생하지 않는다**. operator가 marker 존재를 능동적으로 확인해야 한다.

marker 생성 조건: rolling wire starts >= ceiling **AND** provider_operations가 활성일 때.
provider_operations가 비활성(기본값)이면 marker는 생성되지 않는다.

300초 cadence lane은 영향받지 않는다.

### Marker 위치
```
<runtime_root>/runtime/pulse-paused.marker
```

### Marker 내용
```
rolling_ceiling_exceeded
```

### 자동 동작
1. rolling 5분 wire starts >= ceiling (5 × total_per_tick = 30) **AND** provider_operations 활성 시 marker 생성
2. 최초 초과 tick은 `FAILED/rolling_ceiling_exceeded`, 이후 marker 존재 tick은 `FAILED/paused` receipt로 즉시 종료 (provider I/O 0)
3. **시스템 알림은 발생하지 않는다** — operator가 receipt나 marker를 능동 확인해야 함
4. 300초 cadence는 계속 정상 동작

## Marker clear 절차 (수동)

**자동 clear 금지.** 다음 절차를 수동으로 수행한다:

1. rolling ceiling 초과 원인 확인:
   - pulse_tick_receipts.sqlite3에서 최근 5분 wire starts 집계
   - ceiling 초과를 발생시킨 operation 확인 (TOKEN/PRICE_BATCH/ORDERS_PAGE/ORDER_DETAIL)
   - provider 장애·재시도 없는지 확인 (retry 0이므로 정상 호출 패턴이어야 함)

2. marker 및 receipt 확인:
   - `<runtime_root>/runtime/pulse-paused.marker` 존재 여부 확인
   - pulse_tick_receipts.sqlite3에서 `rolling_ceiling_exceeded` receipt의 시각과 reason 확인
   - **ops_kernel alert_outbox는 Pulse pause 알림을 발생시키지 않으므로 확인 대상이 아니다**

3. 원인 해결 후 marker 삭제:
   ```bash
   rm <runtime_root>/runtime/pulse-paused.marker
   ```

4. 다음 Pulse tick이 정상 resume하는지 확인:
   - receipt가 `SAVED` 또는 `SKIPPED_CLOSED`로 기록되는지 확인
   - rolling wire starts가 ceiling 이내인지 확인

## G2 shadow harness 실행

```bash
cd projects/tokmo-v3
.venv/bin/python -m pytest tests/test_pulse_g2_shadow_harness.py -v
```

### G2 판정 기준
- 30 tick green (SAVED 또는 SKIPPED_CLOSED) — 동일 persistent root 연속 실행 기준
- duplicate wire 0
- ceiling 위반 0
- rolling pause marker 미생성 (provider_operations 비활성 시 생성되지 않음)

## 주의사항

- Pulse cron이 등록되어 있어도 registry가 owner를 부여하지 않은 stage는 provider I/O 0
- cron 파일 추가 ≠ Hermes cron 설치
- canonical production root 변경 금지 — G2는 isolated root만 사용
- G2.5 bounded live canary는 별도 승인 gate
