"""Thin N1 values-to-records orchestration seam."""

from pathlib import Path

from tokmo.records.external_news import ExternalNewsStore
from tokmo.values.external_news import NewsReceipt, NewsWrite


def bootstrap_external_news(path: Path, *, now) -> None:
    ExternalNewsStore(path).bootstrap(now=now)


def save_external_news(path: Path, value: NewsWrite) -> NewsReceipt:
    return ExternalNewsStore(path).save_direct_url(value)
