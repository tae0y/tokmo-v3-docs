from tools.cadence_alert_gate import record_failure, record_success


def test_failure_fingerprint_cooldown_reason_change_and_recovery(tmp_path):
    state = tmp_path / "cadence-alert.json"

    emit, message = record_failure(
        state,
        fingerprint="watchlist:duplicate_symbol",
        detail="reason=watchlist_preflight_failed_duplicate_symbol",
        now=100,
    )
    assert emit is True
    assert "TOKMO_CADENCE_FAILED" in message
    assert "consecutive=1" in message

    emit, message = record_failure(
        state,
        fingerprint="watchlist:duplicate_symbol",
        detail="reason=watchlist_preflight_failed_duplicate_symbol",
        now=200,
    )
    assert (emit, message) == (False, "")

    emit, message = record_failure(
        state,
        fingerprint="credential:missing",
        detail="reason=credential_preflight_failed",
        now=300,
    )
    assert emit is True
    assert "credential_preflight_failed" in message
    assert "consecutive=1" in message

    emit, message = record_failure(
        state,
        fingerprint="credential:missing",
        detail="reason=credential_preflight_failed",
        now=3900,
    )
    assert emit is True
    assert "TOKMO_CADENCE_REMINDER" in message
    assert "consecutive=2" in message

    emit, message = record_success(state, now=4000)
    assert emit is True
    assert "TOKMO_CADENCE_RECOVERED" in message
    assert "consecutive=2" in message

    assert record_success(state, now=4100) == (False, "")
