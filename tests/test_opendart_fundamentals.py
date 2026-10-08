"""Tests for OpenDART fundamentals typed contracts.

KR is PAUSED (SDS §4.4). These typed contracts and tests exist without
invented endpoints. The adapter exists as a structural placeholder that
returns PAUSED unavailability — no network calls, no invented API paths.
"""

import pytest

from tokmo.facts.opendart_fundamentals import (
    OPENDART_FUNDAMENTALS_PAUSED_REASON,
    OPENDART_FUNDAMENTALS_STATUS,
    OpenDartFundamentalsResult,
    build_paused_result,
)


class TestOpenDARTFundamentalsPaused:
    def test_status_is_paused(self):
        assert OPENDART_FUNDAMENTALS_STATUS == "PAUSED"

    def test_paused_reason_is_kr_classifier_unresolved(self):
        assert OPENDART_FUNDAMENTALS_PAUSED_REASON == "kr_classifier_unresolved"

    def test_paused_result_has_no_materials(self):
        result = build_paused_result()
        assert isinstance(result, OpenDartFundamentalsResult)
        assert result.materials is None
        assert result.unavailable_reason == OPENDART_FUNDAMENTALS_PAUSED_REASON

    def test_paused_result_is_immutable(self):
        result = build_paused_result()
        with pytest.raises(AttributeError):
            result.materials = ()  # type: ignore[misc]
