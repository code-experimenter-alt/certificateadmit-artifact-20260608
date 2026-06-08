#!/usr/bin/env python3
"""Write deployment cost-accounting units for one million admitted reports.

The output intentionally separates measured EC2/enclave runtime from provider-
priced services such as KMS, ledger writes, storage, audits, and source
admission. Current cloud prices are deployment-specific; the artifact records
the billable units that a deployment should multiply by its own rates.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path


OUT_JSON = Path(__file__).with_name("deployment_cost_accounting.json")
OUT_CSV = Path(__file__).with_name("deployment_cost_accounting.csv")


def build_rows(reports: int = 1_000_000, batch_size: int = 128, audit_rate: float = 0.01) -> list[dict[str, float | int | str]]:
    return [
        {
            "component": "measured_enclave_runtime",
            "unit": "USD per 1M reports",
            "count": 0.080,
            "basis": "scaled Nitro DirectAttest/TEEOnly EC2 runtime component only",
        },
        {
            "component": "kms_or_session_key_release",
            "unit": "requests",
            "count": math.ceil(reports / batch_size),
            "basis": f"ceil(reports / batch_size), reports={reports}, batch_size={batch_size}",
        },
        {
            "component": "linearizable_ledger_write",
            "unit": "writes",
            "count": 2 * reports,
            "basis": "source-token consumption plus terminal/counter/nonce-key admission",
        },
        {
            "component": "certificate_row_storage",
            "unit": "rows",
            "count": reports,
            "basis": "accepted/rejected certificate provenance rows before aggregation",
        },
        {
            "component": "source_token_issuance",
            "unit": "tokens",
            "count": reports,
            "basis": "one upstream source-admission token per admitted report slot",
        },
        {
            "component": "audit_operation",
            "unit": "audits",
            "count": int(round(reports * audit_rate)),
            "basis": f"audit_rate={audit_rate}",
        },
        {
            "component": "accepted_aggregation_input",
            "unit": "rows",
            "count": reports,
            "basis": "accepted-only reports consumed by LDP aggregation",
        },
    ]


def main() -> None:
    rows = build_rows()
    payload = {
        "artifact": "deployment_cost_accounting",
        "description": "billable-unit accounting for full certificate admission cost model",
        "assumption": "provider prices and replicated-ledger deployment choices are external parameters",
        "parameters": {"reports": 1_000_000, "batch_size": 128, "audit_rate": 0.01},
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["component", "unit", "count", "basis"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
