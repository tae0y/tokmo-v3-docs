# 펄스 일시정지 런북

이 문서는 펄스(60초 주기 수집)가 외부 호출 상한을 넘어 스스로 멈췄을 때 확인하고 다시 켜는 절차입니다. 격리된 환경에서 펄스를 검증하는 G2 shadow 시험 방법도 함께 적습니다.

## 일시정지 표식

펄스는 최근 5분 동안 외부 호출 시작 수(rolling wire starts)가 상한에 닿으면 표식 파일을 만들고 멈춥니다.

- 위치: `<runtime_root>/runtime/pulse-paused.marker`
- 내용: `rolling_ceiling_exceeded`
- 상한: tick당 전체 호출 수의 5배입니다(5 × `total_per_tick` = 30).
- 조건: provider operations가 켜져 있을 때만 표식을 만듭니다. 기본값인 꺼진 상태에서는 만들지 않습니다.

표식이 생기면 다음과 같이 동작합니다.

1. 처음 상한을 넘은 tick은 `FAILED/rolling_ceiling_exceeded` 영수증을 남깁니다.
1. 이후 표식이 있는 동안 모든 tick은 `FAILED/paused` 영수증을 남기고 외부 호출 없이 바로 끝납니다.
1. **시스템 알림은 보내지 않습니다.** 운영자가 영수증이나 표식 파일을 직접 확인해야 합니다.
1. 300초 주기의 캐던스는 영향을 받지 않고 계속 실행됩니다.

## 표식을 지우는 절차

표식은 자동으로 지워지지 않습니다. 아래 순서대로 직접 처리합니다.

1. 상한을 넘은 원인을 확인합니다.
    - `pulse_tick_receipts.sqlite3`에서 최근 5분 동안의 호출 시작 수를 집계합니다.
    - 어떤 호출(`TOKEN`, `PRICE_BATCH`, `ORDERS_PAGE`, `ORDER_DETAIL`)이 상한을 넘겼는지 확인합니다.
    - 공급자 장애나 재시도가 없었는지 확인합니다. 펄스는 재시도를 하지 않으므로 정상적인 호출 패턴이어야 합니다.
1. 표식과 영수증을 확인합니다.
    - `<runtime_root>/runtime/pulse-paused.marker`가 있는지 확인합니다.
    - `pulse_tick_receipts.sqlite3`에서 `rolling_ceiling_exceeded` 영수증의 시각과 사유를 확인합니다.
    - `ops_kernel`의 `alert_outbox`는 펄스 일시정지 알림을 만들지 않으므로 확인할 필요가 없습니다.
1. 원인을 해결한 뒤 표식을 지웁니다.

    ```bash
    rm <runtime_root>/runtime/pulse-paused.marker
    ```

1. 다음 tick이 정상으로 돌아왔는지 확인합니다.
    - 영수증이 `SAVED`나 `SKIPPED_CLOSED`로 기록되는지 봅니다.
    - 최근 5분 호출 시작 수가 상한 안에 있는지 봅니다.

## G2 shadow 시험

G2는 격리된 루트에서 정제된 fixture로 펄스를 30회 이상 실행해 영수증, 지연 시간, 호출 상한을 검증하는 단계입니다.

```bash
.venv/bin/python -m pytest tests/test_pulse_g2_shadow_harness.py -v
```

통과 기준은 다음과 같습니다.

- 같은 영구 루트에서 연속 30 tick이 `SAVED`나 `SKIPPED_CLOSED`로 끝납니다.
- 중복 호출이 0건입니다.
- 상한 위반이 0건입니다.
- 일시정지 표식이 생기지 않습니다(provider operations가 꺼져 있으면 생기지 않습니다).

## 주의할 점

- 펄스 cron이 등록돼 있어도, registry가 담당자를 지정하지 않은 단계는 외부 호출을 하지 않습니다.
- cron 파일을 추가하는 것과 Hermes cron에 설치하는 것은 다른 일입니다.
- 운영 루트는 바꾸지 않습니다. G2는 격리된 루트에서만 실행합니다.
- 실제 계좌로 짧게 돌려 보는 G2.5 시험은 별도 승인이 필요합니다.
