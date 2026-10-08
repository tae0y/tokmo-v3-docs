from datetime import datetime, timezone
import fcntl
import os
import pytest

from tokmo.programs.analysis_pipeline import run_analysis_pipeline
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.analysis_contracts import AnalysisRunState
from test_analysis_pipeline import glm_ok, sol_ok
from test_candidate_builder import _barrier
from tokmo.values.portfolio_scope import resolve_account_scope_pattern

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def test_sixty_tick_local_cadence_is_bounded_and_deterministic(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    lock = tmp_path / "run" / "analysis.lock"
    for tick_id in range(10, 70):
        barrier = _barrier(tick=tick_id)
        outcome = run_analysis_pipeline(
            tick=barrier, barrier=barrier, store=store, run_lock_path=lock,
            invocation_id_factory=lambda tick_id=tick_id: f"{tick_id:032x}",
            glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
            resolve_scope=resolve_account_scope_pattern,
        )
        assert outcome.state is AnalysisRunState.COMPLETED
    assert sum(len(store.read_analysis_batches(tick)) for tick in range(10, 70)) <= 660
    assert lock.stat().st_size == 0


def test_public_pipeline_lock_contention_has_zero_store_writes(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    lock = tmp_path / "run" / "analysis.lock"
    lock.parent.mkdir(mode=0o700)
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        barrier = _barrier()
        outcome = run_analysis_pipeline(
            tick=barrier, barrier=barrier, store=store, run_lock_path=lock,
            invocation_id_factory=lambda: "a" * 32,
            glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
        )
        assert outcome.state is AnalysisRunState.LOCK_CONTENDED
        assert store.read_manifest_summary(10) is None
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _install_crash(store, method_name, *, after=False, injected=None):
    original = getattr(store, method_name)
    fired = {"value": False}

    def crash(*args, **kwargs):
        if fired["value"]:
            return original(*args, **kwargs)
        fired["value"] = True
        if injected is not None:
            kwargs["inject_crash_at"] = injected
            return original(*args, **kwargs)
        if not after:
            raise RuntimeError("matrix crash")
        result = original(*args, **kwargs)
        raise RuntimeError("matrix crash")

    setattr(store, method_name, crash)


@pytest.mark.parametrize(
    ("row_id", "method_name", "after", "injected", "model_exit"),
    (
        ("1", None, False, None, None),
        ("2", "reconcile_analysis_batches", True, None, None),
        ("4", None, False, None, None),
        ("5", "create_manifest_with_baselines", False, "after_manifest", None),
        ("6", "create_manifest_with_baselines", True, None, None),
        ("7", "seal_manifest", False, None, None),
        ("8", "plan_analysis_batches", False, None, None),
        ("9", None, False, None, "stub-crash"),
        ("10", "settle_analysis_batch", True, None, None),
        ("11", "record_disposition_with_outbox", True, None, None),
        ("12", "upsert_deferred", False, None, "timeout"),
        ("13", "write_audit_survey", True, None, None),
    ),
)
def test_te12_public_crash_reopen_matrix_rows(
    tmp_path, monkeypatch, row_id, method_name, after, injected, model_exit,
):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    barrier = _barrier()
    calls = {"glm": 0, "sol": 0}

    if row_id == "4":
        import tokmo.programs.analysis_pipeline as module
        original_build = module.build_candidates
        fired = {"value": False}

        def crash_build(*args, **kwargs):
            if not fired["value"]:
                fired["value"] = True
                raise RuntimeError("matrix crash")
            return original_build(*args, **kwargs)

        monkeypatch.setattr(module, "build_candidates", crash_build)
    elif method_name is not None:
        _install_crash(
            store, method_name, after=after, injected=injected,
        )

    def glm(body):
        calls["glm"] += 1
        if model_exit == "stub-crash":
            raise RuntimeError("matrix crash")
        if model_exit == "timeout":
            from tokmo.values.analysis_contracts import ProviderExit
            return ProviderExit.TIMEOUT, None
        return glm_ok(body)

    def sol(body):
        calls["sol"] += 1
        return sol_ok(body)

    invocation = (
        (lambda: (_ for _ in ()).throw(RuntimeError("matrix crash")))
        if row_id == "1" else (lambda: "a" * 32)
    )
    with pytest.raises(RuntimeError, match="matrix crash|injected"):
        run_analysis_pipeline(
            tick=barrier, barrier=barrier, store=store,
            run_lock_path=tmp_path / "run" / "analysis.lock",
            invocation_id_factory=invocation, glm_call=glm, sol_call=sol,
            now=NOW, clock=lambda: NOW,
        )

    reopened = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    outcome = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=reopened,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=(
            (lambda _body: (_ for _ in ()).throw(
                AssertionError("durable model ownership forbids recall")
            )) if row_id in {"9", "10", "11", "12", "13"} else glm_ok
        ),
        sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    candidates = reopened.read_manifest_candidates(10)
    assert candidates
    assert all(row.state.value != "PENDING" for row in candidates)
    dispositions = reopened.read_dispositions_for_tick(10)
    assert len(dispositions) == len({row.candidate_id for row in dispositions})


@pytest.mark.parametrize("row_id", ("2u", "3"))
def test_te12_public_crash_reopen_matrix_past_candidate_rows(
    tmp_path, row_id,
):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    first = _barrier(tick=10)
    if row_id == "2u":
        with pytest.raises(RuntimeError, match="owner crash"):
            run_analysis_pipeline(
                tick=first, barrier=first, store=store,
                run_lock_path=tmp_path / "run" / "analysis.lock",
                invocation_id_factory=lambda: "a" * 32,
                glm_call=lambda _body: (_ for _ in ()).throw(
                    RuntimeError("owner crash")
                ),
                sol_call=sol_ok, now=NOW, clock=lambda: NOW,
            )
    else:
        _install_crash(store, "plan_analysis_batches")
        with pytest.raises(RuntimeError, match="matrix crash"):
            run_analysis_pipeline(
                tick=first, barrier=first, store=store,
                run_lock_path=tmp_path / "run" / "analysis.lock",
                invocation_id_factory=lambda: "a" * 32,
                glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
            )

    reopened = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    _install_crash(reopened, "record_disposition_with_outbox", after=True)
    second = _barrier(tick=11)
    with pytest.raises(RuntimeError, match="matrix crash"):
        run_analysis_pipeline(
            tick=second, barrier=second, store=reopened,
            run_lock_path=tmp_path / "run" / "analysis.lock",
            invocation_id_factory=lambda: "b" * 32,
            glm_call=lambda _body: (_ for _ in ()).throw(
                AssertionError("past candidate must settle before model work")
            ),
            sol_call=sol_ok, now=NOW, clock=lambda: NOW,
        )

    final_store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    outcome = run_analysis_pipeline(
        tick=second, barrier=second, store=final_store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "c" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    old = final_store.read_manifest_candidates(10)
    assert old and all(row.state.value != "PENDING" for row in old)
    old_dispositions = final_store.read_dispositions_for_tick(10)
    assert len(old_dispositions) == len({
        row.candidate_id for row in old_dispositions
    })
