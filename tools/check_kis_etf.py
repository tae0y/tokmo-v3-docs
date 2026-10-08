"""Bounded, user-authorized KIS ETF connection check. No account/order calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from tokmo.kis_composition import build_kis_etf_client
from tokmo.records.atomic_json import write_atomic_json
from tokmo.values.kis_etf import KisError


def main():
    parser = argparse.ArgumentParser(description="KIS ETF 구성·NAV 조회 연결 검사 (최대 2종목)")
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".local/share/tokmo-v3/kis")
    args = parser.parse_args()
    symbols = list(dict.fromkeys(args.symbol or ["251600", "0138Y0"]))
    if not 1 <= len(symbols) <= 2:
        parser.error("한 번에 최대 2종목만 확인해")
    budget = 1 + 2 * len(symbols)
    report = {"schema": "kis-etf-connection-check-v1", "started_at": datetime.now(timezone.utc).isoformat(),
              "request_cap": budget, "symbols": symbols, "results": []}
    client = None
    try:
        client = build_kis_etf_client(project_root=Path(__file__).resolve().parents[1],
                                      state_root=args.state_root, max_requests=budget)
        for symbol in symbols:
            for operation in ("components", "nav"):
                fact = client.fetch(operation, symbol)
                result = {"symbol": symbol, "operation": operation, "status": fact["status"],
                          "row_count": len(fact["rows"]), "source_date": fact["source_date"], "freshness": fact["freshness"]}
                if operation == "nav" and fact["rows"]:
                    result["latest_returned_sample"] = max(fact["rows"], key=lambda row: row["source_time"])
                if operation == "components":
                    result.update(coverage=fact["coverage"], weight_basis=fact["weight_basis"],
                                  reported_component_count=fact["reported_component_count"])
                report["results"].append(result)
        report["status"] = "CONNECTED"
    except KisError as error:
        report.update(status="INCOMPLETE", code=error.code)
    except Exception:
        report.update(status="INCOMPLETE", code="local_or_protocol_failure")
    report.update(requests=0 if client is None else client.requests,
                  auth_requests=0 if client is None else client.auth_requests,
                  auth_status="not_started" if client is None else client.auth_status,
                  finished_at=datetime.now(timezone.utc).isoformat())
    output = json.dumps(report, ensure_ascii=False, indent=2).encode()
    # No credentials or authentication response is included in this receipt.
    if client is not None:
        write_atomic_json(args.state_root / "connection-check.json", output, max_bytes=65536)
    print(output.decode())
    return 0 if report["status"] == "CONNECTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
