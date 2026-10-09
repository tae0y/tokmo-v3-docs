"""NEWS GLM admission and content-free receipts under the existing worker lock.

No policy overrides, synthetic identity, approval issuance or transport live here.
An unfinished CALL survives worker restarts and blocks every NEWS model until
its outcome has been reconciled. The controller owns that reconciliation.
"""

import importlib.util
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from tokmo.records.quant_decision_reports import atomic_private

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPROVAL = PROJECT_ROOT / "ops/news-product-approval.json"
GATE_PATH = Path.home() / ".hermes/scripts/product_model_gate.py"


class NewsProductGateError(RuntimeError):
    """Unclassified by ModelChain: admission/evidence errors never use fallback."""


def installed_gate():
    spec = importlib.util.spec_from_file_location("news_common_product_gate", GATE_PATH)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def receipt_root(diagnostic_path):
    diagnostic = (
        Path(diagnostic_path)
        if diagnostic_path is not None
        else (
            Path.home()
            / ".local/share/tokmo-v3/news/decision-worker/inference-diagnostics/analyze-last-error.json"
        )
    )
    return diagnostic.parent / "admissions"


def require_complete_receipts(diagnostic_path):
    """Shared by GLM and nonGLM stages; no new persistent state or automatic retry.

    Use iterdir, not glob (which may suppress filesystem errors). Successful
    atomic_private writes are the only way a RESULT is installed by this adapter.
    """
    root = receipt_root(diagnostic_path)
    try:
        try:
            root.stat()
        except FileNotFoundError:
            return
        for call in root.iterdir():
            if call.name.startswith("news-glm-") and call.name.endswith("-CALL.json"):
                result = call.with_name(
                    call.name.removesuffix("-CALL.json") + "-RESULT.json"
                )
                if not result.is_file():
                    raise NewsProductGateError("NEWS_MODEL_RESULT_UNRESOLVED")
    except OSError:
        raise NewsProductGateError("NEWS_MODEL_RECEIPT_UNREADABLE") from None


def _admission_error(error):
    # Do not propagate provider-like categories, exception contents or credentials.
    return NewsProductGateError(
        "NEWS_MODEL_ADMISSION_DENIED:" + getattr(error, "code", type(error).__name__)
    )


@contextmanager
def glm_call(model, stage, diagnostic_path, *, correction=False):
    """Persist intent, yield the immediate pre-transport recheck, persist outcome.

    SUCCEEDED means transport returned; existing decoder/validation runs afterwards.
    A RESULT write error supersedes even a classified provider failure, so neither
    success nor a fallback can escape while the CALL remains unfinished.
    """
    require_complete_receipts(diagnostic_path)
    try:
        gate = installed_gate()
        admission = gate.admit(APPROVAL, model, provider="zai")
        scope, route = admission["scope"], admission["route"]
        if (
            scope["identity_synthetic"]
            or scope["product"] != "tokmo-news"
            or scope["runtime_root"] != str(PROJECT_ROOT)
            or route["model"] != model
            or route["provider"] != "zai"
        ):
            raise NewsProductGateError("NEWS_SCOPE_MISMATCH")
    except Exception as error:
        raise _admission_error(error) from None
    call_id = "news-glm-" + uuid.uuid4().hex
    root = receipt_root(diagnostic_path)
    receipt = dict(
        schema="news-product-call-v1",
        call_id=call_id,
        model=model,
        provider="zai",
        stage=stage,
        correction=bool(correction),
        admission=admission,
    )

    def write(kind, **fields):
        try:
            atomic_private(
                root / (call_id + "-" + kind + ".json"),
                dict(receipt, status=kind, at=datetime.now(UTC).isoformat(), **fields),
            )
        except Exception:
            raise NewsProductGateError(
                "NEWS_MODEL_" + kind + "_RECEIPT_FAILED"
            ) from None

    write("CALL", outcome="INTENT", transport_started=False)
    started = False

    def recheck():
        nonlocal started
        try:
            gate.recheck(APPROVAL, admission)
        except Exception as error:
            raise _admission_error(error) from None
        started = True

    try:
        yield recheck
    except BaseException as error:
        write(
            "RESULT",
            outcome="ERROR" if started else "NOT_SENT",
            transport_started=started,
            error_type=type(error).__name__,
        )
        raise
    else:
        write("RESULT", outcome="SUCCEEDED", transport_started=started)
