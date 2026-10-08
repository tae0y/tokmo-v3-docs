# Stage3 FRED offline TDD ledger

All commands run from the repository root with no network/provider access.

## Slice VAL-01 — closed typed request/value vocabulary

- RED command: `.venv/bin/pytest -q tests/test_fred_values.py`
- Expected failure: collection fails because `tokmo.values.fred_macro` does not yet exist.
- Observed: `ModuleNotFoundError: No module named 'tokmo.values.fred_macro'` (exit 2).
- GREEN command: `.venv/bin/pytest -q tests/test_fred_values.py`
- GREEN observed: `1 passed`.

## Slice PAR-01 — five operation builders and truthful echo parsers

- RED command: `.venv/bin/pytest -q tests/test_fred_fact.py`
- Expected failure: collection fails because `tokmo.facts.fred` does not yet exist.
- Observed: `ModuleNotFoundError: No module named 'tokmo.facts.fred'` (exit 2).
- GREEN command: `.venv/bin/pytest -q tests/test_fred_fact.py tests/test_fred_values.py`
- GREEN observed after correcting the mutation fixture to retain all required provider keys: `2 passed`.

## Slice NET-01 — credential grammar, exact one-call wire, static failures

- RED command: `.venv/bin/pytest -q tests/test_fred_live.py`
- Expected failure: collection fails because `tokmo.facts.fred_live` does not yet exist.
- Observed: `ModuleNotFoundError: No module named 'tokmo.facts.fred_live'` (exit 2).
- GREEN command: `.venv/bin/pytest -q tests/test_fred_live.py tests/test_fred_fact.py tests/test_fred_values.py`
- GREEN observed: `3 passed`.

## Slice REC-01 — atomic reservation and immutable material/acquisition split

- RED command: `.venv/bin/pytest -q tests/test_fred_material_store.py`
- Expected failure: collection fails because `tokmo.records.fred_material_store` does not yet exist.
- Observed: `ModuleNotFoundError: No module named 'tokmo.records.fred_material_store'` (exit 2).
- GREEN command: `.venv/bin/pytest -q tests/test_fred_material_store.py`
- GREEN observed after accepting concrete `Path` subclasses: `1 passed`.

## Slice CMP-01 — reserve-before-I/O composition and zero-start replay

- RED command: `.venv/bin/pytest -q tests/test_fred_composition.py`
- Expected failure: collection fails because `tokmo.fred_composition` does not yet exist.
- Observed: `ModuleNotFoundError: No module named 'tokmo.fred_composition'` (exit 2).
- GREEN command: `.venv/bin/pytest -q tests/test_fred_composition.py tests/test_fred_material_store.py`
- GREEN observed: `2 passed`.

## Slice ARCH-01 — exact layer seals and CLI leaf

- RED command: `.venv/bin/pytest -q tests/test_fred_architecture.py`
- Expected failure: `tools/fred_canonical.py` is absent and the architecture registration delta is not yet applied.
- Observed: two focused failures, both identifying absent `tools/fred_canonical.py`.
- GREEN command: `.venv/bin/pytest -q tests/test_fred_architecture.py tests/test_architecture.py`
- GREEN observed: `27 passed`.

## Acceptance reinforcement — REC-10 and provenance-limited mutations

- Focused command: `.venv/bin/pytest -q tests/test_fred_material_store.py tests/test_fred_fact.py`
- Expected: cap-minus-one concurrent reservations yield exactly one `reserved`; wrong provider-echoed release fails; a correct-looking unrequested dynamic vintage remains accepted only as `SHAPE_UNVERIFIED` request-derived evidence.
- First observed: the test concurrently reconstructed schemas and hit the existing private primitive's `database is locked`; corrected the fixture to race only the reservation behavior through the already initialized store.
- GREEN observed: `4 passed`.

## Final fresh verification (recorded after final edit)

- Focused: `.venv/bin/pytest -q tests/test_fred_values.py tests/test_fred_fact.py tests/test_fred_live.py tests/test_fred_material_store.py tests/test_fred_composition.py tests/test_fred_architecture.py`
- Architecture: `.venv/bin/pytest -q tests/test_architecture.py`
- Private-primitive siblings: `.venv/bin/pytest -q tests/test_macro_runtime_receipts.py tests/test_fundamental_material_store.py tests/test_material_pack_store.py`
- Full: `.venv/bin/pytest -q tests`
- Compile: `.venv/bin/python -m compileall -q` over touched `src` and `tools` leaves.

## Opus repair assertion-level RED -> GREEN evidence

The historical import/missing-file rows above are retained verbatim. They are not counted as assertion-level RED evidence.

- M-3/m-7/m-8 parser command: `.venv/bin/pytest -q tests/test_fred_fact.py`
  - RED: `Failed: DID NOT RAISE ValueError` for the unrequested `PAYEMS_20260102` vintage; `output_type=None` raised `dynamic_shape_mismatch`; duplicate release dates were accepted.
  - GREEN: corrected wrong-vintage rejection, standard output-1 routing, and duplicate/order validation pass.
- M-2/M-1 CLI command: `.venv/bin/pytest -q tests/test_fred_architecture.py`
  - RED: `run_cli() got an unexpected keyword argument 'api_key_loader'`; missing explicit root attempted to open the fixture; rerun returned generic `already_attempted`; CLI path exposed a usable `--live` option.
  - GREEN: explicit root fails closed before fixture access; one fixture operation succeeds; deterministic rerun reports `already_attempted_fixture` with zero starts; `--live` is rejected before parsing/loading.
