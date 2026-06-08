#!/usr/bin/env python3
"""Sensitivity model for the linearizable ledger writes in CertificateAdmit."""

from __future__ import annotations

import csv
import json
from pathlib import Path


OUT_JSON = Path(__file__).with_name("ledger_latency_sensitivity.json")
OUT_CSV = Path(__file__).with_name("ledger_latency_sensitivity.csv")


def build_rows(workers: int = 16, writes_per_report: int = 2) -> list[dict[str, float | int]]:
    rows = []
    for latency_ms in [1, 5, 10, 25, 50]:
        added_ms = writes_per_report * latency_ms
        single_worker_admits = 1000.0 / added_ms
        rows.append(
            {
                "linearizable_write_p95_ms": latency_ms,
                "writes_per_report": writes_per_report,
                "added_p95_ms_per_report": added_ms,
                "single_worker_upper_bound_admits_per_s": single_worker_admits,
                "workers": workers,
                "parallel_upper_bound_admits_per_s": workers * single_worker_admits,
                "ledger_writes_per_million_reports": writes_per_report * 1_000_000,
            }
        )
    return rows


def main() -> None:
    rows = build_rows()
    payload = {
        "artifact": "ledger_latency_sensitivity",
        "description": "upper-bound throughput sensitivity for two linearizable CertificateAdmit writes per report",
        "notes": [
            "transparent sizing model, not a replicated-store measurement",
            "excludes local verification CPU, aborts, retries, batching, and network fanout beyond the write latency parameter",
        ],
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
