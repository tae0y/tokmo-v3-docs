"""Tracer SAVE/SKIP policy orchestration (policy_version=tracer1-v1)."""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from tokmo.observability import TracerLogger
from tokmo.records.decision_journal import DecisionJournal
from tokmo.records.fact_store import FactStore
from tokmo.values import (
    DecisionRecord, KrxDailyTradeFact, OpenDartDisclosureFact, SaveResult, SaveStatus,
    SecFilingFact, UnavailableFact, WtiFact,
)

FactResult = WtiFact | SecFilingFact | OpenDartDisclosureFact | KrxDailyTradeFact | UnavailableFact

POLICY_VERSION = "tracer1-v1"

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


@dataclass(frozen=True)
class ProgramResult:
    fact: FactResult
    save_result: SaveResult | None
    decision: DecisionRecord


class AuditJournalError(RuntimeError):
    """The decision journal append failed after the store action was taken.

    Fact store and JSONL journal cannot be updated cross-file atomically, so
    this partial failure is surfaced fail-closed instead of returning a
    ProgramResult that pretends the audit trail is complete. The message
    carries no fact values or secrets.
    """

    def __init__(self, *, action: str, decision_id: str) -> None:
        self.action = action
        self.decision_id = decision_id
        super().__init__("journal_append_failed")


def run_tracer(fact_result: FactResult, *,
               store: FactStore, journal: DecisionJournal,
               clock: Clock, id_factory: IdFactory, run_id: str,
               logger: TracerLogger | None = None) -> ProgramResult:
    decided_at = clock()
    decision_id = id_factory()

    if isinstance(fact_result, UnavailableFact):
        save_result = None
        action = "SKIP"
        reason = f"SKIP:{fact_result.reason}"
        source_ref = fact_result.source
    else:
        save_result = store.save(fact_result, policy_version=POLICY_VERSION)
        action = "SAVE"
        reason = save_result.reason
        source_ref = fact_result.source_ref

    decision = DecisionRecord(
        decision_id=decision_id,
        run_id=run_id,
        source_ref=source_ref,
        action=action,
        reason=reason,
        observed_at=fact_result.observed_at,
        decided_at=decided_at,
        policy_version=POLICY_VERSION,
    )
    try:
        journal.append(decision)
    except OSError as exc:
        if logger is not None:
            with contextlib.suppress(Exception):
                logger.error(run_id=run_id, stage="journal", action=action,
                             reason="journal_append_failed",
                             source=fact_result.source, decision_id=decision_id)
        raise AuditJournalError(action=action, decision_id=decision_id) from exc

    if logger is not None:
        save_failed = save_result is not None and save_result.status is SaveStatus.FAILED
        emit = logger.error if save_failed else logger.info
        emit(run_id=run_id, stage="decision", action=action, reason=reason,
             source=fact_result.source, decision_id=decision_id)
    return ProgramResult(fact_result, save_result, decision)