- M-5 NET command: `.venv/bin/pytest -q tests/test_fred_live.py`
  - RED: post-build mutated request returned `success`; opener proxy-disable assertion found no inspectable proxy state.
  - GREEN: completed wire is reparsed before opener; proxy absence/empty mapping and redirect refusal are asserted; redirect/auth/429/5xx/400/other, timeout, generic transport, encoding, body cap, non-200, one-call, timeout, query order, and sanitized result branches pass.
- M-1/m-3 provenance command: `.venv/bin/pytest -q tests/test_fred_material_store.py::test_fixture_attempts_and_material_cannot_satisfy_live_identity_or_cap`
  - RED: live reservation received ordinal 2 after fixture ordinal 1, proving a shared cap/campaign.
  - GREEN: FIXTURE and LIVE have distinct invocation/material identities and independent ordinal/cap namespaces; both durable modes remain queryable.
- m-1 pacing command: `.venv/bin/pytest -q tests/test_fred_material_store.py::test_durable_wall_start_spacing_reserves_before_io`
  - RED: `reserve() got an unexpected keyword argument 'started_at'`.
  - GREEN: a start at +0.5 seconds returns `pacing_required`; +1.0 seconds reserves; reservation precedes I/O.
- m-2 evidence command: `.venv/bin/pytest -q tests/test_fred_material_store.py::test_release_date_evidence_is_preserved_without_inventing_an_instant`
  - RED: `source_release_date_evidence` was `None` instead of `2026-01-02`.
  - GREEN: date-only evidence is persisted, `available_at=fetched_at`, and no publication instant is invented.
- m-3/m-4/m-6/m-9 record command: `.venv/bin/pytest -q tests/test_fred_material_store.py::test_fixture_provenance_join_reconciliation_and_zero_rows_fail_closed tests/test_fred_material_store.py::test_store_page_real_invocation_join_and_conflict_rolls_back`
  - RED: initial collection exposed the missing execution-mode contract and was not counted; after the contract existed, assertions exposed generic rerun identity, missing invocation join, accepted empty page, and absent singleton completeness columns.
  - GREEN: UNKNOWN reconciliation consumes the start, empty pages fail `empty_page`, acquisitions join attempts, and singleton batches persist row count with NULL provider count and official-envelope evidence.
- m-11 concurrency command: `.venv/bin/pytest -q tests/test_fred_material_store.py::test_cap_minus_one_concurrency_allows_exactly_one_start`
  - RED method: the prior test lacked a rendezvous and could pass serially; adding `threading.Barrier(2)` makes serial execution time out at the assertion seam.
  - GREEN: two workers rendezvous before reservation and yield exactly one `reserved` and one `cap_exhausted`.
- m-5 architecture command: `.venv/bin/pytest -q tests/test_fred_architecture.py tests/test_architecture.py`
  - RED: keyword-only defaults were not inspected and the added CLI test path violated the closed FRED path set.
  - GREEN: `kwonlyargs/kw_defaults` are inspected and CLI coverage resides inside the existing SDS allowlist.
- m-8 post-commit evidence failure command: `.venv/bin/pytest -q tests/test_fred_composition.py::test_composition_failure_codes_are_truthful`
  - RED: injected evidence write failure committed the SQLite batch, then the failure handler raised `ValueError: settlement_conflict` instead of returning a truthful outcome.
  - GREEN: only that already-settled conflict is absorbed and the operation returns `store_failure`; other settlement errors still propagate.

## Repair final fresh verification

- Focused FRED + architecture + private durability siblings: `.venv/bin/pytest -q tests/test_fred_values.py tests/test_fred_fact.py tests/test_fred_live.py tests/test_fred_material_store.py tests/test_fred_composition.py tests/test_fred_architecture.py tests/test_architecture.py tests/test_macro_runtime_receipts.py tests/test_fundamental_material_store.py tests/test_material_pack_store.py` -> `87 passed`.
- Full suite after the final behavioral edit: `.venv/bin/pytest -q tests` -> `4557 passed, 1 skipped` in `198.58s`; owned FRED skip/xfail count is zero.
- The first full run found one owned assertion bug in the new exact-delta test (`architecture.count(...)` observed 2, expected 1). The assertion was repaired to inspect the runtime registry and actual importer graph; targeted GREEN was `2 passed`, followed by the fresh full-suite GREEN above.
- After the final post-commit evidence-failure repair, the focused matrix was `88 passed`; the final fresh full suite was `4557 passed, 1 skipped` in `198.77s`.

## m-8 atomic rollback micro-repair

- Coverage-only closure, not RED: production already rolled back the exercised `store_page` transaction atomically, so the newly added assertion passed on its first run. No production mutation was made or needed.
- First assertion command: `.venv/bin/pytest -q tests/test_fred_material_store.py::test_store_page_immutable_conflict_rolls_back_entire_transaction` -> `1 passed in 0.83s`.
- Targeted material-store command: `.venv/bin/pytest -q tests/test_fred_material_store.py` -> `8 passed in 0.13s`.
- Full FRED focused matrix command: `.venv/bin/pytest -q tests/test_fred_values.py tests/test_fred_fact.py tests/test_fred_live.py tests/test_fred_material_store.py tests/test_fred_composition.py tests/test_fred_architecture.py tests/test_architecture.py tests/test_macro_runtime_receipts.py tests/test_fundamental_material_store.py tests/test_material_pack_store.py` -> `89 passed in 5.67s`.
- The rollback test commits an initial page, snapshots `materials`, `acquisitions`, `batches`, `attempts`, and the evidence receipt, then processes a new row before a same-identity mutated row raises `immutable_conflict`; reopening proves the complete state is byte/row-for-row unchanged and no partial material, acquisition, or batch survived.
