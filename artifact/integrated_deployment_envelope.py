#!/usr/bin/env python3
"""Component-derived end-to-end deployment envelope for CertificateAdmit.

The paper does not ship a live AWS/KMS/replicated-ledger deployment. This
artifact instead combines the measured and recorded component outputs already
in the artifact into conservative path envelopes. It is intentionally labeled
as a component-derived envelope, not an integrated cloud measurement.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
OUT_CSV = BASE / "integrated_deployment_envelope.csv"
OUT_JSON = BASE / "integrated_deployment_envelope.json"


RECORDED_NITRO_P95_MS = 18.9
RECORDED_NITRO_REPORTS_PER_S = 666.6
RECORDED_NITRO_RUNTIME_USD_PER_M = 0.080


def read_json(name: str) -> dict:
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def best_worker_row(data: dict, workers: int) -> dict:
    for row in data["admission_benchmarks"]:
        if int(row["workers"]) == workers:
            return row
    return max(data["admission_benchmarks"], key=lambda row: int(row["workers"]))


def add_row(rows: list[dict], name: str, p95_parts: dict[str, float], throughput_parts: dict[str, float], evidence: str, limitation: str) -> None:
    total_p95 = sum(p95_parts.values())
    bottleneck = min(throughput_parts, key=throughput_parts.get)
    rows.append(
        {
            "path": name,
            "component_derived_p95_ms": round(total_p95, 3),
            "bottleneck_reports_per_s": round(throughput_parts[bottleneck], 3),
            "bottleneck_component": bottleneck,
            "p95_parts": p95_parts,
            "throughput_parts": throughput_parts,
            "evidence": evidence,
            "limitation": limitation,
        }
    )


def main() -> None:
    e2e = read_json("end_to_end_admission_benchmark.json")
    etcd = read_json("etcd_linearizable_admission_benchmark.json")
    cost = read_json("deployment_cost_accounting.json")

    sqlite32 = best_worker_row(e2e, 32)
    etcd16 = best_worker_row(etcd, 16)
    sqlite_p95 = float(sqlite32["latency_ms"]["p95"])
    sqlite_tput = float(sqlite32["admissions_per_second"])
    etcd_p95 = float(etcd16["latency_ms"]["p95"])
    etcd_tput = float(etcd16["admissions_per_second"])

    rows: list[dict] = []
    add_row(
        rows,
        "local_sql_admission_only",
        {"sqlite_admission": sqlite_p95},
        {"sqlite_admission": sqlite_tput},
        "artifact/end_to_end_admission_benchmark.py, 32 workers",
        "single-node reproducibility backend; no Nitro, KMS, or replicated ledger",
    )
    add_row(
        rows,
        "local_three_node_etcd_admission",
        {"etcd_compare_put": etcd_p95},
        {"etcd_compare_put": etcd_tput},
        "artifact/etcd_linearizable_admission_benchmark.py, 16 clients",
        "local three-node etcd using etcdctl subprocesses; no Nitro or KMS",
    )
    add_row(
        rows,
        "nitro_plus_sql_admission_envelope",
        {"recorded_nitro_oracle": RECORDED_NITRO_P95_MS, "sqlite_admission": sqlite_p95},
        {"recorded_nitro_oracle": RECORDED_NITRO_REPORTS_PER_S, "sqlite_admission": sqlite_tput},
        "recorded scaled Nitro p95 plus SQLite admission artifact",
        "component-derived envelope; no live KMS or external ledger in same run",
    )
    add_row(
        rows,
        "nitro_plus_linearizable_ledger_envelope",
        {"recorded_nitro_oracle": RECORDED_NITRO_P95_MS, "etcd_compare_put": etcd_p95},
        {"recorded_nitro_oracle": RECORDED_NITRO_REPORTS_PER_S, "etcd_compare_put": etcd_tput},
        "recorded scaled Nitro p95 plus local three-node etcd check",
        "component-derived envelope; etcd run is local and uses subprocess clients",
    )
    for kms_ms in [5.0, 25.0]:
        add_row(
            rows,
            f"nitro_kms{kms_ms:g}_linearizable_ledger_envelope",
            {
                "recorded_nitro_oracle": RECORDED_NITRO_P95_MS,
                "kms_session_release": kms_ms,
                "etcd_compare_put": etcd_p95,
            },
            {
                "recorded_nitro_oracle": RECORDED_NITRO_REPORTS_PER_S,
                "kms_batch_release_upper": 128000.0 / kms_ms,
                "etcd_compare_put": etcd_tput,
            },
            "recorded Nitro p95, local etcd check, and explicit KMS latency parameter",
            "what-if envelope for KMS release latency; not a measured AWS KMS integration",
        )

    units = {row["component"]: row for row in cost["rows"]}
    payload = {
        "artifact": "integrated_deployment_envelope",
        "description": "component-derived deployment envelope, not a live integrated cloud benchmark",
        "inputs": {
            "recorded_nitro_p95_ms": RECORDED_NITRO_P95_MS,
            "recorded_nitro_reports_per_s": RECORDED_NITRO_REPORTS_PER_S,
            "recorded_nitro_runtime_usd_per_m": RECORDED_NITRO_RUNTIME_USD_PER_M,
            "sqlite_32_worker_p95_ms": sqlite_p95,
            "sqlite_32_worker_reports_per_s": sqlite_tput,
            "etcd_16_client_p95_ms": etcd_p95,
            "etcd_16_client_reports_per_s": etcd_tput,
            "kms_release_requests_per_m": units["kms_or_session_key_release"]["count"],
            "ledger_writes_per_m": units["linearizable_ledger_write"]["count"],
        },
        "rows": rows,
        "deployment_units_per_million": cost["rows"],
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "path",
                "component_derived_p95_ms",
                "bottleneck_reports_per_s",
                "bottleneck_component",
                "evidence",
                "limitation",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in writer.fieldnames})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
