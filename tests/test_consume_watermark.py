from datetime import datetime, timedelta, timezone

import pytest

from tokmo.records.consume_watermark import ConsumeWatermarkStoreV1


T = datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_bootstrap_and_successful_advance_are_durable_per_scope(tmp_path):
    path = tmp_path / "consume.sqlite3"
    store = ConsumeWatermarkStoreV1(path, clock=lambda: T)
    store.bootstrap(source="toss-orders-v1", account_scope="toss-personal-a",
                    cutover_epoch_utc=T)
    assert store.read(source="toss-orders-v1",
                      account_scope="toss-personal-a").last_consumed_stored_at == T
    store.advance(
        source="toss-orders-v1", account_scope="toss-personal-a",
        last_consumed_stored_at=T + timedelta(minutes=5),
        advanced_at=T + timedelta(minutes=5), tick_id=123)
    reopened = ConsumeWatermarkStoreV1(path, clock=lambda: T)
    assert reopened.read(source="toss-orders-v1",
                         account_scope="toss-personal-a").tick_id == 123


def test_watermark_refuses_backward_advance(tmp_path):
    store = ConsumeWatermarkStoreV1(tmp_path / "consume.sqlite3", clock=lambda: T)
    store.bootstrap(source="toss-orders-v1", account_scope="toss-personal-a",
                    cutover_epoch_utc=T)
    with pytest.raises(ValueError):
        store.advance(
            source="toss-orders-v1", account_scope="toss-personal-a",
            last_consumed_stored_at=T - timedelta(seconds=1),
            advanced_at=T, tick_id=1)
