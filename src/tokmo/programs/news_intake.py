"""Program seam from typed NEWS fetch results to private N1 storage."""

from pathlib import Path

from tokmo.records.external_news import ExternalNewsStore
from tokmo.values.external_news import (
    ContentMode, ContentState, LicenseState, NewsReceipt, NewsWrite,
)
from tokmo.values.news_connectors import (
    NewsConnectorSpec, NewsFetchResult, NewsFetchStatus,
)


def persist_news_result(
    path: Path, spec: NewsConnectorSpec, result: NewsFetchResult, *,
    target_ref: str, on_saved=None,
) -> tuple[NewsReceipt, ...]:
    """Persist a collected metadata batch item-by-item with atomic receipts.

    Empty and failed fetches intentionally produce no synthetic success receipt;
    the caller retains the typed fetch result as the invocation outcome.
    """
    if result.status is not NewsFetchStatus.COLLECTED:
        return ()
    store = ExternalNewsStore(path)
    receipts = []
    with store.connector_batch():
        for item in result.items:
            value = NewsWrite(
                canonical_url=item.canonical_url,
                source_article_id=item.source_article_id,
                publisher_name=item.publisher_name,
                publisher_site_url=item.publisher_site_url,
                title=item.title,
                summary_text=item.summary_text,
                content_mode=item.content_mode,
                content_state=ContentState.METADATA_ONLY,
                license_state=LicenseState.UNKNOWN,
                published_at=item.published_at,
                observed_at=item.observed_at,
                evidence_role=item.evidence_role,
                alert_eligible=item.alert_eligible,
                time_interpretation=item.time_interpretation,
            )
            receipts.append(store.save_connector_news(
                spec, value, target_ref=target_ref,
                bytes_received=result.bytes_received,
            ))
            if on_saved is not None:
                on_saved(receipts[-1])
    return tuple(receipts)
